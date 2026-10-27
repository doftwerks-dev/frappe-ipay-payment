# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""Tests for what iPay calls on this site. They need a bench with ERPNext (run: bench --site test_site run-tests --app ipay_integration)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ipay_integration import ipay, plugin_api


class TestRecord(FrappeTestCase):
	def _row(self):
		return frappe._dict(company="Acme", payment_account="iPay - A", client_id="c")

	def _invoice(self, outstanding=1000):
		return frappe._dict(name="PINV-1", company="Acme", docstatus=1, is_return=0, currency="NGN", outstanding_amount=outstanding, status="Unpaid")

	def _patches(self, invoice, request="REQ-1", existing=None):
		return (
			patch.object(frappe.db, "get_value", side_effect=self._get_value(request, existing)),
			patch.object(plugin_api, "_invoice", return_value=invoice),
			patch.object(plugin_api, "_create_payment_entry", return_value="ACC-PAY-1"),
			patch.object(ipay, "apply_status"),
			patch.object(frappe, "get_doc"),
		)

	@staticmethod
	def _get_value(request, existing):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Invoice":
				return "PINV-1"
			if doctype == "iPay Recording":
				return existing
			return request

		return get_value

	def test_a_payment_nobody_asked_for_is_rejected(self):
		patches = self._patches(self._invoice(), request=None)
		with patches[0], patches[1], patches[2] as pe, patches[3], patches[4]:
			result = plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "500.00", "currency": "NGN", "reference": "r1"})
		self.assertEqual(result["outcome"], "rejected")
		pe.assert_not_called()

	def test_more_than_is_outstanding_or_another_currency_is_rejected(self):
		patches = self._patches(self._invoice(outstanding=300))
		with patches[0], patches[1], patches[2] as pe, patches[3], patches[4]:
			over = plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "500.00", "currency": "NGN", "reference": "r2"})
			usd = plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "100.00", "currency": "USD", "reference": "r3"})
		self.assertEqual((over["outcome"], usd["outcome"]), ("rejected", "rejected"))
		pe.assert_not_called()

	def test_a_reference_longer_than_the_limit_is_rejected_not_truncated(self):
		self.assertEqual(plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "1", "currency": "NGN", "reference": "x" * 101})["outcome"], "rejected")

	def test_a_known_reference_of_another_company_is_not_answered(self):
		patches = self._patches(self._invoice(), existing=frappe._dict(payment_entry="ACC-PAY-9", company="Other"))
		with patches[0], patches[1], patches[2] as pe, patches[3], patches[4]:
			result = plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "500.00", "currency": "NGN", "reference": "r4"})
		self.assertEqual(result["outcome"], "rejected")
		pe.assert_not_called()

	def test_a_validation_refusal_from_erpnext_is_a_rejection_and_rolled_back(self):
		patches = self._patches(self._invoice())
		with patches[0], patches[1], patch.object(plugin_api, "_create_payment_entry", side_effect=frappe.ValidationError("on hold")), patches[3], patches[4], patch.object(frappe.db, "rollback") as rollback:
			result = plugin_api.record(self._row(), {"bill_id": "PINV-1", "amount": "500.00", "currency": "NGN", "reference": "r5"})
		self.assertEqual(result["outcome"], "rejected")
		rollback.assert_called()

	def test_terminal_states_are_sticky(self):
		record = frappe._dict(status="recorded", ipay_payment_id="p1", purchase_invoice="PINV-1")
		calls = []
		record.db_set = lambda *a, **k: calls.append(a)
		with patch.object(ipay, "_sync_invoice"), patch.object(frappe.db, "exists", return_value=True):
			ipay.apply_status(record, "failed")
		self.assertEqual(calls, [])

	def test_recorded_is_not_claimed_when_nothing_was_recorded(self):
		record = frappe._dict(status="processing", ipay_payment_id="p1", purchase_invoice="PINV-1")
		calls = []
		record.db_set = lambda *a, **k: calls.append(a)
		with patch.object(ipay, "_sync_invoice"), patch.object(frappe.db, "exists", return_value=False):
			ipay.apply_status(record, "recorded")
		self.assertEqual(calls, [])
