import json
import pathlib
import time

import pytest

from ipay_plugin_kit import CallbackReceiver, EnvelopeError, IpayClient, IpayError, KeySet, NotFound, PluginConfig, Refused, envelope

V = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text())


def keys(name):
    k = V[name]
    return KeySet.from_base64(k["kid"], k["enc"], k["sign"])


REQUEST, CALLBACK = keys("request_keys"), keys("callback_keys")
CLIENT, TS, NONCE = V["client_id"], V["timestamp"], V["nonce"]


# ---- compatibility with iPay's own PHP implementation ---------------------------------------------------------------

def test_opens_a_request_sealed_by_ipays_php_envelope():
    payload = envelope.open_({REQUEST.kid: REQUEST}, CLIENT, "POST", V["post"]["target"], V["post"]["headers"], V["post"]["body"], now=TS)
    assert payload == {"bill_id": "B-1", "amount": "500.00", "note": "a/b ₦"}


def test_verifies_a_bodyless_request_signed_by_php():
    nonce = envelope.verify_only({REQUEST.kid: REQUEST}, CLIENT, "GET", V["get"]["target"], V["get"]["headers"], "", now=TS)
    assert nonce == NONCE


def test_opens_an_answer_sealed_by_php_and_bound_to_the_nonce():
    payload = envelope.open_({CALLBACK.kid: CALLBACK}, CLIENT, envelope.RESPONSE, V["response"]["target"], V["response"]["headers"], V["response"]["body"], expect_nonce=NONCE, now=TS)
    assert payload["payment"]["status"] == "awaiting_confirm"


def test_what_we_seal_is_byte_for_byte_what_php_would_sign():
    sealed = envelope.seal(REQUEST, CLIENT, "POST", V["post"]["target"], {"x": 1}, nonce=NONCE, timestamp=TS)
    php_style = envelope.canonical("POST", V["post"]["target"], TS, NONCE, REQUEST.kid, sealed["body"])
    assert php_style.count("\n") == 5
    # PHP's own signature over PHP's own body must match ours over the same bytes.
    ours = envelope._signature(REQUEST, "POST", V["post"]["target"], TS, NONCE, V["post"]["body"])
    assert ours == V["post"]["headers"]["x-ipay-signature"]


# ---- refusals --------------------------------------------------------------------------------------------------------

def _php_post(**override):
    h = dict(V["post"]["headers"])
    h.update(override)
    return h


@pytest.mark.parametrize("override,reason", [
    ({"x-client-id": "someone-else"}, "unknown_client"),
    ({"x-ipay-key-id": "key-9"}, "unknown_key"),
    ({"x-timestamp": str(TS - 4000)}, "stale_timestamp"),
    ({"x-ipay-nonce": "short"}, "bad_nonce"),
    ({"x-ipay-signature": "AAAA"}, "bad_signature"),
])
def test_bad_headers_are_refused(override, reason):
    with pytest.raises(EnvelopeError) as e:
        envelope.open_({REQUEST.kid: REQUEST}, CLIENT, "POST", V["post"]["target"], _php_post(**override), V["post"]["body"], now=TS)
    assert e.value.reason == reason


def test_a_changed_body_a_changed_target_and_a_changed_method_are_refused():
    k = {REQUEST.kid: REQUEST}
    with pytest.raises(EnvelopeError):
        envelope.open_(k, CLIENT, "POST", V["post"]["target"], V["post"]["headers"], V["post"]["body"].replace("ct", "cx", 1), now=TS)
    with pytest.raises(EnvelopeError):
        envelope.open_(k, CLIENT, "POST", "/erp-plugin/v1/other", V["post"]["headers"], V["post"]["body"], now=TS)
    with pytest.raises(EnvelopeError):
        envelope.open_(k, CLIENT, "PUT", V["post"]["target"], V["post"]["headers"], V["post"]["body"], now=TS)


def test_a_replayed_nonce_is_refused_after_the_signature_checks_out():
    seen = set()

    def claim(kid, nonce):
        if (kid, nonce) in seen:
            return False
        seen.add((kid, nonce))
        return True

    args = ({REQUEST.kid: REQUEST}, CLIENT, "POST", V["post"]["target"], V["post"]["headers"], V["post"]["body"])
    envelope.open_(*args, claim_nonce=claim, now=TS)
    with pytest.raises(EnvelopeError) as e:
        envelope.open_(*args, claim_nonce=claim, now=TS)
    assert e.value.reason == "replayed_nonce"


def test_an_unsigned_message_cannot_use_up_a_nonce():
    seen = []
    with pytest.raises(EnvelopeError):
        envelope.open_({REQUEST.kid: REQUEST}, CLIENT, "POST", V["post"]["target"], _php_post(**{"x-ipay-signature": "AAAA"}), V["post"]["body"], claim_nonce=lambda k, n: seen.append(n) or True, now=TS)
    assert seen == []


def test_an_answer_for_another_request_is_refused():
    with pytest.raises(EnvelopeError) as e:
        envelope.open_({CALLBACK.kid: CALLBACK}, CLIENT, envelope.RESPONSE, V["response"]["target"], V["response"]["headers"], V["response"]["body"], expect_nonce="BBBBBBBBBBBBBBBBBBBBBB", now=TS)
    assert e.value.reason == "nonce_mismatch"


def test_keys_must_be_32_bytes():
    with pytest.raises(EnvelopeError):
        KeySet.from_base64("k", "AAAA", "AAAA")


def test_keys_are_not_in_their_repr():
    assert "AQEB" not in repr(REQUEST) and "\x01" not in repr(REQUEST)


# ---- the client, against a fake iPay ---------------------------------------------------------------------------------

class Resp:
    def __init__(self, status, body="", headers=None, data=None):
        self.status_code, self.text, self.headers, self._data = status, body, headers or {}, data

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class FakeIpay:
    """Answers like iPay: a token, then sealed answers under the callback keys (iPay's side of the envelope)."""

    def __init__(self, answer=None, status=200, fail_first_with_401=False):
        self.answer, self.status, self.calls, self.tokens = answer or {"ok": True}, status, [], 0
        self.fail_first = fail_first_with_401

    def post(self, url, auth=None, data=None, headers=None, timeout=None):
        assert url.endswith("/oauth/token") and data["grant_type"] == "client_credentials"
        self.tokens += 1
        return Resp(200, data={"access_token": f"tok-{self.tokens}", "expires_in": 3600})

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, headers))
        if self.fail_first and len(self.calls) == 1:
            return Resp(401, "{}", {})
        target = url.split("://", 1)[1].split("/", 1)[1]
        target = "/" + target
        nonce = headers["x-ipay-nonce"]
        sealed = envelope.seal(CALLBACK, CLIENT, envelope.RESPONSE, target, {**self.answer, "nonce": nonce}, nonce=nonce)
        return Resp(self.status, sealed["body"], sealed["headers"])


def config():
    return PluginConfig("https://ipay.example", CLIENT, "secret", REQUEST, CALLBACK)


def test_the_client_asks_for_a_payment_and_returns_iPays_answer():
    fake = FakeIpay({"payment": {"id": "p1", "status": "awaiting_confirm"}, "confirm_url": "https://ipay.example/erp-payments/confirm#t=abc"})
    out = IpayClient(config(), fake).request_payment("odoo:1:5", "INV/1", "500.00", "ada@acme.test", "7", "Acme", "058", "0123456789")
    assert out["confirm_url"].endswith("#t=abc") and "nonce" not in out
    method, url, headers = fake.calls[0]
    assert (method, url) == ("POST", "https://ipay.example/erp-plugin/v1/payments")
    assert headers["Authorization"] == "Bearer tok-1" and headers["x-client-id"] == CLIENT


def test_a_401_gets_one_fresh_token_and_a_new_nonce():
    fake = FakeIpay({"pong": True}, fail_first_with_401=True)
    assert IpayClient(config(), fake).ping() == {"pong": True}
    assert fake.tokens == 2
    assert fake.calls[0][2]["x-ipay-nonce"] != fake.calls[1][2]["x-ipay-nonce"]


def test_an_error_answer_raises_with_iPays_code():
    fake = FakeIpay({"error": {"code": "requester_not_allowed", "message": "Not allowed"}}, status=403)
    with pytest.raises(IpayError) as e:
        IpayClient(config(), fake).payment("p1")
    assert (e.value.code, e.value.status) == ("requester_not_allowed", 403)


def test_an_answer_that_cannot_be_verified_is_never_trusted():
    class Forger(FakeIpay):
        def request(self, method, url, headers=None, data=None, timeout=None):
            sealed = envelope.seal(REQUEST, CLIENT, envelope.RESPONSE, "/erp-plugin/v1/ping", {"nonce": headers["x-ipay-nonce"], "pong": True}, nonce=headers["x-ipay-nonce"])
            return Resp(200, sealed["body"], sealed["headers"])  # sealed with the wrong keys

    with pytest.raises(IpayError):
        IpayClient(config(), Forger()).ping()


def test_the_token_is_reused_until_close_to_expiry():
    fake = FakeIpay({"a": 1})
    c = IpayClient(config(), fake)
    c.ping()
    c.ping()
    assert fake.tokens == 1


def test_iPay_must_be_reached_over_https():
    with pytest.raises(IpayError):
        PluginConfig("http://ipay.example", CLIENT, "s", REQUEST, CALLBACK)


# ---- the receiver: iPay calling the plugin ----------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.seen = set()

    def claim(self, kid, nonce):
        if (kid, nonce) in self.seen:
            return False
        self.seen.add((kid, nonce))
        return True


def receiver(store=None, **handlers):
    store = store or Store()
    h = {
        "bill": lambda bill_id: {"id": bill_id, "status": "open"} if bill_id == "B-1" else (_ for _ in ()).throw(NotFound()),
        "record_payment": lambda p: {"outcome": "recorded", "erp_payment_id": "PAY-1"},
        "payment_event": lambda p: None,
    }
    h.update(handlers)
    return CallbackReceiver(CLIENT, REQUEST, [CALLBACK], store.claim, base_path="/ipay", **h)


def ipay_get(target, nonce=None):
    s = envelope.sign_only(CALLBACK, CLIENT, "GET", target, nonce=nonce)
    return s["headers"], s["nonce"]


def ipay_post(target, payload):
    s = envelope.seal(CALLBACK, CLIENT, "POST", target, payload)
    return s["headers"], s["body"], s["nonce"]


def opened(reply, target, nonce):
    status, headers, body = reply
    return status, envelope.open_({REQUEST.kid: REQUEST}, CLIENT, envelope.RESPONSE, target, headers, body, expect_nonce=nonce)


def test_a_bill_is_read_and_answered_sealed_with_the_request_keys():
    target = "/ipay/bills/B-1"
    headers, nonce = ipay_get(target)
    status, payload = opened(receiver().handle("GET", target, headers, ""), target, nonce)
    assert status == 200 and payload["bill"] == {"id": "B-1", "status": "open"}


def test_an_unknown_bill_is_a_sealed_not_found():
    target = "/ipay/bills/NOPE"
    headers, nonce = ipay_get(target)
    status, payload = opened(receiver().handle("GET", target, headers, ""), target, nonce)
    assert status == 404 and payload["error"]["code"] == "not_found"


def test_a_payment_is_recorded_once_per_signed_call():
    target = "/ipay/payments"
    seen = []
    r = receiver(record_payment=lambda p: seen.append(p) or {"outcome": "recorded", "erp_payment_id": "PAY-1"})
    headers, body, nonce = ipay_post(target, {"bill_id": "B-1", "amount": "500.00", "currency": "NGN", "reference": "r1"})
    status, payload = opened(r.handle("POST", target, headers, body), target, nonce)
    assert status == 200 and payload["outcome"] == "recorded" and len(seen) == 1
    # the same call replayed is refused and records nothing
    assert r.handle("POST", target, headers, body)[0] == 401 and len(seen) == 1


def test_a_call_signed_with_the_wrong_keys_gets_a_plain_401_and_reaches_no_handler():
    target = "/ipay/payments"
    called = []
    r = receiver(record_payment=lambda p: called.append(p))
    s = envelope.seal(REQUEST, CLIENT, "POST", target, {"bill_id": "B-1", "amount": "1", "currency": "NGN", "reference": "x"})  # request keys, not callback keys
    status, headers, body = r.handle("POST", target, s["headers"], s["body"])
    assert status == 401 and called == [] and json.loads(body) == {"error": "unauthenticated"}


def test_missing_fields_are_refused_without_calling_the_handler():
    target = "/ipay/payments"
    called = []
    r = receiver(record_payment=lambda p: called.append(p))
    headers, body, nonce = ipay_post(target, {"bill_id": "B-1"})
    status, payload = opened(r.handle("POST", target, headers, body), target, nonce)
    assert status == 422 and called == []


def test_a_handler_failure_is_a_5xx_that_never_carries_its_cause():
    target = "/ipay/payments"
    r = receiver(record_payment=lambda p: 1 / 0)
    headers, body, nonce = ipay_post(target, {"bill_id": "B-1", "amount": "1", "currency": "NGN", "reference": "x"})
    status, payload = opened(r.handle("POST", target, headers, body), target, nonce)
    assert status == 500 and "zero" not in json.dumps(payload)


def test_events_are_acknowledged_and_other_paths_are_not_found():
    target = "/ipay/payment-events"
    got = []
    r = receiver(payment_event=lambda p: got.append(p))
    headers, body, nonce = ipay_post(target, {"event_id": "p1:recorded", "event": "payment.recorded", "payment": {}})
    status, payload = opened(r.handle("POST", target, headers, body), target, nonce)
    assert status == 200 and got[0]["event_id"] == "p1:recorded"
    assert receiver().handle("GET", "/elsewhere/bills/B-1", {}, "")[0] == 404
    assert receiver().handle("DELETE", "/ipay/payments", {}, "")[0] == 404
