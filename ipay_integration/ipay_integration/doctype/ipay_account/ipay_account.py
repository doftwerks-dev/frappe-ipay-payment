# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class iPayAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		client_id: DF.Data
		client_secret: DF.Password
		company: DF.Link
		parent: DF.Data
		payment_account: DF.Link | None
		parentfield: DF.Data
		parenttype: DF.Data

	# end: auto-generated types

	pass
