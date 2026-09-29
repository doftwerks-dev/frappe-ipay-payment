# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
import requests

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

import ipay_integration.ipay as ipay


class FakeResponse:
	def __init__(self, status_code, body):
		self.status_code = status_code
		self._body = body
		self.text = frappe.as_json(body)
		self.ok = status_code < 400

	def json(self):
		if self._body is None:
			raise ValueError("no json body")
		return self._body


class AccountRow:
	"""Stands in for an iPay Account child row without touching the DB."""

	company = "Test Co"

	def __init__(self, client_id="client-test"):
		self.client_id = client_id

	def get_password(self, fieldname):
		return "secret-test"


class FakeTxn:
	"""Stands in for an iPay Transaction doc; db_set just updates attributes."""

	def __init__(self, status="Initiated", name="IPAY-TXN-TEST"):
		self.name = name
		self.status = status
		self.writes = {}

	def db_set(self, fieldname, value, update_modified=True):
		setattr(self, fieldname, value)
		self.writes[fieldname] = value


SETTINGS = frappe._dict(base_url="https://sandbox.invalid")


class TestTokenExchange(FrappeTestCase):
	def test_success_returns_token_and_expiry(self):
		body = {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
		with patch.object(ipay.requests, "post", return_value=FakeResponse(200, body)) as post:
			token, expires_in = ipay._request_token(SETTINGS.base_url, "cid", "csec")

		self.assertEqual(token, "tok-1")
		self.assertEqual(expires_in, 3600)
		kwargs = post.call_args.kwargs
		self.assertEqual(kwargs["auth"], ("cid", "csec"))
		# exactly "client_credentials" - no stray leading space (Postman bug)
		self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
		self.assertEqual(kwargs["data"]["scope"], ipay.TOKEN_SCOPES)
		self.assertEqual(post.call_args.args[0], "https://sandbox.invalid/oauth/token")

	def test_rejected_credentials_raise_auth_error(self):
		body = {"error": "invalid_client", "error_description": "Client authentication failed"}
		with patch.object(ipay.requests, "post", return_value=FakeResponse(401, body)):
			with self.assertRaises(ipay.IPayAuthError) as ctx:
				ipay._request_token(SETTINGS.base_url, "cid", "wrong")
		self.assertIn("Client authentication failed", str(ctx.exception))

	def test_ok_response_without_token_raises_auth_error(self):
		with patch.object(ipay.requests, "post", return_value=FakeResponse(200, {"message": "odd"})):
			self.assertRaises(ipay.IPayAuthError, ipay._request_token, SETTINGS.base_url, "cid", "csec")

	def test_network_failure_raises_unreachable(self):
		with patch.object(ipay.requests, "post", side_effect=requests.exceptions.ConnectionError):
			self.assertRaises(
				ipay.IPayUnreachableError, ipay._request_token, SETTINGS.base_url, "cid", "csec"
			)

	def test_token_is_cached_until_forced(self):
		row = AccountRow(client_id=f"cache-{frappe.generate_hash(length=8)}")
		with patch.object(ipay, "_request_token", return_value=("tok-cached", 3600)) as fetch:
			first = ipay.get_access_token(SETTINGS, row)
			second = ipay.get_access_token(SETTINGS, row)
			self.assertEqual(first, "tok-cached")
			self.assertEqual(second, "tok-cached")
			self.assertEqual(fetch.call_count, 1)

			ipay.get_access_token(SETTINGS, row, force=True)
			self.assertEqual(fetch.call_count, 2)


class TestHeaders(FrappeTestCase):
	def test_content_type_on_every_request_including_gets(self):
		# a missing Content-Type returns 415, even on GETs
		headers = ipay._api_headers(AccountRow(), "tok")
		self.assertEqual(headers["Content-Type"], "application/json")
		self.assertEqual(headers["x-client-id"], "client-test")
		self.assertEqual(headers["Authorization"], "Bearer tok")

	def test_write_headers_carry_timestamp_and_idempotency_key(self):
		headers = ipay._write_headers(AccountRow(), "tok", "key-123")
		self.assertEqual(headers["x-idempotency-key"], "key-123")
		self.assertTrue(headers["x-timestamp"].isdigit())  # unix seconds
		self.assertEqual(headers["Content-Type"], "application/json")


class TestWalletBalance(FrappeTestCase):
	def test_parses_balance(self):
		body = {"code": 0, "success": True, "message": "Wallet Balance: 1250.5", "data": {"balance": 1250.5}}
		with patch.object(ipay.requests, "get", return_value=FakeResponse(200, body)) as get:
			balance = ipay.fetch_wallet_balance(SETTINGS, AccountRow(), token="tok")
		self.assertEqual(balance, 1250.5)
		self.assertEqual(get.call_args.kwargs["headers"]["Content-Type"], "application/json")

	def test_zero_balance_is_valid(self):
		body = {"code": 0, "success": True, "data": {"balance": 0}}
		with patch.object(ipay.requests, "get", return_value=FakeResponse(200, body)):
			self.assertEqual(ipay.fetch_wallet_balance(SETTINGS, AccountRow(), token="tok"), 0.0)

	def test_error_response_raises(self):
		with patch.object(ipay.requests, "get", return_value=FakeResponse(500, {"message": "server busy"})):
			with self.assertRaises(ipay.IPayError) as ctx:
				ipay.fetch_wallet_balance(SETTINGS, AccountRow(), token="tok")
		self.assertEqual(str(ctx.exception), ipay.OFFLINE_MESSAGE)

	def test_revoked_cached_token_retries_once_fresh(self):
		ok_body = {"code": 0, "success": True, "data": {"balance": 10}}
		responses = [FakeResponse(401, {"errorMessage": "Unauthenticated"}), FakeResponse(200, ok_body)]
		with (
			patch.object(ipay, "get_access_token", side_effect=["stale", "fresh"]) as tokens,
			patch.object(ipay.requests, "get", side_effect=responses),
		):
			balance = ipay.fetch_wallet_balance(SETTINGS, AccountRow())

		self.assertEqual(balance, 10.0)
		self.assertEqual(tokens.call_count, 2)
		self.assertTrue(tokens.call_args.kwargs.get("force"))


class TestCheckStatus(FrappeTestCase):
	def test_found_transfer_returns_status_and_reference(self):
		body = {"code": 0, "success": True, "data": {"status": "Successful", "reference": "qck-trsf_9"}}
		with patch.object(ipay.requests, "get", return_value=FakeResponse(200, body)) as get:
			result = ipay.check_transfer_status(SETTINGS, AccountRow(), "key-1", token="tok")

		self.assertTrue(result.found)
		self.assertEqual(result.status, "Successful")
		self.assertEqual(result.reference, "qck-trsf_9")
		self.assertIn("/api/v1/transactions/check-status/key-1", get.call_args.args[0])
		self.assertEqual(get.call_args.kwargs["headers"]["Content-Type"], "application/json")
		# lookups need no timestamp/idempotency header
		self.assertNotIn("x-idempotency-key", get.call_args.kwargs["headers"])

	def test_404_means_no_transfer_for_that_key(self):
		body = {"code": 404, "success": False, "errorMessage": "Not found", "data": []}
		with patch.object(ipay.requests, "get", return_value=FakeResponse(404, body)):
			result = ipay.check_transfer_status(SETTINGS, AccountRow(), "key-x", token="tok")
		self.assertFalse(result.found)

	def test_error_surfaces_error_message(self):
		body = {"code": 403, "success": False, "errorMessage": "Token missing scope", "data": []}
		with patch.object(ipay.requests, "get", return_value=FakeResponse(403, body)):
			with self.assertRaises(ipay.IPayError) as ctx:
				ipay.check_transfer_status(SETTINGS, AccountRow(), "key-x", token="tok")
		self.assertIn("scope", str(ctx.exception).lower())


class TestResponseParsing(FrappeTestCase):
	def test_error_message_prefers_errorMessage_over_message(self):
		body = {"errorMessage": "the real reason", "message": "misleading"}
		self.assertEqual(ipay._error_message(body, FakeResponse(422, body)), "the real reason")

	def test_error_message_falls_back_to_message_then_http(self):
		self.assertEqual(ipay._error_message({"message": "hm"}, FakeResponse(400, {})), "hm")
		self.assertEqual(ipay._error_message({}, FakeResponse(400, {})), "HTTP 400")

	def test_successful_transfer(self):
		body = {
			"code": 0,
			"success": True,
			"message": "Transfer Successful",
			"data": {"status": "Successful", "reference": "qck-trsf_1"},
		}
		result = ipay._interpret_transfer_response(FakeResponse(200, body))
		self.assertEqual(result.outcome, "successful")
		self.assertEqual(result.reference, "qck-trsf_1")

	def test_processing_is_success_not_failure(self):
		body = {
			"code": 0,
			"success": True,
			"message": "Transfer Processing",
			"data": {"status": "Processing", "reference": "qck-trsf_2"},
		}
		result = ipay._interpret_transfer_response(FakeResponse(200, body))
		self.assertEqual(result.outcome, "processing")

	def test_409_duplicate_means_still_processing(self):
		body = {"code": 409, "success": False, "errorMessage": "Duplicate transfer", "data": []}
		result = ipay._interpret_transfer_response(FakeResponse(409, body))
		self.assertEqual(result.outcome, "processing")

	def test_502_asks_for_retry_with_same_key(self):
		body = {"code": 502, "success": False, "errorMessage": "Gateway error", "data": []}
		result = ipay._interpret_transfer_response(FakeResponse(502, body))
		self.assertEqual(result.outcome, "retry")
		self.assertEqual(result.error, ipay.GATEWAY_RETRY_MESSAGE)

	def test_422_surfaces_error_message(self):
		body = {
			"code": 422,
			"success": False,
			"errorMessage": "Business must be verified before initiating transfers",
			"data": [],
		}
		result = ipay._interpret_transfer_response(FakeResponse(422, body))
		self.assertEqual(result.outcome, "failed")
		self.assertIn("verif", result.error.lower())

	def test_success_flag_wins_over_field_presence(self):
		# no reference in data, but success true and not Processing -> settled
		body = {"code": 0, "success": True, "message": "ok", "data": {"status": "Successful"}}
		result = ipay._interpret_transfer_response(FakeResponse(200, body))
		self.assertEqual(result.outcome, "successful")


class TestForwardOnlyStatus(FrappeTestCase):
	def test_successful_never_downgrades_to_failed(self):
		txn = FakeTxn(status="Successful")
		self.assertFalse(ipay._apply_remote_status(txn, "Failed"))
		self.assertEqual(txn.status, "Successful")

	def test_processing_advances_to_successful(self):
		txn = FakeTxn(status="Processing")
		self.assertTrue(ipay._apply_remote_status(txn, "Successful"))
		self.assertEqual(txn.status, "Successful")

	def test_failed_can_still_settle_after_retry(self):
		# a retry with the same idempotency key is linked to the original
		txn = FakeTxn(status="Failed")
		self.assertTrue(ipay._apply_remote_status(txn, "Successful"))
		self.assertEqual(txn.status, "Successful")

	def test_successful_can_reverse(self):
		txn = FakeTxn(status="Successful")
		self.assertTrue(ipay._apply_remote_status(txn, "Reversed"))
		self.assertEqual(txn.status, "Reversed")


class TestTransferBody(FrappeTestCase):
	def test_builds_confirmed_contract_shape(self):
		doc = frappe._dict(
			name="ACC-PINV-2026-00001",
			supplier="SUP-0001",
			currency="NGN",
			outstanding_amount=5000.456,
		)
		ctx = {
			"bank_account_no": "0127257894",
			"bank_code": "058",
			"row": frappe._dict(payment_account="iPay - DTC"),
		}
		body = ipay._build_transfer_body(doc, ctx)
		self.assertEqual(body["senderErp"], "erpnext")
		self.assertEqual(body["vendorId"], "SUP-0001")
		self.assertEqual(body["bankAccountNumber"], "0127257894")
		self.assertEqual(body["bankCode"], "058")
		self.assertEqual(body["vendorPaymentBankId"], "iPay - DTC")
		self.assertEqual(body["currency"], "NGN")
		self.assertEqual(body["amount"], 5000.46)  # rounded to 2dp, major units
		self.assertIn("ACC-PINV-2026-00001", body["description"])
		# the idempotency key travels as a header, never in the body
		self.assertNotIn("idempotencyKey", body)


class TestPreflight(FrappeTestCase):
	def test_collects_all_errors_at_once(self):
		doc = frappe._dict(
			name="PINV-TEST",
			docstatus=1,
			company="No Such Co",
			supplier="No Such Supplier",
			currency="USD",
			outstanding_amount=100,
		)
		settings = frappe._dict(enabled=0, base_url="https://sandbox.invalid", accounts=[])
		with patch.object(ipay, "_supplier_bank_details", return_value=None):
			errors, _ctx = ipay._preflight(doc, settings)

		joined = "\n".join(errors)
		self.assertEqual(len(errors), 4)  # disabled + currency + no account row + no bank
		self.assertIn("disabled", joined)
		self.assertIn("NGN only", joined)
		self.assertIn("iPay Account row", joined)
		self.assertIn("bank details", joined)

	def test_clean_bill_passes_without_a_balance_gate(self):
		row = frappe._dict(company="Test Co", client_id="c", payment_account="iPay - TC")
		doc = frappe._dict(
			name="PINV-TEST-3",
			docstatus=1,
			company="Test Co",
			supplier="SUP-0001",
			currency="NGN",
			outstanding_amount=5000,
		)
		settings = frappe._dict(enabled=1, base_url="https://sandbox.invalid", accounts=[row])
		# no `bank` link -> _bank_code falls back to branch_code, no DB access
		bank = frappe._dict(name="BA-1", bank=None, bank_account_no="0127257894", branch_code="058")
		with (
			patch.object(ipay, "_supplier_bank_details", return_value=bank),
			patch.object(ipay, "get_access_token", return_value="tok"),
			patch.object(ipay, "fetch_wallet_balance") as balance,
		):
			errors, ctx = ipay._preflight(doc, settings)

		self.assertEqual(errors, [])
		self.assertEqual(ctx["bank_account_no"], "0127257894")
		self.assertEqual(ctx["bank_code"], "058")
		self.assertEqual(ctx["token"], "tok")
		# the wallet balance is indicative only - never consulted as a spend gate
		balance.assert_not_called()

	def test_missing_bank_code_is_reported(self):
		row = frappe._dict(company="Test Co", client_id="c", payment_account="iPay - TC")
		doc = frappe._dict(
			name="PINV-TEST-4",
			docstatus=1,
			company="Test Co",
			supplier="SUP-0001",
			currency="NGN",
			outstanding_amount=5000,
		)
		settings = frappe._dict(enabled=1, base_url="https://sandbox.invalid", accounts=[row])
		bank = frappe._dict(name="BA-1", bank=None, bank_account_no="0127257894", branch_code=None)
		with patch.object(ipay, "_supplier_bank_details", return_value=bank):
			errors, _ctx = ipay._preflight(doc, settings)
		self.assertEqual(len(errors), 1)
		self.assertIn("NIP bank code", errors[0])


class TestFriendlyErrors(FrappeTestCase):
	def test_credential_needles(self):
		self.assertEqual(ipay._friendly_error("invalid_client"), ipay.INVALID_CREDENTIALS_MESSAGE)
		self.assertEqual(ipay._friendly_error("Unauthenticated."), ipay.INVALID_CREDENTIALS_MESSAGE)
		# the sandbox's actual OAuth rejection wording (seen in the e2e smoke test)
		self.assertEqual(
			ipay._friendly_error("Client authentication failed"), ipay.INVALID_CREDENTIALS_MESSAGE
		)

	def test_scope_outranks_credentials(self):
		# "invalid_scope" must match before the generic credential needles
		self.assertIn("scopes", ipay._friendly_error("invalid_scope: not provisioned"))

	def test_account_state_needles(self):
		# 422s that are account state, not bugs - shown plainly with guidance
		self.assertIn("verification", ipay._friendly_error("Business must be verified"))
		self.assertIn("Top up", ipay._friendly_error("Insufficient wallet balance"))
		self.assertIn("provisioning", ipay._friendly_error("No wallet or gateway configured"))

	def test_network_needles(self):
		self.assertEqual(ipay._friendly_error("Connection refused"), ipay.OFFLINE_MESSAGE)
		self.assertEqual(ipay._friendly_error("Request timed out"), ipay.OFFLINE_MESSAGE)

	def test_unknown_message_passes_through(self):
		self.assertEqual(ipay._friendly_error("Completely novel failure"), "Completely novel failure")
