# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay records a payment it made, the way it does in production: a signed and encrypted POST /ipay/plugin/payments through Frappe's
whole web stack, against a real submitted Purchase Invoice, real iPay Settings (keys in the encrypted fields) and ERPNext's own
Payment Entry. The Payment Entry is made once, whatever the number of calls.

The request is served in another thread with its own database connection, so what it needs is committed first. That is fine on a
throwaway test site; the names carry a random part so the test can run again on the same site."""

import base64
import os

import frappe
from frappe.tests.test_api import make_request
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, get_test_client

from ipay_integration import ipay
from ipay_integration.ipay_plugin_kit import KeySet, envelope

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
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": f"iPay Test Supplier {self.suffix}",
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
				"bill_no": f"BILL-{self.suffix}",
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
		"""What iPay sends: sealed with the callback keys. Returns the status and the answer, opened with the request keys."""
		sealed = envelope.seal(self.callback_keys, CLIENT, "POST", PATH, payload)
		response = make_request(
			target=self.client.post,
			args=(PATH,),
			kwargs={"data": sealed["body"], "headers": sealed["headers"], "content_type": "application/json"},
		)
		body = response.get_data(as_text=True)
		self.assertEqual(response.status_code, 200, body)
		answer = envelope.open_(
			{self.request_keys.kid: self.request_keys},
			CLIENT,
			envelope.RESPONSE,
			PATH,
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
			fields=["name", "docstatus", "paid_from", "mode_of_payment", "paid_amount", "party"],
		)
		self.assertEqual(len(entries), 1, entries)
		entry = entries[0]
		self.assertEqual(entry.name, first["erp_payment_id"])
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.paid_from, payment_account)
		self.assertEqual(entry.mode_of_payment, ipay.MODE_OF_PAYMENT)
		self.assertEqual(flt(entry.paid_amount), 1000)
		self.assertEqual(entry.party, invoice.supplier)

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
