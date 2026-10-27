# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class iPaySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from ipay_integration.ipay_integration.doctype.ipay_account.ipay_account import iPayAccount

		accounts: DF.Table[iPayAccount]
		base_url: DF.Data | None
		enabled: DF.Check

	# end: auto-generated types

	def validate(self):
		seen = set()
		for row in self.accounts:
			if row.company in seen:
				frappe.throw(
					frappe._("Company {0} has more than one iPay Account row. Keep one row per company.").format(
						row.company
					)
				)
			seen.add(row.company)
			if row.payment_account and frappe.db.get_value("Account", row.payment_account, "company") != row.company:
				frappe.throw(
					frappe._("Payment Account {0} does not belong to company {1}.").format(row.payment_account, row.company)
				)

		# provision the per-company ledger plumbing (Bank, "iPay" account,
		# company Bank Account, Mode of Payment) and link payment_account
		from ipay_integration.ipay import ensure_payment_setup

		ensure_payment_setup(self)
