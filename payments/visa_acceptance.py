"""Visa Acceptance (CyberSource) sandbox gateway.

Money movement is real Visa API traffic against the SANDBOX (apitest.cybersource.com): every hold is an
authorization, every capture/release/refund is the matching Visa call. No real money moves in sandbox.

What stays Tandem-side (Visa's agentic token / mandate APIs are not public): agent tokens, purchase mandates and
their cap / merchant / expiry checks. Those are inherited from the simulated gateway and enforced BEFORE any Visa call.

Cards: the browser never sends a card number to Tandem (only the last 4 digits for display). Every authorization
uses Visa's published sandbox test card, so any card a demo user types "goes through".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from email.utils import formatdate

import httpx

from payments.gateway import GatewayError
from payments.simulated import SimulatedVisaAgentGateway

log = logging.getLogger(__name__)

SANDBOX_HOST = "apitest.cybersource.com"
TEST_CARD = {"number": "4111111111111111", "expirationMonth": "12", "expirationYear": "2031", "type": "001"}
# Visa's sandbox requires a billing address; this is obviously fictional demo data, never a real person.
TEST_BILL_TO = {"firstName": "Tandem", "lastName": "Demo", "address1": "1 Market St", "locality": "San Francisco",
                "administrativeArea": "CA", "postalCode": "94105", "country": "US", "email": "demo@tandem.example"}
OK_AUTH = {"AUTHORIZED"}
LABEL = "Visa sandbox · test mode"


def configured() -> bool:
    return all(os.getenv(k) for k in ("VISA_MERCHANT_ID", "VISA_KEY_ID", "VISA_SHARED_SECRET"))


def _amount(cents: int, currency: str = "USD") -> dict:
    return {"totalAmount": f"{cents // 100}.{cents % 100:02d}", "currency": currency}


class VisaClient:
    """Minimal signed REST client for the Visa Acceptance API (HTTP Signature auth, HmacSHA256)."""

    def __init__(self, merchant_id: str, key_id: str, shared_secret_b64: str, host: str = SANDBOX_HOST, http=None):
        self.merchant_id, self.key_id, self.host = merchant_id, key_id, host
        self.secret = base64.b64decode(shared_secret_b64)
        self.http = http or httpx.Client(base_url=f"https://{host}", timeout=float(os.getenv("VISA_TIMEOUT_S", "15")))

    @classmethod
    def from_env(cls) -> "VisaClient":
        if not configured():
            raise GatewayError("visa_not_configured", "Set VISA_MERCHANT_ID, VISA_KEY_ID and VISA_SHARED_SECRET (sandbox).", 503)
        return cls(os.environ["VISA_MERCHANT_ID"], os.environ["VISA_KEY_ID"], os.environ["VISA_SHARED_SECRET"],
                   os.getenv("VISA_HOST", SANDBOX_HOST))

    def headers(self, method: str, path: str, body: bytes, date: str | None = None) -> dict:
        date = date or formatdate(usegmt=True)
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        signing = "\n".join([f"host: {self.host}", f"date: {date}", f"(request-target): {method.lower()} {path}",
                             f"digest: {digest}", f"v-c-merchant-id: {self.merchant_id}"])
        signature = base64.b64encode(hmac.new(self.secret, signing.encode(), hashlib.sha256).digest()).decode()
        return {"host": self.host, "date": date, "digest": digest, "v-c-merchant-id": self.merchant_id,
                "content-type": "application/json",
                "signature": f'keyid="{self.key_id}", algorithm="HmacSHA256", '
                             f'headers="host date (request-target) digest v-c-merchant-id", signature="{signature}"'}

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            r = self.http.post(path, content=body, headers=self.headers("POST", path, body))
        except httpx.HTTPError:
            log.warning("Visa sandbox request failed: %s", path, exc_info=True)
            raise GatewayError("gateway_unavailable", "Visa sandbox did not respond", 503)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            reason = (data.get("reason") or data.get("errorInformation", {}).get("reason") or f"http_{r.status_code}")
            log.warning("Visa sandbox %s -> HTTP %s %s", path, r.status_code, reason)
            raise GatewayError("visa_" + str(reason).lower(), data.get("message") or reason, 502 if r.status_code >= 500 else 402)
        return data


class VisaAcceptanceGateway(SimulatedVisaAgentGateway):
    """Agent tokens + mandates enforced locally; holds, captures, releases and refunds go to Visa's sandbox."""

    _default: "VisaAcceptanceGateway | None" = None

    def __init__(self, client: VisaClient | None = None, **kw):
        super().__init__(**kw)
        self.client = client or VisaClient.from_env()

    @classmethod
    def default(cls) -> "VisaAcceptanceGateway":
        if cls._default is None:
            from core.events import log_event_simulated
            cls._default = cls(on_event=log_event_simulated)
        cls._default.hydrate_from_db()
        return cls._default

    def _resp(self, event_type: str, body: dict) -> dict:
        out = {"simulated": False, "sandbox": True, "provider": "visa_acceptance_sandbox", **body}
        out.setdefault("request_id", self._id("req"))
        self.on_event(event_type, out)
        return out

    def _id(self, prefix: str) -> str:
        return super()._id(prefix)                                    # Tandem-side ids (tokens, mandates)

    # ---------- money: real Visa sandbox calls ----------
    def authorize_hold(self, mandate_id, amount, idempotency_key, member_id=""):
        def run():
            m = self.mandates.get(mandate_id)
            if not m or m["status"] != "active":
                return self._resp("gateway.hold_rejected", {"status": "REJECTED", "reason": "mandate_inactive"})
            from datetime import datetime, timezone
            if datetime.fromisoformat(m["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return self._resp("gateway.hold_rejected", {"status": "REJECTED", "reason": "mandate_expired"})
            if amount.amount_cents > m["cap"]:
                return self._resp("gateway.hold_rejected", {"status": "REJECTED", "reason": "cap_exceeded",
                                                            "detail": f"{amount.amount_cents} > cap {m['cap']}"})
            data = self.client.post("/pts/v2/payments", {
                "clientReferenceInformation": {"code": idempotency_key[:50]},
                "processingInformation": {"capture": False},
                "paymentInformation": {"card": TEST_CARD},
                "orderInformation": {"amountDetails": _amount(amount.amount_cents, amount.currency), "billTo": TEST_BILL_TO}})
            status = str(data.get("status", "")).upper()
            if status not in OK_AUTH or not data.get("id"):
                reason = (data.get("errorInformation") or {}).get("reason") or status.lower() or "declined"
                return self._resp("gateway.hold_declined", {"status": "DECLINED", "decline_code": str(reason).lower(),
                                                            "visa_status": status})
            hid = data["id"]
            self.holds[hid] = {"mandate": mandate_id, "amount": amount.amount_cents, "status": "AUTHORIZED"}
            return self._resp("gateway.hold_authorized", {
                "hold_id": hid, "status": "AUTHORIZED", "amount": amount.as_dict(), "visa_status": status,
                "expires_at": (datetime.now(timezone.utc) + self.hold_ttl).isoformat().replace("+00:00", "Z"),
                "last4": self.tokens[m["token"]]["last4"], "mandate_remaining_cents": m["cap"] - amount.amount_cents})
        return self._idempotent(idempotency_key, {"m": mandate_id, "a": amount.amount_cents}, run)

    def adjust_hold(self, hold_id, new_amount, idempotency_key):
        """Visa sandbox incremental auth isn't universally available, so: reverse the old hold, authorize anew.
        The response carries the NEW hold_id; the rules engine stores it."""
        def run():
            h = self.holds[hold_id]
            m = self.mandates[h["mandate"]]
            if h["status"] != "AUTHORIZED":
                raise GatewayError("hold_not_active", hold_id, 409)
            if new_amount.amount_cents > m["cap"]:
                return self._resp("gateway.hold_rejected", {"hold_id": hold_id, "status": "REJECTED", "reason": "cap_exceeded"})
            self._reverse(hold_id, h["amount"], "amount_changed", idempotency_key + ":rev")
            h["status"] = "RELEASED"
            fresh = self.authorize_hold(h["mandate"], new_amount, idempotency_key + ":auth")
            if fresh["status"] != "AUTHORIZED":
                return fresh
            return self._resp("gateway.hold_adjusted", {"hold_id": fresh["hold_id"], "previous_hold_id": hold_id,
                                                        "status": "AUTHORIZED", "amount": new_amount.as_dict(),
                                                        "previous_amount_cents": h["amount"]})
        return self._idempotent(idempotency_key, {"h": hold_id, "a": new_amount.amount_cents}, run)

    def capture(self, hold_id, amount, idempotency_key):
        def run():
            h = self.holds[hold_id]
            if h["status"] != "AUTHORIZED":
                raise GatewayError("hold_not_active", hold_id, 409)
            if amount.amount_cents > h["amount"]:
                raise GatewayError("capture_exceeds_hold", f"{amount.amount_cents} > {h['amount']}")
            data = self.client.post(f"/pts/v2/payments/{hold_id}/captures", {
                "clientReferenceInformation": {"code": idempotency_key[:50]},
                "orderInformation": {"amountDetails": _amount(amount.amount_cents, amount.currency)}})
            h["status"] = "CAPTURED"
            m = self.mandates[h["mandate"]]
            m["status"] = "consumed"
            cid = data.get("id") or self._id("cap")
            self.captures[cid] = {"hold": hold_id, "amount": amount.amount_cents, "refunded": 0}
            return self._resp("gateway.captured", {"capture_id": cid, "hold_id": hold_id, "status": "CAPTURED",
                                                   "visa_status": data.get("status"), "amount": amount.as_dict(),
                                                   "merchant_id": m["merchant_id"], "mandate_status": "consumed"})
        return self._idempotent(idempotency_key, {"h": hold_id, "a": amount.amount_cents}, run)

    def _reverse(self, hold_id, cents, reason, code):
        return self.client.post(f"/pts/v2/payments/{hold_id}/reversals", {
            "clientReferenceInformation": {"code": code[:50]},
            "reversalInformation": {"amountDetails": {"totalAmount": _amount(cents)["totalAmount"]}, "reason": reason[:30]}})

    def release_hold(self, hold_id, reason, idempotency_key):
        def run():
            h = self.holds[hold_id]
            if h["status"] != "AUTHORIZED":
                raise GatewayError("hold_not_active", hold_id, 409)
            data = self._reverse(hold_id, h["amount"], reason, idempotency_key)
            h["status"] = "RELEASED"
            return self._resp("gateway.hold_released", {"hold_id": hold_id, "status": "RELEASED", "visa_status": data.get("status"),
                                                        "released_cents": h["amount"], "reason": reason})
        return self._idempotent(idempotency_key, {"h": hold_id, "r": reason}, run)

    def refund(self, capture_id, amount, idempotency_key):
        def run():
            c = self.captures[capture_id]
            if c["refunded"] + amount.amount_cents > c["amount"]:
                raise GatewayError("refund_exceeds_capture", capture_id)
            data = self.client.post(f"/pts/v2/captures/{capture_id}/refunds", {
                "clientReferenceInformation": {"code": idempotency_key[:50]},
                "orderInformation": {"amountDetails": _amount(amount.amount_cents, amount.currency)}})
            c["refunded"] += amount.amount_cents
            return self._resp("gateway.refunded", {"refund_id": data.get("id") or self._id("rf"), "capture_id": capture_id,
                                                   "status": "REFUNDED", "visa_status": data.get("status"), "amount": amount.as_dict()})
        return self._idempotent(idempotency_key, {"c": capture_id, "a": amount.amount_cents}, run)

    def list_offers(self, agent_token_id, merchant_id):
        return {"simulated": False, "offers": []}
