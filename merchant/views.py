import base64
from django.http import JsonResponse
from cryptography.hazmat.primitives import serialization
from merchant.signing import KEY_ID, _priv


def agent_keys(request):
    raw = _priv().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return JsonResponse({"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": KEY_ID, "x": x, "use": "sig"}],
                         "simulated_directory": True})


def not_implemented(request, *args, **kwargs):
    """Phase 0 stub for the mock merchant endpoints (Rishab replaces these)."""
    return JsonResponse({"error": {"code": "not_implemented", "message": "Merchant endpoint not built yet"}}, status=501)


# Merchant endpoints intentionally use only merchant-owned inventory and bookings.
import json
import secrets
import uuid
import re
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from merchant.models import Listing, Quote, MerchantBooking, SignedRequest
from merchant.verify import verify_request


def failure(code, status=400):
    return JsonResponse({"error": {"code": code, "message": code.replace("_", " "),
                                   "request_id": "req_" + uuid.uuid4().hex}}, status=status)


def endpoint(action, method, tag):
    @csrf_exempt
    def view(request, slug):
        if request.method != method:
            return failure("method_not_allowed", 405)
        try:
            verified = verify_request(request, tag)
        except Exception:
            return failure("verification_unavailable", 503)
        # Keep merchant audit independent of the purchase transaction and its global event-chain lock.
        # Purchase-side HTTP callers publish group-scoped events after receiving this response.
        signature_input = request.headers.get("Signature-Input", "")
        def integer_parameter(name):
            found = re.search(rf";{name}=(\d+)", signature_input)
            return int(found.group(1)) if found else 0
        SignedRequest.objects.create(purpose="checkout" if "checkout" in request.path else "cancel" if "cancel" in request.path else "browse",
            method=request.method, authority=request.get_host(), path=request.path,
            keyid=verified.keyid[:64], nonce=verified.nonce[:128], created=integer_parameter("created"),
            expires=integer_parameter("expires"), tag=tag, content_digest=request.headers.get("Content-Digest", "")[:128],
            signature_b64=request.headers.get("Signature", ""), verified=verified.ok,
            response_code=None if verified.ok else 401, reject_reason=verified.reason)
        if not verified.ok:
            return failure(verified.reason, 401)
        try:
            data = json.loads(request.body or b"{}") if method == "POST" else request.GET
            if not hasattr(data, "get"):
                return failure("invalid_request")
            with transaction.atomic():
                return action(slug, data, request)
        except (ValueError, TypeError, KeyError):
            return failure("invalid_request")
    return view


def listing_action(slug, data, request):
    guests = int(data.get("group_size", 1))
    if guests < 1:
        return failure("invalid_request")
    rows = Listing.objects.filter(merchant__slug=slug, available=True, capacity__gte=guests)
    return JsonResponse({"listings": [{"listing_ref": r.ref, "title": r.title,
        "total_cents": r.unit_price_cents * guests if r.unit_price_cents else r.total_cents,
        "capacity": r.capacity, "features": r.features, "meta": r.meta, "demo_data": True} for r in rows]})


def quote_action(slug, data, request):
    listing = Listing.objects.filter(merchant__slug=slug, ref=data.get("listing_ref"), available=True).first()
    if not listing:
        return failure("listing_not_found", 404)
    guests = int(data.get("guests", 1))
    if not 1 <= guests <= listing.capacity:
        return failure("over_capacity", 409)
    from datetime import date
    for field in ("check_in", "check_out"):
        if data.get(field):
            date.fromisoformat(data[field])
    if data.get("check_in") and data.get("check_out") and data["check_in"] >= data["check_out"]:
        return failure("invalid_dates")
    quote = Quote.objects.create(listing=listing, quote_id="q_" + secrets.token_hex(12), guests=guests,
        amount_cents=listing.unit_price_cents * guests if listing.unit_price_cents else listing.total_cents,
        check_in=data.get("check_in") or None, check_out=data.get("check_out") or None,
        expires_at=timezone.now() + timedelta(minutes=15))
    return JsonResponse({"quote_id": quote.quote_id, "amount_cents": quote.amount_cents, "currency": "USD",
        "check_in": data.get("check_in"), "check_out": data.get("check_out"), "expires_at": quote.expires_at.isoformat()})


def booking_json(booking):
    return {"booking_ref": booking.booking_ref, "amount_cents": booking.amount_cents,
            "status": booking.status, "verified_agent": KEY_ID}


def checkout_action(slug, data, request):
    key = request.headers.get("Idempotency-Key")
    if not key or len(key) > 128:
        return failure("idempotency_key_required")
    quote = Quote.objects.select_for_update().filter(quote_id=data.get("quote_id"),
        listing__merchant__slug=slug, listing__ref=data.get("listing_ref")).select_related("listing").first()
    if not quote:
        return failure("quote_mismatch", 409)
    existing = MerchantBooking.objects.filter(idempotency_key=key).first()
    if existing:
        if existing.quote_id != quote.id or existing.amount_cents != data.get("amount_cents"):
            return failure("idempotency_conflict", 409)
        return JsonResponse(booking_json(existing), status=201)
    if quote.used or quote.expires_at <= timezone.now() or quote.amount_cents != data.get("amount_cents") or not quote.listing.available:
        return failure("quote_mismatch", 409)
    payment = data.get("payment")
    if not isinstance(payment, dict) or not isinstance(payment.get("mandate_ids"), list) or not payment["mandate_ids"]:
        return failure("mandate_required", 409)
    booking = MerchantBooking.objects.create(listing=quote.listing, quote=quote,
        booking_ref=slug[:2].upper() + "-" + secrets.token_hex(4).upper(),
        amount_cents=quote.amount_cents, idempotency_key=key)
    quote.used = True
    quote.save(update_fields=["used", "updated_at"])
    return JsonResponse(booking_json(booking), status=201)


def cancel_action(slug, data, request):
    booking = MerchantBooking.objects.select_for_update().filter(booking_ref=data.get("booking_ref"), listing__merchant__slug=slug).select_related("listing").first()
    if not booking:
        return failure("booking_not_found", 404)
    policy = booking.listing.meta.get("cancellation", "")
    refund = 0 if "non-refundable" in policy.lower() else booking.amount_cents
    match = re.search(r"Free until ([A-Za-z]+ \d{1,2})", policy)
    if match:
        from datetime import datetime
        year = booking.quote.check_in.year if booking.quote.check_in else timezone.now().year
        cutoff = datetime.strptime(f"{match.group(1)} {year}", "%b %d %Y").date()
        if timezone.localdate() > cutoff:
            refund = 0
    booking.status = "cancelled"
    booking.save(update_fields=["status", "updated_at"])
    return JsonResponse({"booking_ref": booking.booking_ref, "status": "cancelled", "refund_amount_cents": refund})


listings = endpoint(listing_action, "GET", "agent-browser-auth")
quotes = endpoint(quote_action, "POST", "agent-browser-auth")
agent_checkout = endpoint(checkout_action, "POST", "agent-payer-auth")
agent_cancel = endpoint(cancel_action, "POST", "agent-payer-auth")
