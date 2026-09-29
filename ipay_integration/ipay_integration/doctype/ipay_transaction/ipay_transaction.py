# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class iPayTransaction(Document):
	"""One row per payment intent against a Purchase Invoice.

	This is the idempotency and audit ledger for iPay payouts. The unique
	idempotency_key is the x-idempotency-key sent to iPay; every retry of the
	same intent reuses this row (attempts increments) and the same key, so a
	retry can never become a second payment. All writes happen in
	ipay_integration.ipay - staff only ever read these rows.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		attempts: DF.Int
		bank_account_no: DF.Data | None
		bank_code: DF.Data | None
		company: DF.Link | None
		currency: DF.Link | None
		error: DF.SmallText | None
		idempotency_key: DF.Data
		ipay_reference: DF.Data | None
		last_checked_at: DF.Datetime | None
		payment_account: DF.Link | None
		payment_entry: DF.Link | None
		purchase_invoice: DF.Link
		request_payload: DF.Code | None
		response_payload: DF.Code | None
		settled_at: DF.Datetime | None
		status: DF.Literal["Initiated", "Processing", "Successful", "Failed", "Reversed"]
		supplier: DF.Link | None

	# end: auto-generated types

	pass
