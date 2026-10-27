"""iPay's plugin API, as a plugin calls it (docs/erp-plugin-api.md): sign in, ask for a payment, read where it stands.

A plugin does not move money, decide who may, or record anything on its own authority. It asks iPay to pay a bill, opens the
confirmation link in the payer's browser (the payer's password, finance PIN and emailed code are entered on iPay's page, never
here), and answers iPay's signed calls about the bill. The keys and secrets this needs live in server configuration only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from . import envelope
from .envelope import EnvelopeError, KeySet

SCOPES = "erp_plugin:read erp_plugin:pay"
PREFIX = "/erp-plugin/v1"


class IpayError(Exception):
    """iPay refused or could not be reached. `code` is iPay's error code when it gave one; `status` the HTTP status or 0."""

    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class PluginConfig:
    base_url: str
    client_id: str  # the API client's id, also sent as X-Client-Id
    client_secret: str = field(repr=False)  # for the OAuth token only
    request_keys: KeySet = field(repr=False)  # seals what we send, and what we answer iPay's calls with
    callback_keys: KeySet = field(repr=False)  # opens what iPay sends us, and what iPay answers us with
    previous_callback_keys: KeySet | None = field(default=None, repr=False)

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        # https only. Plain http is allowed for a development machine whose host is exactly localhost or a loopback address, nothing else.
        loopback = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
        if (parts.scheme != "https" and not loopback) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise IpayError("The iPay address must be https")


class IpayClient:
    """`http` is anything with requests' `post` and `request` (a `requests.Session`, or a fake in tests)."""

    def __init__(self, config: PluginConfig, http=None, timeout: int = 20):
        if http is None:
            import requests  # imported here so the envelope and callbacks work without it

            http = requests.Session()
        self.config = config
        self.http = http
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry = 0.0

    # ---- calls ---------------------------------------------------------------------------------------------------

    def ping(self) -> dict:
        return self._get("/ping")

    def banks(self) -> dict:
        return self._get("/banks")

    def resolve_account(self, bank_code: str, account_number: str) -> dict:
        return self._post("/accounts/resolve", {"bank_code": bank_code, "account_number": account_number})

    def request_payment(self, external_reference: str, bill_id: str, amount: str, requested_by_email: str, supplier_id: str, supplier_name: str, bank_code: str, account_number: str, bill_url: str | None = None) -> dict:
        """Asks iPay to pay a bill. Asking again with the same `external_reference` returns the same payment with a fresh `confirm_url`."""
        body = {
            "external_reference": external_reference,
            "bill_id": bill_id,
            "amount": amount,
            "requested_by": {"email": requested_by_email},
            "supplier": {"id": supplier_id, "name": supplier_name},
            "bank": {"bank_code": bank_code, "account_number": account_number},
        }
        if bill_url:
            body["bill_url"] = bill_url
        return self._post("/payments", body)

    def payment(self, payment_id: str) -> dict:
        return self._get("/payments/" + quote(payment_id, safe=""))

    # ---- plumbing -------------------------------------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        target = PREFIX + path
        return self._send("GET", target, lambda: envelope.sign_only(self.config.request_keys, self.config.client_id, "GET", target))

    def _post(self, path: str, payload: dict) -> dict:
        target = PREFIX + path
        return self._send("POST", target, lambda: envelope.seal(self.config.request_keys, self.config.client_id, "POST", target, payload))

    def _send(self, method: str, target: str, make, retried: bool = False) -> dict:
        sealed = make()  # a new nonce every time it is made, so a retry is never a replay
        headers = {**sealed["headers"], "Authorization": "Bearer " + self._access_token(), "Accept": "application/json", "Content-Type": "application/json"}
        try:
            response = self.http.request(method, self.config.base_url + target, headers=headers, data=sealed.get("body"), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise IpayError("iPay could not be reached", status=0) from exc
        if response.status_code == 401 and not retried:
            # The token may have been revoked or have expired early: one fresh token, then the same request sealed again.
            self._token = None
            return self._send(method, target, make, retried=True)
        return self._answer(response, target, sealed["nonce"])

    def _answer(self, response, target: str, nonce: str) -> dict:
        # iPay answers under the callback keys (the ones it also uses to call us); during a rotation, the previous ones.
        keys = {self.config.callback_keys.kid: self.config.callback_keys}
        if self.config.previous_callback_keys is not None:
            keys[self.config.previous_callback_keys.kid] = self.config.previous_callback_keys
        try:
            answer = envelope.open_(keys, self.config.client_id, envelope.RESPONSE, target, response.headers, response.text, expect_nonce=nonce)
        except EnvelopeError as exc:
            # An answer that cannot be verified is never trusted, whatever status it carried.
            raise IpayError("iPay's answer could not be verified", status=response.status_code) from exc
        answer.pop("nonce", None)
        if response.status_code >= 400:
            error = answer.get("error") or {}
            raise IpayError(str(error.get("message") or "iPay refused the request")[:200], str(error.get("code") or ""), response.status_code)
        return answer

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            response = self.http.post(
                self.config.base_url + "/oauth/token",
                auth=(self.config.client_id, self.config.client_secret),
                data={"grant_type": "client_credentials", "scope": SCOPES},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise IpayError("iPay could not be reached") from exc
        data = {}
        try:
            data = response.json()
        except ValueError:
            pass
        if response.status_code != 200 or not data.get("access_token"):
            raise IpayError("iPay did not accept the client id and secret", status=response.status_code)
        self._token = str(data["access_token"])
        self._token_expiry = time.time() + int(data.get("expires_in", 300))
        return self._token
