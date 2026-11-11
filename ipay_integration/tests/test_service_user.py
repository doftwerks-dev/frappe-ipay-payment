# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay's callbacks run as a narrow service user, never Administrator, and only after the call is verified."""

import frappe
from frappe.tests.utils import FrappeTestCase

from ipay_integration import plugin_api, service_user
from ipay_integration.service_user import SERVICE_ROLES, SERVICE_USER


class TestServiceUser(FrappeTestCase):
	def _roles(self):
		return set(
			frappe.get_all("Has Role", filters={"parent": SERVICE_USER, "parenttype": "User"}, pluck="role")
		)

	def test_the_service_user_is_narrow_and_cannot_sign_in(self):
		service_user.ensure()  # what install and every migrate do
		user = frappe.db.get_value(
			"User", SERVICE_USER, ["enabled", "user_type", "restrict_ip"], as_dict=True
		)
		self.assertTrue(user.enabled)
		self.assertEqual(user.user_type, "System User")
		self.assertEqual(user.restrict_ip, "127.0.0.1")
		self.assertEqual(self._roles(), set(SERVICE_ROLES))
		self.assertNotIn("System Manager", frappe.get_roles(SERVICE_USER))
		self.assertFalse(
			frappe.db.exists("__Auth", {"doctype": "User", "name": SERVICE_USER, "fieldname": "password"}),
			"the service user has no password",
		)

	def test_it_may_record_a_payment_and_no_more_than_an_accountant(self):
		service_user.ensure()
		self.assertTrue(frappe.has_permission("Payment Entry", "create", user=SERVICE_USER))
		self.assertTrue(frappe.has_permission("Purchase Invoice", "read", user=SERVICE_USER))
		# Every desk user may edit its own User record; the service user may not edit anyone else's, nor the system's settings.
		self.assertFalse(frappe.has_permission("User", "write", doc="Administrator", user=SERVICE_USER))
		self.assertFalse(frappe.has_permission("System Settings", "write", user=SERVICE_USER))
		self.assertFalse(frappe.has_permission("iPay Settings", "read", user=SERVICE_USER))

	def test_a_role_added_by_hand_is_taken_away_on_migrate(self):
		service_user.ensure()
		user = frappe.get_doc("User", SERVICE_USER)
		user.append("roles", {"role": "System Manager"})
		user.save(ignore_permissions=True)
		self.assertIn("System Manager", self._roles())
		service_user.ensure()
		self.assertEqual(self._roles(), set(SERVICE_ROLES))

	def test_a_handler_runs_as_the_service_user_and_the_visitor_is_put_back(self):
		service_user.ensure()
		seen = []
		frappe.set_user("Guest")
		try:
			plugin_api.as_service_user(lambda payload: seen.append(frappe.session.user))({})
			self.assertEqual(seen, [SERVICE_USER])
			self.assertEqual(frappe.session.user, "Guest")

			def fails(payload):
				raise ValueError("the handler failed")

			with self.assertRaises(ValueError):
				plugin_api.as_service_user(fails)({})
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")

	def test_a_disabled_service_user_stops_the_handlers(self):
		service_user.ensure()
		frappe.db.set_value("User", SERVICE_USER, "enabled", 0)
		ran = []
		frappe.set_user("Guest")
		try:
			with self.assertRaises(RuntimeError):
				plugin_api.as_service_user(lambda payload: ran.append(1))({})
			self.assertEqual(ran, [])
			self.assertEqual(frappe.session.user, "Guest")
			service_user.ensure()
			self.assertFalse(
				frappe.db.get_value("User", SERVICE_USER, "enabled"),
				"a user an administrator disabled stays disabled",
			)
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("User", SERVICE_USER, "enabled", 1)
