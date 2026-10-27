# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay front door for ERPNext.

This app asks iPay to pay a Purchase Invoice and sends the accountant to iPay's confirmation page. iPay decides, moves the money and
is the record of the payment; ERPNext records the Payment Entry only when iPay tells it to (see plugin_api.py), in a signed and
encrypted call. Nothing here holds an iPay password or PIN, calls iPay's transfer API, or writes a payment on its own authority.

The envelope keys and the OAuth client secret are Password fields (encrypted by Frappe), never committed, never logged and never
returned by any method. The repository is public: nothing secret may ever be added to it.
"""

import re

import frappe
from frappe.utils import cint, cstr, flt

from ipay_integration.ipay_plugin_kit import EnvelopeError, IpayClient, IpayError, KeySet, PluginConfig

SETTINGS_DOCTYPE = "iPay Settings"
PAYMENT_DOCTYPE = "iPay Payment"

IPAY_BANK_NAME = "iPay"
MODE_OF_PAYMENT = "iPay"

PAYER_ROLE = "iPay Payer"

# iPay's statuses, in the order a payment only ever moves forward, and what the Purchase Invoice shows for each.
STATUS_RANK = {
	"awaiting_confirm": 0,
	"in_approval": 1,
	"processing": 2,
	"transferred": 3,
	"recorded": 4,
	"failed": 5,
	"rejected": 5,
	"cancelled": 5,
}
OPEN_STATUSES = ("awaiting_confirm", "in_approval", "processing", "transferred")
TERMINAL_STATUSES = ("recorded", "failed", "rejected", "cancelled")
INVOICE_STATUS = {
	"awaiting_confirm": "Awaiting Confirmation",
	"in_approval": "In Approval",
	"processing": "Processing",
	"transferred": "Transferred",
	"recorded": "Paid",
	"failed": "Failed",
	"rejected": "Rejected",
	"cancelled": "Cancelled",
}

SECRET_FIELDS = (
	"client_secret",
	"request_enc_key",
	"request_sign_key",
	"callback_enc_key",
	"callback_sign_key",
)


class IPayConfigError(Exception):
	pass


# ---------------------------------------------------------------------------
# Configuration and the iPay client
# ---------------------------------------------------------------------------


def _find_account(settings, company):
	for row in settings.accounts:
		if row.company == company:
			return row
	return None


def account_by_client_id(settings, client_id):
	"""The Account row for the API client id an incoming iPay call names, or None."""
	if not client_id:
		return None
	for row in settings.accounts:
		if row.client_id == client_id:
			return row
	return None


def plugin_config(settings, row):
	"""The client configuration for one company, from the encrypted fields. Raises IPayConfigError naming what is missing, never a value."""
	missing = [f for f in ("client_id", "key_id", *SECRET_FIELDS) if not (row.get(f) if f in ("client_id", "key_id") else row.get_password(f, raise_exception=False))]
	if missing:
		raise IPayConfigError(frappe._("iPay Settings is incomplete for {0}: {1}").format(row.company, ", ".join(missing)))
	try:
		previous = None
		if row.get_password("prev_callback_enc_key", raise_exception=False):
			previous = KeySet.from_base64(
				cstr(row.prev_key_id),
				row.get_password("prev_callback_enc_key"),
				row.get_password("prev_callback_sign_key"),
			)
		return PluginConfig(
			base_url=cstr(settings.base_url),
			client_id=cstr(row.client_id),
			client_secret=row.get_password("client_secret"),
			request_keys=KeySet.from_base64(row.key_id, row.get_password("request_enc_key"), row.get_password("request_sign_key")),
			callback_keys=KeySet.from_base64(row.key_id, row.get_password("callback_enc_key"), row.get_password("callback_sign_key")),
			previous_callback_keys=previous,
		)
	except (EnvelopeError, IpayError) as e:
		raise IPayConfigError(frappe._("The iPay keys for {0} are not valid ({1}).").format(row.company, getattr(e, "reason", None) or e)) from e


def _client(settings, row):
	return IpayClient(plugin_config(settings, row))


def callback_keys_for(settings, row):
	"""The keys iPay's calls to us are opened with (current, then the previous during a rotation), and the keys we answer with."""
	config = plugin_config(settings, row)
	keys = [config.callback_keys] + ([config.previous_callback_keys] if config.previous_callback_keys else [])
	return config, keys


@frappe.whitelist(methods=["POST"])
def test_connection():
	frappe.only_for("System Manager")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	results = []
	for row in settings.accounts:
		try:
			info = _client(settings, row).ping()
			results.append({"company": row.company, "ok": True, "detail": frappe._("Connected ({0}, {1})").format(cstr(info.get("erp")), cstr(info.get("environment")))})
		except (IPayConfigError, IpayError) as e:
			results.append({"company": row.company, "ok": False, "detail": cstr(e)})
	return results


# ---------------------------------------------------------------------------
# Ledger plumbing: the "iPay" bank account payments are recorded against
# ---------------------------------------------------------------------------


def ensure_payment_setup(settings):
	"""Create the ledger plumbing each company needs so iPay payments can be recorded: a Bank, an Account of type Bank, a company Bank
	Account and a Mode of Payment. Idempotent - search first, create only when missing."""
	for row in settings.accounts:
		if not row.company:
			continue
		account = _ensure_company_account(row.company)
		if not account:
			continue
		if not row.payment_account:
			row.payment_account = account
		_ensure_bank_account(row.company, account)
		_ensure_mode_of_payment(row.company, account)


def _ensure_company_account(company):
	abbr, currency = frappe.get_cached_value("Company", company, ["abbr", "default_currency"])
	name = f"{IPAY_BANK_NAME} - {abbr}"
	if frappe.db.exists("Account", name):
		return name

	parent = frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name")
	if not parent:
		frappe.msgprint(
			frappe._(
				"Could not find a Bank group account for {0}, so the iPay ledger account was not created. Create an Account named iPay (type Bank) manually and set it as Payment Account on the iPay Settings row."
			).format(company),
			indicator="orange",
		)
		return None

	frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": IPAY_BANK_NAME,
			"company": company,
			"parent_account": parent,
			"account_type": "Bank",
			"account_currency": currency,
		}
	).insert(ignore_permissions=True)
	return name


def _ensure_bank_account(company, account):
	if not frappe.db.exists("Bank", IPAY_BANK_NAME):
		frappe.get_doc({"doctype": "Bank", "bank_name": IPAY_BANK_NAME}).insert(ignore_permissions=True)

	if frappe.db.exists("Bank Account", {"company": company, "account": account}):
		return
	abbr = frappe.get_cached_value("Company", company, "abbr")
	frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": f"{IPAY_BANK_NAME} Wallet {abbr}",
			"bank": IPAY_BANK_NAME,
			"is_company_account": 1,
			"company": company,
			"account": account,
		}
	).insert(ignore_permissions=True)


def _ensure_mode_of_payment(company, account):
	if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT):
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": MODE_OF_PAYMENT,
				"enabled": 1,
				"type": "Bank",
				"accounts": [{"company": company, "default_account": account}],
			}
		).insert(ignore_permissions=True)
		return

	mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT)
	if not any(r.company == company for r in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": account})
		mop.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Supplier destination bank
# ---------------------------------------------------------------------------


def _supplier_bank_details(supplier):
	"""The supplier's default (else any) active ERPNext Bank Account."""
	filters = {"party_type": "Supplier", "party": supplier, "disabled": 0}
	fields = ["name", "bank", "bank_account_no", "branch_code"]
	bank = frappe.db.get_value("Bank Account", {**filters, "is_default": 1}, fields, as_dict=True)
	return bank or frappe.db.get_value("Bank Account", filters, fields, as_dict=True)


def _bank_code(bank):
	"""NIP code for a supplier Bank Account row: the curated code on the Bank master first, the row's own Branch Code as fallback."""
	code = bank.bank and frappe.db.get_value("Bank", bank.bank, "ipay_nip_code")
	return cstr(code or bank.branch_code or "")


@frappe.whitelist(methods=["POST"])
def save_supplier_bank(supplier, bank, account_number):
	"""Create or update the supplier's default Bank Account from the pay dialog. Standard permission checks apply: the caller needs
	Bank Account write rights."""
	frappe.only_for(PAYER_ROLE)
	account_number = cstr(account_number).strip()
	if not re.fullmatch(r"\d{10}", account_number):
		frappe.throw(frappe._("NUBAN account numbers are exactly 10 digits."))
	if not frappe.db.exists("Bank", bank):
		frappe.throw(frappe._("Bank {0} does not exist.").format(bank))

	existing = _supplier_bank_details(supplier)
	if existing:
		if existing.bank == bank and existing.bank_account_no == account_number:
			return existing.name
		doc = frappe.get_doc("Bank Account", existing.name)
		doc.bank = bank
		doc.bank_account_no = account_number
		doc.save()
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier,
			"bank": bank,
			"party_type": "Supplier",
			"party": supplier,
			"bank_account_no": account_number,
			"is_default": 1,
		}
	).insert()
	return doc.name


# ---------------------------------------------------------------------------
# Pay via iPay on a Purchase Invoice
# ---------------------------------------------------------------------------


def _reference_part(value):
	"""iPay accepts letters, digits and . _ : - in an external reference (100 at most)."""
	return re.sub(r"[^A-Za-z0-9._:-]", "-", cstr(value))


@frappe.whitelist()
def get_payment_context(name):
	"""What the pay dialog shows: the outstanding amount, the supplier's saved destination bank and any request already open."""
	frappe.only_for(PAYER_ROLE)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("read")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	row = _find_account(settings, doc.company)

	bank = _supplier_bank_details(doc.supplier)
	context = {
		"enabled": cint(settings.enabled),
		"configured": bool(row and row.get("payment_account") and _configured(row)),
		"outstanding": flt(doc.outstanding_amount),
		"currency": cstr(doc.currency),
		"supplier": cstr(doc.supplier),
		"supplier_name": cstr(doc.supplier_name or doc.supplier),
		"bank": None,
		"payment": None,
	}
	if bank:
		context["bank"] = {"name": bank.name, "bank": cstr(bank.bank), "bank_account_no": cstr(bank.bank_account_no), "bank_code": _bank_code(bank)}

	open_request = _open_payment(doc.name)
	if open_request:
		context["payment"] = {"name": open_request.name, "status": open_request.status, "amount": flt(open_request.amount)}
	return context


def _configured(row):
	return all((row.get(f) if f in ("client_id", "key_id") else row.get_password(f, raise_exception=False)) for f in ("client_id", "key_id", *SECRET_FIELDS))


def _open_payment(invoice):
	rows = frappe.get_all(PAYMENT_DOCTYPE, filters={"purchase_invoice": invoice, "status": ["in", list(OPEN_STATUSES)]}, fields=["name", "status", "amount", "external_reference"], order_by="creation desc", limit=1)
	return frappe._dict(rows[0]) if rows else None


@frappe.whitelist(methods=["POST"])
def pay_invoice(name):
	"""Asks iPay to pay this invoice. Returns {"route", "status", "confirm_url"}: open the link in the payer's browser.

	Everything is checked here, for whoever calls. The link is a secret for 30 minutes: it is returned to the caller and never stored or logged."""
	frappe.only_for(PAYER_ROLE)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("write")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	row = _find_account(settings, doc.company)

	errors = _preflight(doc, settings, row)
	if errors:
		frappe.throw("<br>".join(errors), title=frappe._("iPay Payment"))

	email = cstr(frappe.db.get_value("User", frappe.session.user, "email")).strip()
	if not email:
		frappe.throw(frappe._("Your user has no email address. iPay identifies who is paying by their email."))

	bank = _supplier_bank_details(doc.supplier)
	amount = flt(doc.outstanding_amount, 2)

	open_request = _open_payment(doc.name)
	if open_request and flt(open_request.amount, 2) != amount:
		frappe.throw(frappe._("A payment of {0} is already in progress for this invoice. Finish or cancel it in iPay first.").format(open_request.amount))
	reference = open_request.external_reference if open_request else _next_reference(doc)

	try:
		answer = _client(settings, row).request_payment(
			external_reference=reference,
			bill_id=doc.name,
			amount=f"{amount:.2f}",
			requested_by_email=email,
			supplier_id=cstr(doc.supplier),
			supplier_name=cstr(doc.supplier_name or doc.supplier)[:200],
			bank_code=_bank_code(bank),
			account_number=cstr(bank.bank_account_no),
			bill_url=_bill_url(doc),
		)
	except IPayConfigError as e:
		frappe.throw(cstr(e))
	except IpayError as e:
		if e.code == "requester_not_allowed":
			frappe.throw(frappe._("iPay does not allow {0} to pay bills. Ask the iPay administrator to give them access.").format(email))
		frappe.throw(frappe._("iPay could not start this payment: {0}").format(cstr(e)))

	payment = answer.get("payment") or {}
	route = cstr(payment.get("route") or answer.get("route") or "direct")
	record = frappe.get_doc(PAYMENT_DOCTYPE, open_request.name) if open_request else _create_payment_record(doc, amount, reference, bank, email)
	record.db_set("route", route if route in ("direct", "approval") else "direct", update_modified=False)
	apply_status(record, cstr(payment.get("status") or "awaiting_confirm"), cstr(payment.get("id")) or None)

	return {"route": route, "status": cstr(payment.get("status")), "confirm_url": None if route == "approval" else answer.get("confirm_url")}


def _preflight(doc, settings, row):
	"""Every reason the request cannot be made, at once."""
	errors = []
	if not cint(settings.enabled):
		errors.append(frappe._("The iPay integration is disabled. Enable it in iPay Settings."))
	if doc.docstatus != 1:
		errors.append(frappe._("The invoice must be submitted."))
	if flt(doc.outstanding_amount) <= 0:
		errors.append(frappe._("Nothing is outstanding on this invoice."))
	if cstr(doc.currency) != "NGN":
		errors.append(frappe._("iPay pays in NGN only; this invoice is in {0}.").format(doc.currency))
	if not row:
		errors.append(frappe._("Company {0} has no iPay account row in iPay Settings.").format(doc.company))
	elif not row.get("payment_account"):
		errors.append(frappe._("Company {0} has no Payment Account in iPay Settings.").format(doc.company))
	elif not _configured(row):
		errors.append(frappe._("iPay Settings is incomplete for {0}: the keys are missing.").format(doc.company))
	bank = _supplier_bank_details(doc.supplier)
	if not bank or not cstr(bank.bank_account_no).strip() or not _bank_code(bank):
		errors.append(frappe._("The supplier needs a bank account number and a bank with an iPay NIP code."))
	return errors


def _next_reference(doc):
	count = frappe.db.count(PAYMENT_DOCTYPE, {"purchase_invoice": doc.name})
	return _reference_part(f"erpnext:{doc.company}:{doc.name}:{count + 1}")[:100]


def _bill_url(doc):
	url = cstr(frappe.utils.get_url_to_form("Purchase Invoice", doc.name))
	return url if url.startswith("https://") else None


def _create_payment_record(doc, amount, reference, bank, email):
	return frappe.get_doc(
		{
			"doctype": PAYMENT_DOCTYPE,
			"purchase_invoice": doc.name,
			"company": doc.company,
			"supplier": doc.supplier,
			"amount": amount,
			"external_reference": reference,
			"status": "awaiting_confirm",
			"bank_account_last4": cstr(bank.bank_account_no)[-4:],
			"requested_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def refresh_payment_status(name):
	"""Reads where iPay says the open payments of this invoice stand (events normally keep this current)."""
	frappe.only_for(PAYER_ROLE)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("read")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	row = _find_account(settings, doc.company)
	if not row:
		frappe.throw(frappe._("Company {0} has no iPay account row in iPay Settings.").format(doc.company))
	status = None
	for item in frappe.get_all(PAYMENT_DOCTYPE, filters={"purchase_invoice": doc.name, "status": ["in", list(OPEN_STATUSES)], "ipay_payment_id": ["!=", ""]}, pluck="name"):
		record = frappe.get_doc(PAYMENT_DOCTYPE, item)
		try:
			data = _client(settings, row).payment(record.ipay_payment_id)
		except (IPayConfigError, IpayError) as e:
			frappe.throw(frappe._("iPay could not say: {0}").format(cstr(e)))
		status = cstr((data.get("payment") or data).get("status"))
		apply_status(record, status)
	return {"status": status}


# ---------------------------------------------------------------------------
# Status: forward only
# ---------------------------------------------------------------------------


def apply_status(record, status, ipay_payment_id=None, error=None):
	"""A late or replayed message never moves a payment backwards."""
	if status not in STATUS_RANK:
		return
	if status == "recorded" and not frappe.db.exists("iPay Recording", {"purchase_invoice": record.purchase_invoice}):
		return  # "recorded" is only ever true once a Payment Entry was recorded here, whatever an event says
	values = {}
	if ipay_payment_id and not record.ipay_payment_id:
		values["ipay_payment_id"] = ipay_payment_id
	# A payment that has ended stays as it ended: a late or replayed message cannot move recorded to failed, or back.
	if record.status not in TERMINAL_STATUSES and STATUS_RANK[status] >= STATUS_RANK.get(record.status, 0) and status != record.status:
		values["status"] = status
	if error:
		values["error"] = cstr(error)[:500]
	for field, value in values.items():
		record.db_set(field, value, update_modified=True)
	_sync_invoice(record)


def _sync_invoice(record):
	"""The invoice shows the latest request's status."""
	latest = frappe.get_all(PAYMENT_DOCTYPE, filters={"purchase_invoice": record.purchase_invoice}, fields=["status", "ipay_payment_id", "external_reference", "error"], order_by="creation desc", limit=1)
	if not latest:
		return
	latest = latest[0]
	values = {
		"ipay_status": INVOICE_STATUS.get(latest.status, ""),
		"ipay_reference": cstr(latest.ipay_payment_id or latest.external_reference),
		"ipay_error": cstr(latest.error),
	}
	if latest.status == "recorded":
		values["ipay_paid_at"] = frappe.utils.now()
	frappe.db.set_value("Purchase Invoice", record.purchase_invoice, values, update_modified=False)
