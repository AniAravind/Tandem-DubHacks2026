"""Public demo enrollment. Photon credentials and phone numbers stay server-side."""
import hashlib
import os
import re
from datetime import timedelta
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from chat.models import PhotonRegistration
from purchases.api import ApiError, api_errors
from purchases.plans import normalize_phone

SALT = "tandem.demo-onboarding.v1"
TICKET_SECONDS = 1800
HEARTBEAT_SECONDS = 60
ERRORS = {
    "capacity": "No iMessage connections are available right now. Please try again later.",
    "authorization": "The iMessage demo is temporarily unavailable. Please try the browser demo.",
    "unavailable": "We couldn’t connect your number. Please try again in a moment.",
    "ambiguous": "This number needs a connection update. Please try the browser demo for now.",
}


def registration_key(phone):
    return hashlib.sha256((os.getenv("SPECTRUM_PROJECT_ID", "") + phone).encode()).hexdigest()


def cipher():
    # Public onboarding must never fall back to plaintext phone storage.
    return Fernet(os.environ["FIELD_ENCRYPTION_KEY"].encode())


def decrypt_phone(registration):
    return cipher().decrypt(registration.phone_encrypted.encode()).decode()


def ticket_for(registration):
    return signing.dumps({"registration": registration.pk}, salt=SALT)


def from_ticket(token):
    try:
        value = signing.loads(token, salt=SALT, max_age=TICKET_SECONDS)
        return PhotonRegistration.objects.get(pk=value["registration"])
    except (signing.BadSignature, PhotonRegistration.DoesNotExist, KeyError, TypeError, ValueError):
        raise ApiError("expired_registration", "This connection link expired. Enter your number again.", 410)


def online(registration):
    return bool(registration.connected_at and registration.connected_at > timezone.now() - timedelta(seconds=HEARTBEAT_SECONDS))


def public_status(registration):
    status = registration.status
    if status in {"ready", "active"} and not online(registration):
        status = "connecting"
    result = {"status": status}
    if status in {"ready", "active"}:
        result["destination"] = registration.destination
    if status == "failed":
        result["error"] = {"code": registration.error_code, "message": ERRORS.get(registration.error_code, ERRORS["unavailable"])}
    return result


def limited(key, limit, seconds):
    try:
        cache.add(key, 0, timeout=seconds)
        return cache.incr(key) > limit
    except Exception:
        # A public enrollment endpoint must not become unlimited on a cache outage.
        raise ApiError("unavailable", "Registration is temporarily unavailable. Try again shortly.", 503)


def enqueue(key):
    from chat.tasks import provision_registration
    try:
        provision_registration.delay(key)
    except Exception:
        PhotonRegistration.objects.filter(pk=key, status="queued").update(status="failed", error_code="unavailable")


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
@api_errors
def register(request):
    if os.getenv("PHOTON_PUBLIC_REGISTRATION") != "1" or not all(os.getenv(key) for key in ("SPECTRUM_PROJECT_ID", "SPECTRUM_PROJECT_SECRET", "PHOTON_BRIDGE_SECRET")):
        raise ApiError("unavailable", "The iMessage demo is not open right now. Try the browser demo below.", 503)
    try:
        cipher()
    except (KeyError, ValueError):
        raise ApiError("unavailable", "Registration is temporarily unavailable.", 503)
    data = request.data if isinstance(request.data, dict) else {}
    phone = normalize_phone(data.get("phone"))
    if not phone:
        raise ApiError("invalid_phone", "Enter a valid phone number including its country code.")
    # Heroku appends the connecting IP; do not trust an attacker-supplied first hop.
    address = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[-1].strip()
    ip_key = hashlib.sha256(address.encode()).hexdigest()
    if limited("enroll:ip:" + ip_key, 10, 900):
        raise ApiError("rate_limited", "Too many attempts. Please wait a few minutes.", 429)
    key = registration_key(phone)
    if limited("enroll:phone:" + key, 5, 900):
        raise ApiError("rate_limited", "Please wait a few minutes before trying this number again.", 429)
    with transaction.atomic():
        registration, created = PhotonRegistration.objects.get_or_create(
            key=key, defaults={"project_id": os.getenv("SPECTRUM_PROJECT_ID", ""), "phone_encrypted": cipher().encrypt(phone.encode()).decode()})
        registration = PhotonRegistration.objects.select_for_update().get(pk=key)
        stale = registration.status in {"queued", "provisioning"} and registration.updated_at < timezone.now() - timedelta(minutes=3)
        if created or registration.status == "failed" or stale:
            if limited("enroll:global", int(os.getenv("PHOTON_REGISTRATION_DAILY_MAX", "100")), 86400):
                raise ApiError("rate_limited", "The demo’s daily connection allowance is used up. Please try again tomorrow.", 429)
            registration.status = "queued"
            registration.error_code = ""
            registration.save(update_fields=["status", "error_code", "updated_at"])
            transaction.on_commit(lambda: enqueue(key))
    response = Response({"token": ticket_for(registration), **public_status(registration)}, status=202)
    response["Cache-Control"] = "no-store"
    return response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
@api_errors
def status(request):
    registration = from_ticket(request.headers.get("X-Tandem-Onboarding", ""))
    response = Response(public_status(registration))
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


class ProvisionError(Exception):
    def __init__(self, code):
        self.code = code


def photon_request(method, *, body=None, params=None):
    project = os.getenv("SPECTRUM_PROJECT_ID", "")
    secret = os.getenv("SPECTRUM_PROJECT_SECRET", "")
    if not project or not secret:
        raise ProvisionError("authorization")
    try:
        response = httpx.request(method, f"https://spectrum.photon.codes/projects/{quote(project, safe='')}/users/",
                                 auth=(project, secret), json=body, params=params, timeout=20, follow_redirects=False)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise ProvisionError("unavailable") from None
    if response.status_code in {401, 403}:
        raise ProvisionError("authorization")
    if response.status_code in {402, 409, 429}:
        raise ProvisionError("capacity")
    if not response.is_success or not isinstance(payload, dict) or payload.get("succeed") is not True:
        raise ProvisionError("unavailable")
    return payload.get("data")


def ensure_user(phone):
    matches = []
    offset = 0
    while True:
        data = photon_request("GET", params={"offset": offset, "limit": 100})
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise ProvisionError("unavailable")
        rows = data["users"]
        matches.extend(user for user in rows if normalize_phone(user.get("phoneNumber")) == phone)
        offset += len(rows)
        if not rows or offset >= data.get("total", offset):
            break
    if len(matches) > 1:
        raise ProvisionError("ambiguous")
    user = matches[0] if matches else photon_request("POST", body={"type": "shared", "phoneNumber": phone, "sendInvite": False})
    if isinstance(user, dict) and isinstance(user.get("user"), dict):
        user = user["user"]
    if (not isinstance(user, dict) or not isinstance(user.get("id"), str)
            or normalize_phone(user.get("phoneNumber")) != phone
            or not re.fullmatch(r"\+[1-9]\d{7,14}", user.get("assignedPhoneNumber") or "")
            or user.get("type") not in {"shared", "dedicated"}):
        raise ProvisionError("unavailable")
    return user


def provision(key):
    # Shared lock also covers provider calls, preventing duplicate remote allocations.
    lock = "enroll:provision:" + key
    if not cache.add(lock, True, timeout=120):
        return
    try:
        registration = PhotonRegistration.objects.get(pk=key)
        if registration.project_id != os.getenv("SPECTRUM_PROJECT_ID", ""):
            raise ProvisionError("authorization")
        if registration.status not in {"queued", "provisioning", "failed"}:
            return
        PhotonRegistration.objects.filter(pk=key).update(status="provisioning", updated_at=timezone.now())
        user = ensure_user(decrypt_phone(registration))
        PhotonRegistration.objects.filter(pk=key).update(
            provider_user_id=user["id"], destination=user["assignedPhoneNumber"], allocation=user["type"],
            status="connecting", error_code="", updated_at=timezone.now())
    except ProvisionError as exc:
        PhotonRegistration.objects.filter(pk=key).update(status="failed", error_code=exc.code, updated_at=timezone.now())
    except Exception:
        PhotonRegistration.objects.filter(pk=key).update(status="failed", error_code="unavailable", updated_at=timezone.now())
    finally:
        cache.delete(lock)
