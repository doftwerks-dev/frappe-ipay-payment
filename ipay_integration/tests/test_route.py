# Copyright (c) 2026, Doftwerks West Africa Limited and contributors
# For license information, please see license.txt

"""iPay's calls go through Frappe's whole web stack: a guest request to /ipay/plugin/... reaches the page renderer (hooks.py,
page_renderer), is verified, handled and answered sealed. The requests are made against the real WSGI application, the way
Frappe's own API tests do, so routing, sessions and CSRF are Frappe's, not mocked."""

import json
import os
from unittest.mock import patch

import frappe
from frappe.tests.test_api import make_request
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_test_client

from ipay_integration import ipay
from ipay_integration.ipay_plugin_kit import KeySet, envelope

CLIENT = "route-test-client"
# Throwaway keys made for this test run only.
REQUEST = KeySet("route-key", os.urandom(32), os.urandom(32))
CALLBACK = KeySet("route-key", os.urandom(32), os.urandom(32))


class TestPluginRoute(FrappeTestCase):
	def setUp(self):
		self.client = get_test_client(use_cookies=False)

	def _registered(self):
		"""This site as iPay's registered plugin client, without saving keys: the request runs in another thread, so the
		settings are faked at the module level the renderer reads them from."""
		settings = frappe._dict(enabled=1, accounts=[frappe._dict(company="Route Test Co", client_id=CLIENT)])
		get_single = frappe.get_single

		def fake_get_single(doctype, *args, **kwargs):
			return settings if doctype == ipay.SETTINGS_DOCTYPE else get_single(doctype, *args, **kwargs)

		return (
			patch.object(frappe, "get_single", side_effect=fake_get_single),
			patch.object(ipay, "callback_keys_for", return_value=(frappe._dict(request_keys=REQUEST), [CALLBACK])),
		)

	def _post(self, path, headers, body):
		return make_request(
			target=self.client.post,
			args=(path,),
			kwargs={"data": body, "headers": headers, "content_type": "application/json"},
		)

	@staticmethod
	def _open_answer(response, path, nonce):
		return envelope.open_(
			{REQUEST.kid: REQUEST},
			CLIENT,
			envelope.RESPONSE,
			path,
			dict(response.headers.items()),
			response.get_data(as_text=True),
			expect_nonce=nonce,
		)

	def test_an_unsigned_guest_post_reaches_the_renderer_and_is_refused(self):
		response = self._post("/ipay/plugin/payment-events", {}, "{}")
		self.assertEqual(response.status_code, 401)
		self.assertEqual(json.loads(response.get_data(as_text=True)), {"error": "unauthenticated"})

	def test_a_signed_guest_post_is_verified_handled_and_answered_sealed(self):
		path = "/ipay/plugin/payment-events"
		event = {
			"event_id": "pay_route:processing",
			"event": "payment.processing",
			"external_reference": "not-a-reference-of-this-site",
			"occurred_at": "2026-11-10T09:00:00Z",
			"payment": {"id": "pay_route", "status": "processing"},
		}
		sealed = envelope.seal(CALLBACK, CLIENT, "POST", path, event)
		settings_patch, keys_patch = self._registered()
		with settings_patch, keys_patch:
			response = self._post(path, sealed["headers"], sealed["body"])
			replay = self._post(path, sealed["headers"], sealed["body"])

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(self._open_answer(response, path, sealed["nonce"]).get("received"), True)
		self.assertEqual(replay.status_code, 401, "the same signed call is never accepted twice")

	def test_a_signed_guest_get_for_an_unknown_bill_is_a_sealed_not_found(self):
		path = "/ipay/plugin/bills/NO-SUCH-BILL"
		signed = envelope.sign_only(CALLBACK, CLIENT, "GET", path)
		settings_patch, keys_patch = self._registered()
		with settings_patch, keys_patch:
			response = make_request(target=self.client.get, args=(path,), kwargs={"headers": signed["headers"]})

		self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
		self.assertEqual(self._open_answer(response, path, signed["nonce"])["error"]["code"], "not_found")
