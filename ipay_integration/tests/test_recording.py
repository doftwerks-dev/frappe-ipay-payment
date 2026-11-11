# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay records a payment it made, the way it does in production: a signed and encrypted POST /ipay/plugin/payments through Frappe's
whole web stack, against a real submitted Purchase Invoice, real iPay Settings (keys in the encrypted fields) and ERPNext's own
Payment Entry. The Payment Entry is made once, whatever the number of calls.

The request is served in another thread with its own database connection, so what it needs is committed first. That is fine on a
throwaway test site; the names carry a random part so the test can run again on the same site."""

import base64
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.test_api import make_request
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, get_test_client

from ipay_integration import ipay
from ipay_integration.ipay_plugin_kit import KeySet, envelope
from ipay_integration.service_user import SERVICE_USER

COMPANY = "iPay Recording Test Co"
ABBR = "IRTC"
ITEM = "iPay Test Service"
PRICE_LIST = "iPay Test Buying NGN"
CLIENT = "recording-test-client"
KID = "recording-key"
PATH = "/ipay/plugin/payments"


def _b64(raw):
	return base64.b64encode(raw).decode()


class TestRecordingThroughTheRoute(FrappeTestCase):
	def setUp(self):
		self.client = get_test_client(use_cookies=False)
		# Throwaway keys made for this run only, stored the way a real site stores them.
		self.request_keys = KeySet(KID, os.urandom(32), os.urandom(32))
		self.callback_keys = KeySet(KID, os.urandom(32), os.urandom(32))
		self.suffix = frappe.generate_hash(length=8)

	# ---- setup ---------------------------------------------------------------------------------------------------------

	def _company(self):
		if not frappe.db.exists("Company", COMPANY):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": COMPANY,
					"abbr": ABBR,
					"default_currency": "NGN",
					"country": "Nigeria",
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": "Standard",
				}
			).insert()
		if not frappe.db.exists("Price List", PRICE_LIST):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": PRICE_LIST,
					"currency": "NGN",
					"buying": 1,
					"enabled": 1,
				}
			).insert()
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": ITEM,
					"item_name": ITEM,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

	def _settings(self):
		settings = frappe.get_single(ipay.SETTINGS_DOCTYPE)
		settings.enabled = 1
		settings.base_url = "https://ipay.example"
		row = next((r for r in settings.accounts if r.company == COMPANY), None) or settings.append(
			"accounts", {"company": COMPANY}
		)
		row.update(
			{
				"client_id": CLIENT,
				"key_id": KID,
				"client_secret": "test-only-" + self.suffix,
				"request_enc_key": _b64(self.request_keys.enc_key),
				"request_sign_key": _b64(self.request_keys.sign_key),
				"callback_enc_key": _b64(self.callback_keys.enc_key),
				"callback_sign_key": _b64(self.callback_keys.sign_key),
			}
		)
		settings.save()
		account = next(r for r in settings.accounts if r.company == COMPANY).payment_account
		self.assertTrue(account, "saving iPay Settings sets up the iPay ledger account")
		return account

	def _invoice(self, amount):
		unique = frappe.generate_hash(length=8)
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": f"iPay Test Supplier {unique}",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}),
				"default_currency": "NGN",
			}
		).insert()
		invoice = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": COMPANY,
				"supplier": supplier.name,
				"currency": "NGN",
				"conversion_rate": 1,
				"buying_price_list": PRICE_LIST,
				"bill_no": f"BILL-{unique}",
				"items": [{"item_code": ITEM, "qty": 1, "rate": amount}],
			}
		)
		invoice.insert()
		invoice.submit()
		return invoice

	def _asked_for(self, invoice, amount):
		"""The iPay Payment this site made when someone pressed Pay with iPay; iPay has since moved the money."""
		frappe.get_doc(
			{
				"doctype": ipay.PAYMENT_DOCTYPE,
				"purchase_invoice": invoice.name,
				"company": COMPANY,
				"supplier": invoice.supplier,
				"amount": amount,
				"external_reference": f"erpnext:test:{self.suffix}",
				"status": "transferred",
				"ipay_payment_id": f"pay_{self.suffix}",
			}
		).insert(ignore_permissions=True)

	# ---- iPay's side -----------------------------------------------------------------------------------------------------

	def _ipay_records(self, payload):
		return self._ipay_posts(PATH, payload)

	def _ipay_posts(self, path, payload):
		"""What iPay sends: sealed with the callback keys. Returns the answer, opened with the request keys."""
		sealed = envelope.seal(self.callback_keys, CLIENT, "POST", path, payload)
		response = make_request(
			target=self.client.post,
			args=(path,),
			kwargs={"data": sealed["body"], "headers": sealed["headers"], "content_type": "application/json"},
		)
		# The request committed on its own connection. End this connection's read snapshot (MariaDB repeatable read) so what the
		# test reads next includes it; nothing of the test's own is pending here, its setup was committed before the first call.
		frappe.db.rollback()
		body = response.get_data(as_text=True)
		self.assertEqual(response.status_code, 200, body)
		answer = envelope.open_(
			{self.request_keys.kid: self.request_keys},
			CLIENT,
			envelope.RESPONSE,
			path,
			dict(response.headers.items()),
			body,
			expect_nonce=sealed["nonce"],
		)
		# The answer is bound to its own call's nonce (checked above); what is compared is the rest.
		answer.pop("nonce", None)
		return answer

	# ---- the test --------------------------------------------------------------------------------------------------------

	def test_ipay_records_a_payment_once_as_a_payment_entry(self):
		self._company()
		payment_account = self._settings()
		invoice = self._invoice(1000)
		self._asked_for(invoice, 1000)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - the request is served on another connection

		reference = f"qck-trsf_{self.suffix}"
		payload = {"bill_id": invoice.name, "amount": "1000.00", "currency": "NGN", "reference": reference}
		first = self._ipay_records(payload)
		again = self._ipay_records(payload)  # a retry after a slow answer: a new nonce, the same reference

		self.assertEqual(first["outcome"], "recorded", first)
		self.assertTrue(first["erp_payment_id"])
		self.assertEqual(again, first, "the same reference is answered with the same Payment Entry")

		entries = frappe.get_all(
			"Payment Entry",
			filters={"reference_no": reference},
			fields=["name", "docstatus", "paid_from", "mode_of_payment", "paid_amount", "party", "owner"],
		)
		self.assertEqual(len(entries), 1, entries)
		entry = entries[0]
		self.assertEqual(entry.name, first["erp_payment_id"])
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.paid_from, payment_account)
		self.assertEqual(entry.mode_of_payment, ipay.MODE_OF_PAYMENT)
		self.assertEqual(flt(entry.paid_amount), 1000)
		self.assertEqual(entry.party, invoice.supplier)
		self.assertEqual(entry.owner, SERVICE_USER, "made by iPay's service user, not Administrator")

		paid = frappe.db.get_value(
			"Purchase Invoice", invoice.name, ["outstanding_amount", "status", "ipay_status"], as_dict=True
		)
		self.assertEqual(flt(paid.outstanding_amount), 0)
		self.assertEqual(paid.status, "Paid")
		self.assertEqual(paid.ipay_status, "Paid")
		self.assertEqual(
			frappe.db.get_value(ipay.PAYMENT_DOCTYPE, {"purchase_invoice": invoice.name}, "status"),
			"recorded",
		)
		self.assertEqual(frappe.db.count("iPay Recording", {"reference": reference}), 1)

	def test_a_payment_made_in_ipays_panel_inside_erpnext_is_adopted_and_recorded_once(self):
		self._company()
		self._settings()
		invoice = self._invoice(2500)
		not_opened = self._invoice(700)

		# Pay with iPay, for a company that pays inside ERPNext: iPay answers with its panel's address (stubbed here, nothing else is).
		launch = MagicMock()
		launch.config.base_url = "https://ipay.example"
		launch.panel_launch.return_value = {
			"panel_url": "https://ipay.example/erp-payments/in-erp/c#l=once",
			"expires_in": 120,
		}
		with patch.object(ipay, "_client", return_value=launch):
			opened = ipay.open_panel(invoice.name)
			reopened = ipay.open_panel(invoice.name)
		self.assertEqual(opened["panel_url"], "https://ipay.example/erp-payments/in-erp/c#l=once")
		self.assertEqual(reopened, opened)
		waiting = frappe.get_all(
			ipay.PAYMENT_DOCTYPE,
			filters={"purchase_invoice": invoice.name},
			fields=["external_reference", "route"],
		)
		self.assertEqual(len(waiting), 1, "opening the panel twice keeps one waiting launch")
		self.assertTrue(waiting[0].external_reference.startswith(ipay.PANEL_LAUNCH_PREFIX))
		self.assertEqual(waiting[0].route, "panel")
		self.assertFalse(
			frappe.db.get_value("Purchase Invoice", invoice.name, "ipay_status"),
			"nothing shown until iPay reports",
		)

		# An address that is not iPay's own is never framed.
		launch.panel_launch.return_value = {"panel_url": "https://evil.example/erp-payments/in-erp/c#l=x"}
		with patch.object(ipay, "_client", return_value=launch), self.assertRaises(frappe.ValidationError):
			ipay.open_panel(invoice.name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - the requests are served on another connection

		# iPay reports the payment made in the panel; its reference is iPay's own.
		reference = f"panel-{self.suffix}-0000-4000-8000-000000000000"

		def event(bill, status, ref=reference):
			payment = {"id": f"pay_{self.suffix}", "status": status, "bill_id": bill, "amount": "2500.00"}
			payment["origin"] = {"name": "ERPNext", "bill_url": None, "in_erp": True}
			return {
				"event_id": f"pay_{self.suffix}:{status}",
				"event": f"payment.{status}",
				"external_reference": ref,
				"occurred_at": "2026-11-11T09:00:00Z",
				"payment": payment,
			}

		self.assertEqual(
			self._ipay_posts("/ipay/plugin/payment-events", event(invoice.name, "processing")),
			{"received": True},
		)
		self._ipay_posts(
			"/ipay/plugin/payment-events", event(not_opened.name, "processing", ref=reference + "-x")
		)

		adopted = frappe.db.get_value(
			ipay.PAYMENT_DOCTYPE,
			{"purchase_invoice": invoice.name},
			["external_reference", "ipay_payment_id", "status", "amount"],
			as_dict=True,
		)
		self.assertEqual(adopted.external_reference, reference)
		self.assertEqual(adopted.ipay_payment_id, f"pay_{self.suffix}")
		self.assertEqual(adopted.status, "processing")
		self.assertEqual(frappe.db.get_value("Purchase Invoice", invoice.name, "ipay_status"), "Processing")
		self.assertFalse(
			frappe.db.exists(ipay.PAYMENT_DOCTYPE, {"purchase_invoice": not_opened.name}),
			"a panel payment for an invoice this site opened no panel for is not taken",
		)

		# iPay records it, once.
		recorded = f"qck-trsf_{self.suffix}"
		payload = {"bill_id": invoice.name, "amount": "2500.00", "currency": "NGN", "reference": recorded}
		first = self._ipay_records(payload)
		self.assertEqual(first["outcome"], "recorded", first)
		self.assertEqual(self._ipay_records(payload), first)
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": recorded, "docstatus": 1}), 1)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", invoice.name, "status"), "Paid")
		self.assertEqual(
			frappe.db.get_value(ipay.PAYMENT_DOCTYPE, {"external_reference": reference}, "status"), "recorded"
		)

		# Recording a payment for an invoice this site asked nothing for is refused, panel or not.
		self.assertEqual(
			self._ipay_records(
				{
					"bill_id": not_opened.name,
					"amount": "700.00",
					"currency": "NGN",
					"reference": recorded + "-x",
				}
			)["outcome"],
			"rejected",
			"a payment for an invoice this site asked nothing for is refused",
		)
