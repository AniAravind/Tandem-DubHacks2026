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
