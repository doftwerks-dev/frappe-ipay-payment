# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""The user iPay's verified callbacks run as (plugin_api.py): reading a bill, recording a payment, applying a payment event.

Not Administrator: a site user with ERPNext's standard Accounts User role and nothing else, so a callback can do what recording a payment
needs (read the invoice, create and submit the Payment Entry) and no more than an accountant could. A custom role with only those rights
would need custom permissions on ERPNext's own doctypes, which freezes their standard permissions on the site; Accounts User does not.

Nobody signs in as it: it has no password, may only sign in from the server itself, and its address is in a domain that cannot receive
mail (.invalid). It is created on install and checked on every migrate, which also takes away any role added to it by hand.
"""

import frappe

SERVICE_USER = "ipay.callbacks@ipay-integration.invalid"
SERVICE_ROLES = ("Accounts User",)


def ensure():
	"""Creates the service user, or puts its roles back to exactly SERVICE_ROLES. A user an administrator disabled stays disabled:
	that stops iPay's callbacks (iPay retries them) until it is enabled again."""
	if not frappe.db.exists("User", SERVICE_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": SERVICE_USER,
				"first_name": "iPay Callbacks",
				"user_type": "System User",
				"send_welcome_email": 0,
				"enabled": 1,
				"restrict_ip": "127.0.0.1",
				"roles": [{"role": role} for role in SERVICE_ROLES],
			}
		).insert(ignore_permissions=True)
		return

	user = frappe.get_doc("User", SERVICE_USER)
	roles = {r.role for r in user.roles}
	if roles != set(SERVICE_ROLES) or user.user_type != "System User":
		user.set("roles", [{"role": role} for role in SERVICE_ROLES])
		user.user_type = "System User"
		user.save(ignore_permissions=True)


def ready():
	"""The service user's name when it can be used, else None (missing: run bench migrate; disabled: an administrator stopped it)."""
	return SERVICE_USER if frappe.db.get_value("User", SERVICE_USER, "enabled") else None
