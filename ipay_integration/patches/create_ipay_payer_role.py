# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

import frappe


def execute():
	"""The iPay Payer role is named by the permissions of this app's doctypes, so it must exist before they are synced."""
	if not frappe.db.exists("Role", "iPay Payer"):
		frappe.get_doc({"doctype": "Role", "role_name": "iPay Payer", "desk_access": 1}).insert(ignore_permissions=True)
