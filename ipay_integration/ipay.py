# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay payout integration - the server-side HTTP layer and payment flow.

All iPay API access lives in this one module (mirrors doftwerks_nrs/einvoice.py).
Implements the confirmed ERP Payment API contract:

- OAuth2 client-credentials token (Basic auth + form body), cached ~55 min.
- Content-Type: application/json on EVERY request, GETs included (else 415).
- Write requests additionally carry x-timestamp (unix seconds, <5 min skew)
  and x-idempotency-key (ONE UUID per payment intent, reused on retry).
- Responses are parsed by the `success` flag; `message` on success,
  `errorMessage` on failure.
- "Processing" is acceptance, NOT settlement: the Payment Entry is only
  created once check-status reports Successful (enqueued poll + cron net).

The idempotency/audit ledger is the iPay Transaction doctype - one row per
payment intent, unique on idempotency_key.
"""

import re
import time
import uuid

import frappe
import requests
from frappe.utils import add_to_date, cint, cstr, flt, fmt_money, now, now_datetime, nowdate

SETTINGS_DOCTYPE = "iPay Settings"
TRANSACTION_DOCTYPE = "iPay Transaction"
REQUEST_TIMEOUT = 30

# Names of the ledger plumbing auto-provisioned per company (the Zoho
# parallel: an "iPay" bank added to the books so payments are "Paid Through" it)
IPAY_BANK_NAME = "iPay"
MODE_OF_PAYMENT = "iPay"

TOKEN_SCOPES = "transfers:create transfers:status transfers:walletBalance"

# Ask for a fresh token this many seconds before the current one expires.
TOKEN_REFRESH_MARGIN = 60

# iPay Transaction statuses still awaiting a settlement verdict from iPay.
PENDING_STATUSES = ("Initiated", "Processing")

# Forward-only status ladder: a replayed or out-of-order event must never
# downgrade a record. Failed -> Successful stays allowed (a retry with the
# same idempotency key is linked to the original and can still settle).
STATUS_RANK = {"Initiated": 0, "Processing": 1, "Failed": 2, "Successful": 3, "Reversed": 4}

# iPay Transaction status -> Purchase Invoice ipay_status
INVOICE_STATUS = {
	"Initiated": "Initiated",
	"Processing": "Processing",
	"Successful": "Paid",
	"Failed": "Failed",
	"Reversed": "Reversed",
}

PAYER_ROLES = ("System Manager", "Accounts Manager")

OFFLINE_MESSAGE = (
	"iPay could not be reached. Check the Base URL in iPay Settings and your "
	"network connection, then retry."
)

INVALID_CREDENTIALS_MESSAGE = (
	"iPay rejected the credentials. Check the Client ID and Client Secret in "
	"iPay Settings - they come from the iPay dashboard's API settings."
)

GATEWAY_RETRY_MESSAGE = (
	"iPay's banking gateway reported a temporary fault (HTTP 502). Retry in a "
	"moment - the same payment key is reused, so retrying cannot double-pay."
)

PROCESSING_MESSAGE = (
	"iPay accepted the transfer and it is settling. The Payment Entry will be "
	"recorded automatically once iPay confirms the payout."
)

UNCONFIRMED_MESSAGE = (
	"The transfer was sent but iPay's answer did not arrive. Its status will "
	"be checked automatically; retrying later is also safe - the same payment "
	"key is reused."
)

# (regex needle searched in the platform's error message, plain guidance) - matched top to
# bottom, so keep specific needles before general ones, and use word boundaries
# for short needles.
FRIENDLY_ERRORS = [
	(
		r"business.*verif|verif.*business",
		"iPay reports the business is not verified yet. Complete verification in "
		"the iPaySuite dashboard - there is nothing to fix in ERPNext.",
	),
	(
		"insufficient",
		"The iPay wallet balance is too low for this payment. Top up the wallet "
		"in iPaySuite, then retry.",
	),
	(
		r"no wallet|gateway configured",
		"The iPay account has no wallet or payment gateway configured yet. "
		"Contact iPay support to finish provisioning the account.",
	),
	(
		"invalid_scope",
		"iPay rejected the requested API scopes. These credentials may not be "
		"provisioned for transfers - contact iPay support.",
	),
	("invalid_client", INVALID_CREDENTIALS_MESSAGE),
	("unauthenticated", INVALID_CREDENTIALS_MESSAGE),
	("unauthorized", INVALID_CREDENTIALS_MESSAGE),
	("access denied", INVALID_CREDENTIALS_MESSAGE),
	("forbidden", INVALID_CREDENTIALS_MESSAGE),
	("timed out", OFFLINE_MESSAGE),
	("timeout", OFFLINE_MESSAGE),
	("connection", OFFLINE_MESSAGE),
	("unavailable", OFFLINE_MESSAGE),
	("offline", OFFLINE_MESSAGE),
	("busy", OFFLINE_MESSAGE),
]


class IPayError(Exception):
	"""Base for iPay API failures. str(exc) is safe to show to staff."""


class IPayAuthError(IPayError):
	"""The platform refused the credentials (or issued no token)."""


class IPayUnreachableError(IPayError):
	"""Network-level failure before any platform answer."""


def _logger():
	return frappe.logger("ipay")


def _friendly_error(raw):
	low = cstr(raw).lower()
	for needle, friendly in FRIENDLY_ERRORS:
		if re.search(needle, low):
			return friendly
	return cstr(raw)


def _find_account(settings, company):
	for row in settings.accounts:
		if row.company == company:
			return row
	return None


def _parse_body(response):
	try:
		body = response.json()
	except ValueError:
		body = {}
	return body if isinstance(body, dict) else {}


def _error_message(body, response):
	"""iPay puts `errorMessage` on failures and `message` on successes; some
	proxies answer with neither, so fall back to the HTTP status."""
	return (
		cstr(body.get("errorMessage"))
		or cstr(body.get("message"))
		or f"HTTP {response.status_code}"
	)


# ---------------------------------------------------------------------------
# OAuth token
# ---------------------------------------------------------------------------


def get_access_token(settings, row, force=False):
	"""Bearer token for one account row, cached in Redis until shortly before
	expiry (60 min lifetime, no refresh token). force=True bypasses and
	overwrites the cache."""
	cache_key = f"ipay_token|{cstr(settings.base_url).rstrip('/')}|{cstr(row.client_id)}"
	if not force:
		# expires=True: never memoize a TTL'd key (or its miss) in frappe.local.cache
		cached = frappe.cache().get_value(cache_key, expires=True)
		if cached:
			return cached

	token, expires_in = _request_token(
		settings.base_url, row.client_id, row.get_password("client_secret")
	)
	ttl = max(cint(expires_in) - TOKEN_REFRESH_MARGIN, 30)
	frappe.cache().set_value(cache_key, token, expires_in_sec=ttl)
	return token


def _request_token(base_url, client_id, client_secret):
	"""OAuth2 client-credentials exchange. Returns (access_token, expires_in)."""
	url = f"{cstr(base_url).rstrip('/')}/oauth/token"
	try:
		response = requests.post(
			url,
			auth=(cstr(client_id), cstr(client_secret)),
			# exactly "client_credentials" - the Postman collection carries a
			# stray leading space in this value; do not copy it
			data={"grant_type": "client_credentials", "scope": TOKEN_SCOPES},
			headers={"Accept": "application/json"},
			timeout=REQUEST_TIMEOUT,
		)
	except requests.exceptions.RequestException:
		raise IPayUnreachableError(OFFLINE_MESSAGE)

	body = _parse_body(response)
	token = cstr(body.get("access_token"))
	if token and response.ok:
		_logger().info(
			f"token acquired for client {cstr(client_id)[:12]}, expires_in={body.get('expires_in')}"
		)
		return token, body.get("expires_in")

	detail = (
		cstr(body.get("error_description"))
		or cstr(body.get("error"))
		or _error_message(body, response)
	)
	raise IPayAuthError(detail)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _api_headers(row, token):
	return {
		"Authorization": f"Bearer {token}",
		"x-client-id": cstr(row.client_id),
		# required on EVERY request, GETs included - iPay answers 415 without it
		"Content-Type": "application/json",
		"Accept": "application/json",
	}


def _write_headers(row, token, idempotency_key):
	return {
		**_api_headers(row, token),
		# unix seconds; iPay rejects >5 min of skew, so keep the server clock synced
		"x-timestamp": str(int(time.time())),
		"x-idempotency-key": cstr(idempotency_key),
	}


def fetch_wallet_balance(settings, row, token=None):
	"""Wallet balance in naira. Indicative only - it reports settled funds and
	ignores in-flight transfers, so it is shown to staff but never used as a
	spend gate; a true shortfall comes back as a 422 on initiate."""
	if token is None:
		token = get_access_token(settings, row)
		response, body = _wallet_request(settings, row, token)
		if response.status_code == 401:
			# a cached token can be revoked server-side: one fresh retry
			token = get_access_token(settings, row, force=True)
			response, body = _wallet_request(settings, row, token)
	else:
		response, body = _wallet_request(settings, row, token)

	data = body.get("data") if isinstance(body.get("data"), dict) else {}
	if response.ok and body.get("success") and data.get("balance") is not None:
		return flt(data.get("balance"))

	raise IPayError(_friendly_error(_error_message(body, response)))


def _wallet_request(settings, row, token):
	url = f"{cstr(settings.base_url).rstrip('/')}/api/v1/transactions/wallet-balance"
	try:
		response = requests.get(url, headers=_api_headers(row, token), timeout=REQUEST_TIMEOUT)
	except requests.exceptions.RequestException:
		raise IPayUnreachableError(OFFLINE_MESSAGE)
	return response, _parse_body(response)


def check_transfer_status(settings, row, idempotency_key, token=None):
	"""GET check-status/{key} - lookup by OUR idempotency key, not iPay's
	reference. Returns _dict(found, status, reference, body); found=False on
	404, meaning iPay never received a transfer for this key."""
	if token is None:
		token = get_access_token(settings, row)
	response, body = _status_request(settings, row, token, idempotency_key)
	if response.status_code == 401:
		token = get_access_token(settings, row, force=True)
		response, body = _status_request(settings, row, token, idempotency_key)

	if response.status_code == 404:
		return frappe._dict(found=False, status=None, reference=None, body=body)

	data = body.get("data") if isinstance(body.get("data"), dict) else {}
	if response.ok and body.get("success"):
		return frappe._dict(
			found=True,
			status=cstr(data.get("status")),
			reference=cstr(data.get("reference")),
			body=body,
		)

	raise IPayError(_friendly_error(_error_message(body, response)))


def _status_request(settings, row, token, idempotency_key):
	url = (
		f"{cstr(settings.base_url).rstrip('/')}"
		f"/api/v1/transactions/check-status/{cstr(idempotency_key)}"
	)
	try:
		response = requests.get(url, headers=_api_headers(row, token), timeout=REQUEST_TIMEOUT)
	except requests.exceptions.RequestException:
		raise IPayUnreachableError(OFFLINE_MESSAGE)
	return response, _parse_body(response)


# ---------------------------------------------------------------------------
# Settings-page actions
# ---------------------------------------------------------------------------


@frappe.whitelist()
def test_connection():
	"""Validate each account row's credentials with a fresh token exchange,
	then prove API access by reading the wallet balance."""
	frappe.only_for(PAYER_ROLES)
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	results = []
	for row in settings.accounts:
		try:
			token, _expires_in = _request_token(
				settings.base_url, row.client_id, row.get_password("client_secret")
			)
		except IPayUnreachableError:
			results.append(
				{
					"company": row.company,
					"ok": False,
					"detail": "iPay could not be reached - check Base URL / network",
				}
			)
			continue
		except IPayAuthError as e:
			results.append(
				{
					"company": row.company,
					"ok": False,
					"detail": f"Invalid credentials - check Client ID and Client Secret ({cstr(e)})",
				}
			)
			continue

		try:
			balance = fetch_wallet_balance(settings, row, token=token)
			detail = f"Credentials OK - wallet balance {fmt_money(balance, currency='NGN')}"
		except IPayError:
			detail = "Credentials OK, but the wallet balance could not be read"
		results.append({"company": row.company, "ok": True, "detail": detail})
	return results


@frappe.whitelist()
def wallet_balances():
	"""Balance per configured account row, for the settings-page button."""
	frappe.only_for(PAYER_ROLES)
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	results = []
	for row in settings.accounts:
		try:
			balance = fetch_wallet_balance(settings, row)
			results.append(
				{
					"company": row.company,
					"ok": True,
					"balance": balance,
					"formatted": fmt_money(balance, currency="NGN"),
				}
			)
		except IPayError as e:
			results.append({"company": row.company, "ok": False, "detail": _friendly_error(cstr(e))})
	return results


# ---------------------------------------------------------------------------
# Ledger provisioning (runs from iPay Settings validate; idempotent)
# ---------------------------------------------------------------------------


def ensure_payment_setup(settings):
	"""Create the ledger plumbing each company needs so iPay payouts can be
	recorded as payments: a Bank, a Chart of Accounts account of type Bank
	(the "Paid Through" ledger), a company Bank Account and a Mode of Payment.
	Fills row.payment_account when empty. Idempotent - search first, create
	only when missing, so reconnecting never duplicates anything."""
	for row in settings.accounts:
		if not row.company:
			continue
		account = _ensure_company_account(row.company)
		if not account:
			continue
		if not row.payment_account:
			row.payment_account = account
		_ensure_bank_account(row.company, account)
		_ensure_mode_of_payment(row.company, account)


def _ensure_company_account(company):
	abbr, currency = frappe.get_cached_value("Company", company, ["abbr", "default_currency"])
	name = f"{IPAY_BANK_NAME} - {abbr}"
	if frappe.db.exists("Account", name):
		return name

	parent = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name"
	)
	if not parent:
		frappe.msgprint(
			frappe._(
				"Could not find a Bank group account for {0}, so the iPay ledger account was "
				"not created. Create an Account named iPay (type Bank) manually and set it as "
				"Payment Account on the iPay Settings row."
			).format(company),
			indicator="orange",
		)
		return None

	frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": IPAY_BANK_NAME,
			"company": company,
			"parent_account": parent,
			"account_type": "Bank",
			"account_currency": currency,
		}
	).insert(ignore_permissions=True)
	_logger().info(f"created iPay ledger account {name}")
	return name


def _ensure_bank_account(company, account):
	if not frappe.db.exists("Bank", IPAY_BANK_NAME):
		frappe.get_doc({"doctype": "Bank", "bank_name": IPAY_BANK_NAME}).insert(ignore_permissions=True)

	if frappe.db.exists("Bank Account", {"company": company, "account": account}):
		return
	abbr = frappe.get_cached_value("Company", company, "abbr")
	frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": f"{IPAY_BANK_NAME} Wallet {abbr}",
			"bank": IPAY_BANK_NAME,
			"is_company_account": 1,
			"company": company,
			"account": account,
		}
	).insert(ignore_permissions=True)


def _ensure_mode_of_payment(company, account):
	if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT):
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": MODE_OF_PAYMENT,
				"enabled": 1,
				"type": "Bank",
				"accounts": [{"company": company, "default_account": account}],
			}
		).insert(ignore_permissions=True)
		return

	mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT)
	if not any(r.company == company for r in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": account})
		mop.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Supplier destination bank
# ---------------------------------------------------------------------------


def _supplier_bank_details(supplier):
	"""The supplier's default (else any) active ERPNext Bank Account."""
	filters = {"party_type": "Supplier", "party": supplier, "disabled": 0}
	fields = ["name", "bank", "bank_account_no", "branch_code"]
	bank = frappe.db.get_value("Bank Account", {**filters, "is_default": 1}, fields, as_dict=True)
	return bank or frappe.db.get_value("Bank Account", filters, fields, as_dict=True)


def _bank_code(bank):
	"""NIP code for a supplier Bank Account row: the curated code on the Bank
	master first, the row's own Branch Code as fallback."""
	code = bank.bank and frappe.db.get_value("Bank", bank.bank, "ipay_nip_code")
	return cstr(code or bank.branch_code or "")


@frappe.whitelist()
def save_supplier_bank(supplier, bank, account_number):
	"""Create or update the supplier's default Bank Account from the pay
	dialog. Standard permission checks apply (no ignore_permissions): the
	caller needs Bank Account write rights."""
	frappe.only_for(PAYER_ROLES)
	account_number = cstr(account_number).strip()
	if not re.fullmatch(r"\d{10}", account_number):
		frappe.throw(frappe._("NUBAN account numbers are exactly 10 digits."))
	if not frappe.db.exists("Bank", bank):
		frappe.throw(frappe._("Bank {0} does not exist.").format(bank))

	existing = _supplier_bank_details(supplier)
	if existing:
		if existing.bank == bank and existing.bank_account_no == account_number:
			return existing.name
		doc = frappe.get_doc("Bank Account", existing.name)
		doc.bank = bank
		doc.bank_account_no = account_number
		doc.save()
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier,
			"bank": bank,
			"party_type": "Supplier",
			"party": supplier,
			"bank_account_no": account_number,
			"is_default": 1,
		}
	).insert()
	return doc.name


# ---------------------------------------------------------------------------
# AP flow: Pay via iPay on Purchase Invoice
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_payment_context(name):
	"""Everything the pay dialog shows: outstanding, the supplier's saved
	destination bank, the (indicative) wallet balance and any pending state."""
	frappe.only_for(PAYER_ROLES)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("read")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	row = _find_account(settings, doc.company)

	bank = _supplier_bank_details(doc.supplier)
	context = {
		"enabled": cint(settings.enabled),
		"configured": bool(row and row.get("payment_account")),
		"outstanding": flt(doc.outstanding_amount),
		"currency": cstr(doc.currency),
		"supplier": cstr(doc.supplier),
		"supplier_name": cstr(doc.supplier_name or doc.supplier),
		"bank": None,
		"wallet": None,
		"transaction": None,
	}
	if bank:
		context["bank"] = {
			"name": bank.name,
			"bank": cstr(bank.bank),
			"bank_account_no": cstr(bank.bank_account_no),
			"bank_code": _bank_code(bank),
		}

	txn = _latest_transaction(doc.name)
	if txn:
		context["transaction"] = {
			"name": txn.name,
			"status": txn.status,
			"reference": cstr(txn.ipay_reference),
			"payment_entry": cstr(txn.payment_entry),
		}

	if row:
		try:
			balance = fetch_wallet_balance(settings, row)
			context["wallet"] = {
				"balance": balance,
				"formatted": fmt_money(balance, currency="NGN"),
			}
		except IPayError as e:
			context["wallet"] = {"error": cstr(e)}

	return context


@frappe.whitelist()
def pay_invoice(name):
	"""'Pay via iPay' entrypoint: pre-flight, initiate the transfer with the
	invoice's stored idempotency key, then record the Payment Entry - but only
	once the transfer is Successful. Processing settles via background polling."""
	frappe.only_for(PAYER_ROLES)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("write")
	settings = frappe.get_single(SETTINGS_DOCTYPE)

	txn_head = _latest_transaction(doc.name)
	txn = frappe.get_doc(TRANSACTION_DOCTYPE, txn_head.name) if txn_head else None

	# legacy guard: invoices paid by earlier builds carry only ipay_reference
	if not txn and doc.get("ipay_reference"):
		return {"ok": True, "already_paid": True, "reference": doc.ipay_reference}

	if txn and txn.status == "Successful":
		if txn.payment_entry:
			return {"ok": True, "already_paid": True, "reference": txn.ipay_reference,
				"payment_entry": txn.payment_entry}
		# ordering guard: the money already moved - never re-initiate, only
		# re-record the missing Payment Entry
		return _settle_success(txn, txn.ipay_reference)

	if txn and txn.status == "Processing":
		# a transfer with this key is in flight on iPay's side - re-check, never re-send
		_enqueue_poll(txn.name)
		return {"ok": True, "processing": True, "message": PROCESSING_MESSAGE}

	if txn and txn.status == "Initiated":
		# an earlier attempt got no clear answer (502/timeout). Ask iPay before
		# deciding: 404 means no transfer exists for our key, so re-sending the
		# SAME key is safe; anything else adopts iPay's verdict.
		row = _find_account(settings, doc.company)
		if row:
			try:
				result = check_transfer_status(settings, row, txn.idempotency_key)
			except IPayError:
				_enqueue_poll(txn.name)
				return {"ok": True, "processing": True, "message": UNCONFIRMED_MESSAGE}
			if result.found:
				if result.status == "Successful":
					return _settle_success(txn, result.reference, body=result.body)
				if result.status not in ("Failed", "Reversed"):
					_apply_remote_status(txn, "Processing", body=result.body)
					_sync_invoice(doc.name, "Processing")
					_enqueue_poll(txn.name)
					return {"ok": True, "processing": True, "message": PROCESSING_MESSAGE}
				# Failed/Reversed: funds released back - fall through and
				# re-initiate with the same key (iPay links the retry)
				_apply_remote_status(txn, result.status, body=result.body)

	errors, ctx = _preflight(doc, settings)
	if errors:
		doc.db_set("ipay_error", "\n".join(errors), update_modified=False)
		return {"ok": False, "errors": errors}

	# ONE idempotency key per payment intent, generated once, stored on the
	# invoice and reused verbatim on every retry. A fresh key on a retry would
	# be a second, separate payment.
	key = cstr(doc.get("ipay_idempotency_key")) or (txn and cstr(txn.idempotency_key)) or str(uuid.uuid4())
	if cstr(doc.get("ipay_idempotency_key")) != key:
		doc.db_set("ipay_idempotency_key", key, update_modified=False)

	body = _build_transfer_body(doc, ctx)
	txn = _get_or_create_transaction(doc, ctx, key, body, existing=txn)

	# Deliberate manual commit (not in a document hook): the idempotency key
	# and attempt row MUST survive even if this request later fails, because
	# the POST below can move real money. Without this, a crash after the POST
	# would lose the only handle we have on the transfer.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	try:
		response = _post_initiate(settings, ctx, body, key)
		if response.status_code == 401:
			# a cached token can be revoked server-side: one fresh retry,
			# same idempotency key so it cannot double-send
			ctx["token"] = get_access_token(settings, ctx["row"], force=True)
			response = _post_initiate(settings, ctx, body, key)
	except IPayUnreachableError:
		# Ambiguous: iPay may or may not have received the transfer. Never
		# assume failure - check-status with our key settles it (404 = never
		# landed, so the poll marks it Failed and retry stays safe).
		frappe.log_error(title=f"iPay unreachable for {doc.name}", message=frappe.get_traceback())
		_write_txn(txn, error=UNCONFIRMED_MESSAGE)
		_sync_invoice(doc.name, "Initiated", error=UNCONFIRMED_MESSAGE)
		_enqueue_poll(txn.name)
		return {"ok": True, "processing": True, "message": UNCONFIRMED_MESSAGE}

	return _handle_transfer_response(doc, txn, response)


def _post_initiate(settings, ctx, body, idempotency_key):
	url = f"{cstr(settings.base_url).rstrip('/')}/api/v1/transactions/initiate"
	headers = _write_headers(ctx["row"], ctx["token"], idempotency_key)
	try:
		return requests.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
	except requests.exceptions.RequestException:
		raise IPayUnreachableError(OFFLINE_MESSAGE)


def _preflight(doc, settings):
	"""Collect ALL problems at once - one error per attempt is a terrible
	staff experience. Returns (errors, context-for-the-transfer).

	Wallet balance is deliberately NOT checked here: it is indicative only
	(ignores in-flight transfers), so we initiate and surface iPay's own 422
	if the wallet truly cannot cover the payment."""
	errors = []
	ctx = {}

	if not settings.enabled:
		errors.append("iPay integration is disabled. Enable it in iPay Settings.")
	if doc.docstatus != 1:
		errors.append("The bill must be submitted before it can be paid.")
	if flt(doc.outstanding_amount) <= 0:
		errors.append("This bill has no outstanding amount - it is already paid.")
	if cstr(doc.currency) != "NGN":
		errors.append(f"iPay payouts support NGN only at launch; this bill is in {doc.currency}.")

	row = _find_account(settings, doc.company)
	ctx["row"] = row
	if not row:
		errors.append(f"Company {doc.company} has no iPay Account row in iPay Settings.")
	elif not row.get("payment_account"):
		errors.append(
			"The company's iPay ledger account is missing. Open iPay Settings and Save to provision it."
		)

	bank = _supplier_bank_details(doc.supplier)
	if not bank:
		errors.append(
			f"Supplier {doc.supplier} has no bank details on file. Add the destination "
			"bank and account number in the Pay via iPay dialog."
		)
	else:
		if not bank.bank_account_no:
			errors.append(f"Bank Account {bank.name} has no Bank Account No.")
		code = _bank_code(bank)
		if not code:
			errors.append(
				f"No NIP bank code found for {bank.bank or bank.name}. Set 'NIP Bank Code' on "
				"the Bank record (or Branch Code on the supplier's Bank Account)."
			)
		ctx["bank_account_no"] = cstr(bank.bank_account_no)
		ctx["bank_code"] = code

	if row and not errors:
		try:
			ctx["token"] = get_access_token(settings, row)
		except IPayError as e:
			errors.append(_friendly_error(cstr(e)))

	return errors, ctx


def _build_transfer_body(doc, ctx):
	return {
		"senderErp": "erpnext",
		"vendorId": cstr(doc.supplier),
		"bankAccountNumber": cstr(ctx["bank_account_no"]),
		"bankCode": cstr(ctx["bank_code"]),
		# the provisioned "iPay" ledger account - the same account the Payment
		# Entry is paid from
		"vendorPaymentBankId": cstr(ctx["row"].payment_account),
		"currency": cstr(doc.currency),
		"amount": flt(doc.outstanding_amount, 2),
		"description": f"Payment for {doc.name}",
		# extra context the Zoho widget also sent; harmless if iPay ignores it
		"reference": doc.name,
		"erpBillId": doc.name,
	}


def _interpret_transfer_response(response):
	"""Map an initiate response to one of four outcomes, per the contract:
	success flag true -> successful or processing (data.status);
	409 -> a transfer with this key is still processing (wait, re-check);
	502 -> gateway-side fault, retry with the SAME key;
	anything else -> failed, with errorMessage surfaced."""
	body = _parse_body(response)
	data = body.get("data") if isinstance(body.get("data"), dict) else {}
	reference = cstr(data.get("reference"))

	if response.ok and body.get("success"):
		status = cstr(data.get("status")) or "Successful"
		outcome = "processing" if status == "Processing" else "successful"
		return frappe._dict(outcome=outcome, reference=reference, body=body, error=None)

	if response.status_code == 409:
		return frappe._dict(outcome="processing", reference=reference, body=body, error=None)

	if response.status_code == 502:
		return frappe._dict(outcome="retry", reference=None, body=body, error=GATEWAY_RETRY_MESSAGE)

	error = _friendly_error(_error_message(body, response))
	return frappe._dict(outcome="failed", reference=None, body=body, error=error)


def _handle_transfer_response(doc, txn, response):
	result = _interpret_transfer_response(response)

	if result.outcome == "successful":
		return _settle_success(txn, result.reference, body=result.body)

	if result.outcome == "processing":
		_apply_remote_status(txn, "Processing", body=result.body, ipay_reference=result.reference or None)
		_sync_invoice(doc.name, "Processing", reference=result.reference or None, error="")
		_enqueue_poll(txn.name)
		return {"ok": True, "processing": True, "message": PROCESSING_MESSAGE}

	if result.outcome == "retry":
		# stays Initiated; the user can retry now, and the cron poller will
		# mark it Failed if check-status says iPay never received it
		_write_txn(txn, error=result.error, response_payload=frappe.as_json(result.body, indent=2))
		_sync_invoice(doc.name, "Initiated", error=result.error)
		_enqueue_poll(txn.name)
		return {"ok": False, "errors": [result.error], "retryable": True}

	_apply_remote_status(txn, "Failed", body=result.body, error=result.error)
	_sync_invoice(doc.name, "Failed", error=result.error)
	frappe.log_error(
		title=f"iPay transfer failed for {doc.name}",
		message=f"HTTP {response.status_code}\n{cstr(response.text)[:5000]}",
	)
	return {"ok": False, "errors": [result.error]}


# ---------------------------------------------------------------------------
# Settlement, polling and the Payment Entry
# ---------------------------------------------------------------------------


def _settle_success(txn, reference, body=None):
	"""The transfer is settled: record it, then create the Payment Entry.
	Ordering guard: the transfer result is persisted BEFORE the Payment Entry
	attempt, and a Payment Entry failure never rolls back or re-runs the
	transfer - a later run only re-records the ledger entry."""
	reference = cstr(reference or txn.ipay_reference)
	_apply_remote_status(
		txn, "Successful", body=body, ipay_reference=reference or None,
		settled_at=txn.get("settled_at") or now(),
	)
	_sync_invoice(txn.purchase_invoice, "Paid", reference=reference or None, paid_at=now(), error="")

	result = {"ok": True, "reference": reference}
	if txn.payment_entry:
		result["payment_entry"] = txn.payment_entry
		return result

	if flt(frappe.db.get_value("Purchase Invoice", txn.purchase_invoice, "outstanding_amount")) <= 0:
		# someone already recorded a payment (e.g. manually, after the PE
		# warning) - nothing left to allocate, and definitely no second PE
		return result

	try:
		pe = _record_payment(txn, reference)
	except Exception:
		frappe.log_error(
			title=f"iPay Payment Entry failed for {txn.purchase_invoice}",
			message=frappe.get_traceback(),
		)
		manual = (
			f"The iPay transfer succeeded (reference {reference}) but the Payment Entry "
			"could not be created. Click 'Record iPay Payment' on the bill to record it - "
			"the transfer itself will NOT run again."
		)
		_write_txn(txn, error=manual)
		_sync_invoice(txn.purchase_invoice, error=manual)
		_notify(txn, f"iPay paid {txn.purchase_invoice} but the Payment Entry failed - action needed")
		result["warning"] = manual
		return result

	_write_txn(txn, payment_entry=pe, error="")
	_notify(txn, f"{txn.purchase_invoice} paid via iPay (ref {reference})")
	result["payment_entry"] = pe
	_logger().info(f"{txn.purchase_invoice}: settled via iPay, reference={reference}, pe={pe}")
	return result


def _record_payment(txn, reference):
	"""ERPNext's 'Record Payment': a submitted Payment Entry paid from the
	company's iPay ledger account. GL entries come from the standard helper -
	never written directly."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	invoice = frappe.get_doc("Purchase Invoice", txn.purchase_invoice)
	amount = min(flt(txn.amount), flt(invoice.outstanding_amount))
	pe = get_payment_entry(
		"Purchase Invoice", txn.purchase_invoice, party_amount=amount, bank_account=txn.payment_account
	)
	pe.mode_of_payment = MODE_OF_PAYMENT
	pe.paid_from = txn.payment_account
	pe.reference_no = reference or txn.idempotency_key
	pe.reference_date = nowdate()
	pe.custom_remarks = 1
	pe.remarks = frappe._("Paid via iPay wallet. iPay reference: {0}").format(reference or "-")
	# ignore_permissions: the initiator's write permission was checked at pay
	# time; settlement may land from a background worker with no user session
	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name


def _enqueue_poll(transaction):
	"""Background settlement poll for one transaction (deduplicated)."""
	if frappe.flags.in_test:
		return
	frappe.enqueue(
		"ipay_integration.ipay.poll_transfer",
		transaction=transaction,
		queue="long",
		job_id=f"ipay-poll-{transaction}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def poll_transfer(transaction, attempts=4, wait=20):
	"""Enqueued job: poll check-status until the transfer settles or attempts
	run out; the cron safety net (poll_pending_transfers) picks up the rest."""
	for attempt in range(cint(attempts) or 1):
		if attempt:
			time.sleep(cint(wait) or 20)
		txn = frappe.get_doc(TRANSACTION_DOCTYPE, transaction)
		if txn.status not in PENDING_STATUSES:
			return
		try:
			if _check_status_once(txn):
				return
		except IPayError:
			# transient (network/token) - the next attempt or the cron retries
			frappe.log_error(
				title=f"iPay status check failed for {transaction}", message=frappe.get_traceback()
			)


def poll_pending_transfers():
	"""Cron safety net: settle any transaction stuck in Initiated/Processing
	(missed job, worker restart, iPay slow to settle)."""
	cutoff = add_to_date(now_datetime(), minutes=-3)
	names = frappe.get_all(
		TRANSACTION_DOCTYPE,
		filters={"status": ["in", PENDING_STATUSES], "modified": ["<", cutoff]},
		pluck="name",
	)
	for name in names:
		txn = frappe.get_doc(TRANSACTION_DOCTYPE, name)
		try:
			_check_status_once(txn)
		except Exception:
			frappe.log_error(title=f"iPay status check failed for {name}", message=frappe.get_traceback())


def _check_status_once(txn):
	"""One check-status round trip for a pending transaction. Returns True
	when a final state was reached (nothing left to poll)."""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	row = _find_account(settings, txn.company)
	if not row:
		_write_txn(txn, error=f"No iPay Account row for {txn.company} - cannot check status.")
		return True

	result = check_transfer_status(settings, row, txn.idempotency_key)
	_write_txn(txn, last_checked_at=now())

	if not result.found:
		# 404: iPay has no transfer for this key. For an Initiated attempt the
		# initiation never landed - mark Failed so the retry button re-sends
		# (same key, so still no double-pay risk).
		if txn.status == "Initiated":
			message = (
				"iPay has no record of this transfer - the initiation never arrived. "
				"Retrying is safe; the same payment key is reused."
			)
			_apply_remote_status(txn, "Failed", error=message)
			_sync_invoice(txn.purchase_invoice, "Failed", error=message)
			_notify(txn, f"iPay payment for {txn.purchase_invoice} did not go through - retry when ready")
			return True
		# Processing but now 404 would be an iPay-side inconsistency: keep
		# polling rather than guessing an outcome.
		return False

	if result.status == "Successful":
		_settle_success(txn, result.reference, body=result.body)
		return True

	if result.status in ("Failed", "Reversed"):
		message = (
			f"iPay reported the transfer as {result.status}. Funds are released back "
			"to the wallet. Retrying is safe; the same payment key is reused."
		)
		_apply_remote_status(txn, result.status, body=result.body, error=message)
		_sync_invoice(txn.purchase_invoice, INVOICE_STATUS[result.status], error=message)
		_notify(txn, f"iPay transfer for {txn.purchase_invoice} was {result.status.lower()}")
		return True

	if txn.status == "Initiated":
		_apply_remote_status(txn, "Processing", body=result.body, ipay_reference=result.reference or None)
		_sync_invoice(txn.purchase_invoice, "Processing")
	return False


@frappe.whitelist()
def refresh_transfer_status(name):
	"""'Check iPay Status' button: one synchronous check-status round trip for
	the invoice's pending transaction; returns the transaction state."""
	frappe.only_for(PAYER_ROLES)
	doc = frappe.get_doc("Purchase Invoice", name)
	doc.check_permission("read")

	head = _latest_transaction(name)
	if not head:
		return {"status": None}
	txn = frappe.get_doc(TRANSACTION_DOCTYPE, head.name)
	if txn.status in PENDING_STATUSES:
		try:
			_check_status_once(txn)
		except IPayError as e:
			return {"status": txn.status, "error": _friendly_error(cstr(e))}
		txn.reload()
	return {
		"status": txn.status,
		"reference": cstr(txn.ipay_reference),
		"payment_entry": cstr(txn.payment_entry),
		"error": cstr(txn.error),
	}


# ---------------------------------------------------------------------------
# iPay Transaction plumbing
# ---------------------------------------------------------------------------


def _latest_transaction(invoice_name):
	rows = frappe.get_all(
		TRANSACTION_DOCTYPE,
		filters={"purchase_invoice": invoice_name},
		fields=["name", "status", "idempotency_key", "ipay_reference", "payment_entry"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _get_or_create_transaction(doc, ctx, key, body, existing=None):
	"""One iPay Transaction per idempotency key. A retry after Failed/Reversed
	reuses the row: attempts increments, the key never changes."""
	if existing and cstr(existing.idempotency_key) != cstr(key):
		# never restart a row under a different key - that would break the
		# one-key-per-intent invariant (data was tampered with; fail loudly)
		frappe.throw(
			frappe._("Idempotency key mismatch between {0} and its iPay Transaction {1}.").format(
				existing.purchase_invoice, existing.name
			)
		)
	name = existing.name if existing else frappe.db.get_value(TRANSACTION_DOCTYPE, {"idempotency_key": key})
	if name:
		txn = existing if existing and existing.name == name else frappe.get_doc(TRANSACTION_DOCTYPE, name)
		# user-driven restart of a failed attempt - the one sanctioned
		# backward move on the status ladder
		_write_txn(
			txn,
			status="Initiated",
			error="",
			attempts=cint(txn.attempts) + 1,
			request_payload=frappe.as_json(body, indent=2),
		)
		return txn

	txn = frappe.get_doc(
		{
			"doctype": TRANSACTION_DOCTYPE,
			"purchase_invoice": doc.name,
			"supplier": doc.supplier,
			"company": doc.company,
			"idempotency_key": key,
			"status": "Initiated",
			"amount": flt(doc.outstanding_amount, 2),
			"currency": doc.currency,
			"bank_account_no": ctx.get("bank_account_no"),
			"bank_code": ctx.get("bank_code"),
			"payment_account": ctx["row"].payment_account,
			"attempts": 1,
			"request_payload": frappe.as_json(body, indent=2),
		}
	)
	# system-managed audit row; the initiator's authority was already checked
	# in pay_invoice (role + invoice write permission)
	txn.insert(ignore_permissions=True)
	return txn


def _write_txn(txn, **values):
	for fieldname, value in values.items():
		txn.db_set(fieldname, value, update_modified=(fieldname != "last_checked_at"))


def _apply_remote_status(txn, status, body=None, **values):
	"""Forward-only status write: a replayed or out-of-order event must never
	downgrade the record (e.g. Successful can never become Failed)."""
	if STATUS_RANK.get(status, 0) < STATUS_RANK.get(txn.status, 0):
		_logger().info(f"{txn.name}: ignored backward status {txn.status} -> {status}")
		return False
	values["status"] = status
	if body is not None:
		values["response_payload"] = frappe.as_json(body, indent=2)
	_write_txn(txn, **{k: v for k, v in values.items() if v is not None})
	return True


def _sync_invoice(invoice, status=None, reference=None, error=None, paid_at=None):
	"""Mirror transaction state onto the invoice's convenience fields."""
	values = {}
	if status is not None:
		values["ipay_status"] = status
	if reference is not None:
		values["ipay_reference"] = reference
	if error is not None:
		values["ipay_error"] = error
	if paid_at is not None:
		values["ipay_paid_at"] = paid_at
	if values:
		frappe.db.set_value("Purchase Invoice", invoice, values, update_modified=False)


def _notify(txn, subject):
	"""Notification Log for the person who initiated the payment. db_set does
	not fire 'Value Change' notifications, so alerts are created directly."""
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": txn.owner,
				"type": "Alert",
				"document_type": "Purchase Invoice",
				"document_name": txn.purchase_invoice,
				"subject": subject,
			}
		).insert(ignore_permissions=True)
	except Exception:
		# alerting must never break settlement
		frappe.log_error(title=f"iPay notification failed for {txn.name}", message=frappe.get_traceback())
