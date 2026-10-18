"""
Member sessions (milestone M8).

When someone opens their personal consent or invite link, we hand their browser a signed cookie that says "this
browser belongs to member X". After that, the API knows who is calling without the demo-only X-Tandem-Member header.

The cookie is signed with django.core.signing (using SECRET_KEY), so a browser cannot change the member id inside it
without us noticing. It is HttpOnly (page scripts cannot read it) and expires after 7 days.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core import signing

COOKIE_NAME = "tandem_session"
SALT = "tandem.member-session"
MAX_AGE_SECONDS = 7 * 24 * 3600


def make_session_value(member_id) -> str:
    return signing.dumps({"member_id": str(member_id)}, salt=SALT)


def member_id_from_request(request) -> uuid.UUID | None:
    """The member id in a valid session cookie, or None (missing, tampered with, or expired)."""
    raw = request.COOKIES.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = signing.loads(raw, salt=SALT, max_age=MAX_AGE_SECONDS)
        return uuid.UUID(data["member_id"])
    except (signing.BadSignature, KeyError, ValueError):
        return None


def set_session_cookie(response, member_id) -> None:
    response.set_cookie(COOKIE_NAME, make_session_value(member_id), max_age=MAX_AGE_SECONDS, httponly=True,
                        samesite="Lax", secure=not settings.DEBUG)
