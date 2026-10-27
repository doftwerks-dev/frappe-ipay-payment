"""What an ERP plugin serves for iPay: read a bill, record a payment, receive payment events (docs/erp-plugin-api.md, "What the plugin must serve").

iPay signs and encrypts every call with the callback keys. This opens it, checks it (client, key id, clock window, signature, a nonce
never seen before), hands the content to the plugin's own handlers and seals the answer with the plugin's request keys, bound to the
nonce of the call. Anything that fails verification gets the same plain 401, with the reason only in the log: the caller learns nothing.

The framework glue (an Odoo controller, a Frappe whitelisted method) passes the raw request in and returns what comes back; nothing
here knows about either.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from . import envelope
from .envelope import EnvelopeError, KeySet

_log = logging.getLogger("ipay_plugin_kit")

Reply = tuple  # (status, headers, body)


class NotFound(Exception):
    """Raised by a handler when the record does not exist."""


class Refused(Exception):
    """Raised by a handler to refuse a request with a message safe to show iPay (422)."""


class CallbackReceiver:
    """
    handlers:
      bill(bill_id) -> {"id","number","supplier_id","supplier_name","currency","total","amount_due","status"}   (status "open" = may be paid)
      record_payment({"bill_id","amount","currency","reference"}) -> {"outcome": "recorded"|"rejected"|"unknown", "erp_payment_id": str|None}
      payment_event({"event_id","event","external_reference","occurred_at","payment"}) -> None
    `claim_nonce(kid, nonce)` must atomically remember the nonce and return False if it was already used (use a shared store).
    """

    def __init__(self, client_id: str, request_keys: KeySet, callback_keys: list[KeySet], claim_nonce: Callable[[str, str], bool], bill: Callable, record_payment: Callable, payment_event: Callable, base_path: str = "", on_failure: Callable | None = None):
        self.client_id = client_id
        self.request_keys = request_keys
        self.callback_keys = {k.kid: k for k in callback_keys}
        self.claim_nonce = claim_nonce
        self.handlers = {"bill": bill, "record_payment": record_payment, "payment_event": payment_event}
        self.base_path = base_path.rstrip("/")
        # Called when a handler fails, so the framework can roll back whatever it half wrote (the nonce was already claimed and kept).
        self.on_failure = on_failure

    def handle(self, method: str, target: str, headers, body: str) -> Reply:
        """`target` is the path plus query string exactly as the web server received it."""
        method = method.upper()
        route = self._route(method, target)
        if route is None:
            return self._plain(404)
        try:
            if method == "GET":
                nonce = envelope.verify_only(self.callback_keys, self.client_id, method, target, headers, body, self.claim_nonce)
                payload: dict = {}
            else:
                payload = envelope.open_(self.callback_keys, self.client_id, method, target, headers, body, self.claim_nonce)
                nonce = _lower(headers).get(envelope.H_NONCE, "")
        except EnvelopeError as exc:
            _log.warning("iPay callback refused: %s", exc.reason)
            return self._plain(401)
        except Exception:  # noqa: BLE001
            # Whatever an unauthenticated caller sends, it is a refusal with nothing to tell it apart, never a server error.
            _log.warning("iPay callback refused: unreadable message")
            return self._plain(401)

        name, arg = route
        try:
            if name == "bill":
                bill = self.handlers["bill"](arg)
                return self._sealed(200, target, nonce, {"bill": bill})
            if name == "record_payment":
                self._require(payload, "bill_id", "amount", "currency", "reference")
                return self._sealed(200, target, nonce, self.handlers["record_payment"](payload))
            self._require(payload, "event_id", "event")
            self.handlers["payment_event"](payload)
            return self._sealed(200, target, nonce, {"received": True})
        except NotFound:
            return self._sealed(404, target, nonce, {"error": {"code": "not_found", "message": "Not found"}})
        except Refused as exc:
            return self._sealed(422, target, nonce, {"error": {"code": "refused", "message": str(exc)[:200]}})
        except Exception:  # noqa: BLE001
            # A server error never carries its cause; iPay retries a 5xx.
            _log.exception("iPay callback failed")
            if self.on_failure is not None:
                try:
                    self.on_failure()
                except Exception:  # noqa: BLE001
                    _log.exception("iPay callback rollback failed")
            return self._sealed(500, target, nonce, {"error": {"code": "server_error", "message": "Something went wrong"}})

    # ---- plumbing -------------------------------------------------------------------------------------------------

    def _route(self, method: str, target: str):
        path = target.split("?", 1)[0]
        if self.base_path:
            if not path.startswith(self.base_path + "/"):
                return None
            path = path[len(self.base_path):]
        if method == "GET":
            m = re.fullmatch(r"/bills/([^/]+)", path)
            return ("bill", _unquote(m.group(1))) if m else None
        if method == "POST" and path == "/payments":
            return ("record_payment", None)
        if method == "POST" and path == "/payment-events":
            return ("payment_event", None)
        return None

    @staticmethod
    def _require(payload: dict, *names: str) -> None:
        missing = [n for n in names if payload.get(n) in (None, "")]
        if missing:
            raise Refused("Missing: " + ", ".join(missing))

    def _sealed(self, status: int, target: str, nonce: str, payload: dict) -> Reply:
        sealed = envelope.seal(self.request_keys, self.client_id, envelope.RESPONSE, target, {**payload, "nonce": nonce}, nonce=nonce)
        return status, {**sealed["headers"], "Content-Type": "application/json"}, sealed["body"]

    @staticmethod
    def _plain(status: int) -> Reply:
        return status, {"Content-Type": "application/json"}, json.dumps({"error": "unauthenticated" if status == 401 else "not_found"})


def _lower(headers) -> dict:
    return envelope._lower(headers)


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)
