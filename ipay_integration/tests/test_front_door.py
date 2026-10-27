# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""Tests for the front door. iPay is never called: the client is patched. The signing and encryption are tested separately, against
messages produced by iPay's own implementation (kit_tests/)."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ipay_integration import ipay, plugin_api
from ipay_integration.ipay_plugin_kit import IpayError


class TestFrontDoor(FrappeTestCase):
	def test_reference_keeps_only_characters_iPay_accepts(self):
		self.assertEqual(ipay._reference_part("erpnext:ACME/Co:ACC PINV#1:1"), "erpnext:ACME-Co:ACC-PINV-1:1")

	def test_status_only_moves_forward(self):
		record = frappe._dict(status="recorded", ipay_payment_id="p1", purchase_invoice="PINV-1")
		record.db_set = MagicMock()
		with patch.object(ipay, "_sync_invoice"):
			ipay.apply_status(record, "processing")
		record.db_set.assert_not_called()

	def test_a_known_later_status_is_applied(self):
		record = frappe._dict(status="processing", ipay_payment_id="p1", purchase_invoice="PINV-1")
		record.db_set = MagicMock()
		with patch.object(ipay, "_sync_invoice"):
			ipay.apply_status(record, "recorded")
		record.db_set.assert_any_call("status", "recorded", update_modified=True)

	def test_an_unknown_status_is_ignored(self):
		record = frappe._dict(status="processing", ipay_payment_id="p1", purchase_invoice="PINV-1")
		record.db_set = MagicMock()
		with patch.object(ipay, "_sync_invoice"):
			ipay.apply_status(record, "banana")
		record.db_set.assert_not_called()

	def test_only_ipay_payers_may_start_a_payment(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				ipay.pay_invoice("ANY")
		finally:
			frappe.set_user("Administrator")

	def test_ipay_refusing_the_person_is_said_plainly(self):
		client = MagicMock()
		client.request_payment.side_effect = IpayError("no", code="requester_not_allowed", status=403)
		doc = frappe._dict(name="PINV-1", company="Acme", supplier="S", supplier_name="S", outstanding_amount=100, currency="NGN", docstatus=1)
		doc.check_permission = lambda *_: None
		with patch.object(ipay, "_client", return_value=client), patch.object(ipay, "_preflight", return_value=[]), patch.object(
			ipay, "_open_payment", return_value=None
		), patch.object(ipay, "_next_reference", return_value="r"), patch.object(
			ipay, "_supplier_bank_details", return_value=frappe._dict(bank_account_no="0123456789", bank="B")
		), patch.object(ipay, "_bank_code", return_value="058"), patch.object(frappe, "get_doc", return_value=doc), patch.object(
			frappe, "only_for"
		), patch.object(frappe.db, "get_value", return_value="ada@acme.test"), patch.object(frappe, "get_single", return_value=frappe._dict(accounts=[frappe._dict(company="Acme")])), patch.object(
			ipay, "_find_account", return_value=frappe._dict(company="Acme")
		):
			with self.assertRaises(frappe.ValidationError):
				ipay.pay_invoice("PINV-1")

	def test_a_nonce_can_be_claimed_once(self):
		key = frappe.generate_hash(length=22)
		self.assertTrue(plugin_api.claim_nonce("k", key))
		self.assertFalse(plugin_api.claim_nonce("k", key))
