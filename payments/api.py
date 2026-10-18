"""Consent and reconfirm endpoints. Auth is the signed consent token, not the demo member header."""
from __future__ import annotations

import functools
import uuid

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.session import set_session_cookie
from payments.services import ConsentError, ConsentService, assert_not_pan, read_consent_token
from purchases.api import ApiError, api_errors, current_member
from purchases.models import Share

svc = ConsentService()


def _error(exc: ConsentError) -> Response:
    body = {"error": {"code": exc.code, "message": exc.message, "request_id": "req_" + uuid.uuid4().hex[:12]}}
    body["error"].update(exc.extra)
    return Response(body, status=exc.status)


def consent_errors(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ConsentError as e:
            return _error(e)
    return wrapper


def public_consent(methods):
    def deco(view):
        return api_view(methods)(authentication_classes([])(permission_classes([AllowAny])(consent_errors(view))))
    return deco


def _header_member(request) -> str:
    return request.headers.get("X-Tandem-Member", "")


@public_consent(["GET"])
def consent_detail(request, token):
    response = Response(svc.get_payload(token, _header_member(request)))
    # Opening your personal link signs this browser in as you (a signed cookie, see core/session.py).
    set_session_cookie(response, read_consent_token(token, _header_member(request))["share"].member_id)
    return response


@public_consent(["POST"])
def consent_tokenize(request, token):
    assert_not_pan(request.body.decode("utf-8", errors="replace"))
    transient = (request.data or {}).get("transient_card_token", "")
    return Response(svc.tokenize(token, transient, _header_member(request)))


@public_consent(["POST"])
def consent_approve(request, token):
    data = request.data or {}
    return Response(svc.approve(
        token, data.get("agent_token_id", ""), data.get("cap_cents", 0),
        data.get("consent_text_sha256", ""), data.get("assertion") or {},
        ip=request.META.get("REMOTE_ADDR", ""), user_agent=request.headers.get("User-Agent", ""),
        header_member_id=_header_member(request)))


@public_consent(["POST"])
def consent_decline(request, token):
    return Response(svc.decline(token, (request.data or {}).get("reason", ""), _header_member(request)))


@api_view(["POST"])
@api_errors
def share_reconfirm(request, sid):
    share = Share.objects.select_related("purchase").filter(pk=sid).first()
    if share is None:
        raise ApiError("not_found", "That item does not exist.", 404)
    member = current_member(request, share.purchase.group_id)
    if share.member_id != member.id:
        raise ApiError("not_your_share", "You can only reconfirm your own share.", 403)
    try:
        return Response(svc.reconfirm(share.id, (request.data or {}).get("assertion") or {}))
    except ConsentError as e:
        return _error(e)
