# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

import frappe


def before_tests():
	"""Bootstrap a bare test site (company, item groups, UOMs) before tests.

	Cross-version: ERPNext v15 ships erpnext.setup.utils.before_tests and we
	delegate to it; ERPNext v16 removed it, so there we complete the setup
	wizard directly. Both paths are no-ops when a Company already exists.
	"""
	try:
		from erpnext.setup.utils import before_tests as erpnext_before_tests
	except ImportError:
		erpnext_before_tests = None

	if erpnext_before_tests:
		erpnext_before_tests()
		return

	frappe.clear_cache()
	if not frappe.db.a_row_exists("Company"):
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
		from frappe.utils import now_datetime

		year = now_datetime().year
		setup_complete(
			{
				"currency": "NGN",
				"full_name": "Test User",
				"company_name": "Doftwerks Test Co",
				"timezone": "Africa/Lagos",
				"company_abbr": "DTC",
				"industry": "Services",
				"country": "Nigeria",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"company_tagline": "Testing",
				"email": "test@example.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)
