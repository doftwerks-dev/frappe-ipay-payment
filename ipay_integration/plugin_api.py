# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""What iPay calls on this site (iPay docs/erp-plugin-api.md, "What the plugin must serve").

	GET  /ipay/plugin/bills/<invoice>     read a Purchase Invoice
	POST /ipay/plugin/payments            record a payment iPay has made
	POST /ipay/plugin/payment-events      a payment moved

Every call is verified by the plugin kit before anything is read: the client, the key id, a five minute clock window, the HMAC
signature and a nonce never seen before; the body is AES-256-GCM encrypted. A failure is a plain 401 with the reason only in the log.
Handlers run as the site's system user for the one company the verified client belongs to, never as a visitor.
"""

import json

import frappe
from frappe.utils import cstr, flt, nowdate
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from ipay_integration import ipay
from ipay_integration.ipay_plugin_kit import CallbackReceiver, NotFound

PREFIX = "/ipay/plugin"


class IpayPluginRenderer(BaseRenderer):
	"""Serves iPay's callbacks. Registered in hooks.py (page_renderer) so the path can be /ipay/plugin/bills/<id>, which a whitelisted
	method cannot be."""

	def can_render(self):
		return cstr(self.path).strip("/").startswith("ipay/plugin/")

	def render(self):
		request = frappe.local.request
		settings = frappe.get_single(ipay.SETTINGS_DOCTYPE)
		row = ipay.account_by_client_id(settings, request.headers.get("x-client-id", "")) if settings.enabled else None
		if row is None:
			return _response((401, {"Content-Type": "application/json"}, json.dumps({"error": "unauthenticated"})))

		try:
			config, callback_keys = ipay.callback_keys_for(settings, row)
		except ipay.IPayConfigError:
			frappe.log_error(title="iPay callback: settings incomplete", message=f"company {row.company}")
			return _response((401, {"Content-Type": "application/json"}, json.dumps({"error": "unauthenticated"})))

		receiver = build_receiver(row, config, callback_keys)
		target = request.full_path.rstrip("?")
		body = request.get_data(as_text=True)
		previous_user = frappe.session.user
		frappe.set_user("Administrator")  # after verification only: the handlers below touch records no visitor could
		try:
			return _response(receiver.handle(request.method, target, dict(request.headers), body))
		finally:
			frappe.set_user(previous_user)


def _response(reply):
	status, headers, body = reply
	return Response(body, status=status, headers=list(headers.items()))


def build_receiver(row, config, callback_keys):
	company = row.company

	def bill(bill_id):
		invoice = _invoice(company, bill_id, any_state=True)
		if invoice is None:
			raise NotFound()
		return {
			"id": invoice.name,
			"number": cstr(invoice.bill_no or invoice.name),
			"supplier_id": cstr(invoice.supplier),
			"supplier_name": cstr(invoice.supplier_name or invoice.supplier),
			"currency": cstr(invoice.currency),
			"total": f"{flt(invoice.grand_total, 2):.2f}",
			"amount_due": f"{flt(invoice.outstanding_amount, 2):.2f}",
			"status": "open" if _payable(invoice) else "closed",
		}

	def record_payment(payload):
		return record(row, payload)

	def payment_event(payload):
		payment = payload.get("payment") or {}
		found = frappe.db.get_value(ipay.PAYMENT_DOCTYPE, {"external_reference": cstr(payload.get("external_reference"))}, ["name", "company", "last_event_id"], as_dict=True)
		if not found or found.company != company:
			return  # not ours: acknowledged so iPay does not retry, nothing changed
		if found.last_event_id == payload.get("event_id"):
			return
		doc = frappe.get_doc(ipay.PAYMENT_DOCTYPE, found.name)
		doc.db_set("last_event_id", cstr(payload.get("event_id"))[:140], update_modified=False)
		status = cstr(payment.get("status") or cstr(payload.get("event")).split(".", 1)[-1])
		ipay.apply_status(doc, status, cstr(payment.get("id")) or None, payment.get("failure_reason"))

	return CallbackReceiver(
		row.client_id,
		config.request_keys,
		callback_keys,
		claim_nonce,
		bill=bill,
		record_payment=record_payment,
		payment_event=payment_event,
		base_path=PREFIX,
	)


def claim_nonce(kid, nonce):
	"""True the first time a nonce is seen, False after. The document name is the key, so the database makes it atomic."""
	try:
		frappe.get_doc({"doctype": "iPay Call Nonce", "nonce_key": f"{kid}:{nonce}"}).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return False
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the claim must outlast a failing handler
	return True


def prune_nonces():
	"""Nonces older than the clock window (plus margin) can never be replayed: the timestamp check refuses them first."""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
	frappe.db.delete("iPay Call Nonce", {"creation": ["<", cutoff]})


# ---------------------------------------------------------------------------
# Reading and recording
# ---------------------------------------------------------------------------


def _invoice(company, bill_id, any_state=False):
	name = cstr(bill_id)
	if not name or not frappe.db.exists("Purchase Invoice", name):
		return None
	invoice = frappe.get_doc("Purchase Invoice", name)
	if invoice.company != company or invoice.docstatus != 1 or invoice.is_return:
		return None
	return invoice


def _payable(invoice):
	return invoice.docstatus == 1 and flt(invoice.outstanding_amount) > 0 and cstr(invoice.status) not in ("Paid", "Cancelled", "Debit Note Issued")


def record(row, payload):
	"""Records a payment iPay made, once per `reference`. Returns {"outcome": "recorded"|"rejected"|"unknown", "erp_payment_id": str|None}."""
	reference = cstr(payload["reference"])[:100]
	existing = frappe.db.get_value("iPay Recording", {"reference": reference}, "payment_entry")
	if existing:
		return {"outcome": "recorded", "erp_payment_id": existing}

	invoice = _invoice(row.company, payload["bill_id"])
	if invoice is None or not _payable(invoice):
		return {"outcome": "rejected", "erp_payment_id": None}
	try:
		amount = flt(payload["amount"], 2)
	except (TypeError, ValueError):
		return {"outcome": "rejected", "erp_payment_id": None}
	if amount <= 0 or cstr(payload.get("currency")) != cstr(invoice.currency) or amount > flt(invoice.outstanding_amount, 2) + 0.005:
		return {"outcome": "rejected", "erp_payment_id": None}

	entry = _create_payment_entry(row, invoice, min(amount, flt(invoice.outstanding_amount, 2)), reference)
	frappe.get_doc({"doctype": "iPay Recording", "reference": reference, "purchase_invoice": invoice.name, "payment_entry": entry, "amount": amount}).insert(ignore_permissions=True)
	return {"outcome": "recorded", "erp_payment_id": entry}


def _create_payment_entry(row, invoice, amount, reference):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Purchase Invoice", invoice.name, party_amount=amount, bank_account=row.payment_account)
	pe.mode_of_payment = ipay.MODE_OF_PAYMENT
	pe.paid_from = row.payment_account
	pe.reference_no = reference
	pe.reference_date = nowdate()
	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name
