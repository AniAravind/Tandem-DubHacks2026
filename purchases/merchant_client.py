"""
How the rules engine talks to the merchant (milestone M4).

Two interchangeable clients, chosen by the MERCHANT_MODE setting:

  "http"    the real thing: signed HTTP requests to MERCHANT_BASE_URL (Rishab's mock merchant).
  "direct"  an in-process stand-in used in tests and until Rishab's merchant is on main. It checks the signature
            and the content digest exactly the way the contract says (section 6.9), so a tampered price is
            rejected with 401 digest_mismatch just like the real merchant would.

Both have one method:  send(slug, action, headers, body) -> (http_status, json_dict)
where `action` is "quotes", "agent/checkout" or "agent/cancel".
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from merchant import signing
from merchant.models import Listing, MerchantBooking, Quote

MAX_WINDOW_S = signing.MAX_WINDOW_S
TAG_FOR_ACTION = {"quotes": "agent-browser-auth", "agent/checkout": "agent-payer-auth", "agent/cancel": "agent-payer-auth"}


def get_merchant_client():
    return HttpMerchantClient() if getattr(settings, "MERCHANT_MODE", "direct") == "http" else DirectMerchantClient()


def _error(status: int, code: str, message: str):
    return status, {"error": {"code": code, "message": message}}


def parse_signature_input(value: str) -> dict:
    """'sig2=("@authority" ...);created=1;expires=2;keyid="k";alg="ed25519";nonce="n";tag="t"' -> a dict."""
    label, _, params = value.partition("=")
    parsed = {"label": label, "params": params, "components": re.findall(r'"([^"]+)"', params.split(")")[0])}
    for key in ("created", "expires"):
        m = re.search(rf"{key}=(\d+)", params)
        parsed[key] = int(m.group(1)) if m else None
    for key in ("keyid", "nonce", "tag"):
        m = re.search(rf'{key}="([^"]*)"', params)
        parsed[key] = m.group(1) if m else None
    return parsed


# ------------------------------------------------------------------------------------------------------
class HttpMerchantClient:
    """Real signed HTTP calls. Uses only the standard library."""

    def send(self, slug: str, action: str, headers: dict, body: bytes):
        url = f"{settings.MERCHANT_BASE_URL.rstrip('/')}/{slug}/{action}"
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, TimeoutError):
            return 503, {"error": {"code": "merchant_unreachable", "message": "Could not reach the merchant."}}


# ------------------------------------------------------------------------------------------------------
class DirectMerchantClient:
    """A small in-process merchant that follows the section 6.9 verification order."""

    def send(self, slug: str, action: str, headers: dict, body: bytes):
        # The signature covers the URL path, so rebuild it the same way the signer did.
        from urllib.parse import urlparse
        base = urlparse(settings.MERCHANT_BASE_URL)
        authority, path = base.netloc, f"{base.path.rstrip('/')}/{slug}/{action}"
        failure = self._verify(authority, path, headers, body, TAG_FOR_ACTION[action])
        if failure:
            return failure
        data = json.loads(body or b"{}")
        if action == "quotes":
            return self._quote(slug, data)
        if action == "agent/checkout":
            return self._checkout(slug, data, headers)
        return self._cancel(slug, data)

    # ---- the verifier (checks 1 to 7 of the table in section 6.9)
    @staticmethod
    def _verify(authority, path, headers, body, expected_tag):
        sig_input, sig = headers.get("Signature-Input"), headers.get("Signature")
        if not sig_input or not sig:
            return _error(401, "signature_missing", "Signature headers are required")
        info = parse_signature_input(sig_input)
        if info["tag"] != expected_tag:
            return _error(401, "wrong_tag", f"Expected tag {expected_tag}")
        now = int(time.time())
        if (info["created"] is None or info["expires"] is None or info["created"] > now + 30
                or now >= info["expires"] or info["expires"] - info["created"] > MAX_WINDOW_S):
            return _error(401, "expired", "The signature is expired or its window is too long")
        if not cache.add(f"nonce:{info['keyid']}:{info['nonce']}", 1, timeout=MAX_WINDOW_S + 60):
            return _error(401, "nonce_replayed", "That nonce was already used")
        if info["keyid"] != signing.KEY_ID:
            return _error(401, "unknown_agent", "Unknown key id")
        digest = headers.get("Content-Digest")
        if expected_tag == "agent-payer-auth" and "content-digest" not in info["components"]:
            return _error(401, "digest_mismatch", "Checkout and cancel require a signed content digest")
        if "content-digest" in info["components"] and digest != signing.content_digest(body):
            return _error(401, "digest_mismatch", "Body does not match signed Content-Digest")
        try:
            public = Ed25519PublicKey.from_public_bytes(base64.b64decode(signing.public_key_b64()))
            raw_sig = base64.b64decode(sig.split(":")[1])
            public.verify(raw_sig, signing.signature_base(authority, path, digest if "content-digest" in info["components"] else None,
                                                          info["params"]))
        except (InvalidSignature, ValueError, IndexError):
            return _error(401, "signature_invalid", "Signature does not verify")
        return None

    # ---- the three endpoints
    @staticmethod
    def _amount_for(listing: Listing, guests: int) -> int:
        return listing.unit_price_cents * guests if listing.unit_price_cents else listing.total_cents

    def _quote(self, slug, data):
        try:
            listing = Listing.objects.get(merchant__slug=slug, ref=data.get("listing_ref"), available=True)
        except Listing.DoesNotExist:
            return _error(404, "listing_not_found", "No such listing")
        guests = int(data.get("guests") or 1)
        if guests > listing.capacity:
            return _error(409, "over_capacity", "Too many guests for this listing")
        quote = Quote.objects.create(
            listing=listing, quote_id=f"q_{slug}_{secrets.token_hex(4)}", amount_cents=self._amount_for(listing, guests),
            guests=guests, check_in=data.get("check_in") or None, check_out=data.get("check_out") or None,
            expires_at=timezone.now() + timedelta(minutes=15))
        return 200, {"quote_id": quote.quote_id, "amount_cents": quote.amount_cents, "currency": "USD",
                     "check_in": data.get("check_in"), "check_out": data.get("check_out"),
                     "expires_at": quote.expires_at.isoformat().replace("+00:00", "Z")}

    def _checkout(self, slug, data, headers):
        key = headers.get("Idempotency-Key", "")
        existing = MerchantBooking.objects.filter(idempotency_key=key).first() if key else None
        if existing:                                                  # same request again: same answer, no 2nd booking
            return 201, self._booking_json(existing)
        quote = Quote.objects.filter(quote_id=data.get("quote_id"), listing__merchant__slug=slug,
                                     listing__ref=data.get("listing_ref")).select_related("listing").first()
        if quote is None or quote.used or quote.expires_at <= timezone.now() or quote.amount_cents != data.get("amount_cents"):
            return _error(409, "quote_mismatch", "The amount does not match the quote")
        quote.used = True
        quote.save(update_fields=["used", "updated_at"])
        ref = f"{slug[:2].upper()}-{''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(4))}"
        booking = MerchantBooking.objects.create(listing=quote.listing, quote=quote, booking_ref=ref,
                                                 amount_cents=quote.amount_cents, idempotency_key=key or ref)
        return 201, self._booking_json(booking)

    @staticmethod
    def _booking_json(booking):
        return {"booking_ref": booking.booking_ref, "amount_cents": booking.amount_cents, "status": booking.status,
                "verified_agent": signing.KEY_ID}

    @staticmethod
    def _cancel(slug, data):
        booking = MerchantBooking.objects.filter(booking_ref=data.get("booking_ref"), listing__merchant__slug=slug).first()
        if booking is None:
            return _error(404, "booking_not_found", "No such booking")
        booking.status = "cancelled"
        booking.save(update_fields=["status", "updated_at"])
        return 200, {"booking_ref": booking.booking_ref, "status": "cancelled", "refund_amount_cents": booking.amount_cents}
