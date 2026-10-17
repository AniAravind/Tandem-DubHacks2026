"""Phase 0 stub consent endpoints (Ani replaces these with the real ConsentService)."""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from purchases.fixtures import FIXTURES


@api_view(["GET"])
def consent_detail(request, token):
    return Response(FIXTURES["consent_detail"])


@api_view(["POST"])
def consent_tokenize(request, token):
    return Response(FIXTURES["tokenize"])


@api_view(["POST"])
def consent_approve(request, token):
    return Response(FIXTURES["approve"])


@api_view(["POST"])
def consent_decline(request, token):
    return Response(FIXTURES["decline"])


@api_view(["POST"])
def share_reconfirm(request, sid):
    return Response(FIXTURES["reconfirm"])
