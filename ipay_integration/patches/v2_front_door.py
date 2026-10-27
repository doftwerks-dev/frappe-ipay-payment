# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""Version 2 asks iPay through its plugin API instead of calling the transfer API itself.

- iPay Settings is switched off: each company must be registered in iPay as an ERP plugin and have its keys entered before it can pay.
  (The old client id and secret belong to the transfers API client; rotate that secret in iPay, old backups still hold it.)
- Purchase Invoices that version 1 left in flight are flagged: nothing tracks them any more, so someone must look them up in iPay.
Payment history (iPay Transaction) is kept untouched.
"""

import frappe


def execute():
	if frappe.db.exists("DocType", "iPay Settings"):
		frappe.db.set_single_value("iPay Settings", "enabled", 0)

	if frappe.db.has_column("Purchase Invoice", "ipay_status"):
		frappe.db.sql(
			"""update `tabPurchase Invoice`
			set ipay_error = %s
			where ipay_status in ('Initiated', 'Processing')""",
			"Started by version 1 of the iPay app and no longer tracked here. Check this payment in iPay before paying again.",
		)
