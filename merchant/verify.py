"""Merchant verification. Replay protection uses the shared cache in deployment."""
import base64
import re
import time
from dataclasses import dataclass
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from merchant.nonce_store import claim
from merchant import signing

@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    keyid: str = ""
    nonce: str = ""


def verify_request(request, expected_tag):
    value = request.headers.get("Signature-Input", "")
    signature = request.headers.get("Signature", "")
    if not value or not signature:
        return VerifyResult(False, "signature_missing")
    try:
        label, params = value.split("=", 1)
        fields = {}
        for key, quoted, number in re.findall(r';(\w+)=(?:"([^"]*)"|(\d+))', params):
            fields[key] = quoted or number
        keyid, nonce = fields.get("keyid", ""), fields.get("nonce", "")
        def reject(reason):
            return VerifyResult(False, reason, keyid, nonce)
        if fields.get("tag") != expected_tag:
            return reject("wrong_tag")
        created, expires = int(fields.get("created", 0)), int(fields.get("expires", 0))
        now = int(time.time())
        if created > now + 30 or now >= expires or not 0 < expires-created <= signing.MAX_WINDOW_S:
            return reject("expired")
        if not claim(keyid, nonce, expires-now):
            return reject("nonce_replayed")
        if keyid != signing.KEY_ID:
            return reject("unknown_agent")
        components = re.findall(r'"([^"]+)"', params.split(")", 1)[0])
        digest = request.headers.get("Content-Digest")
        payer = expected_tag == "agent-payer-auth"
        if payer and "content-digest" not in components:
            return reject("digest_mismatch")
        if "content-digest" in components and digest != signing.content_digest(request.body):
            return reject("digest_mismatch")
        if label != "sig2" or fields.get("alg") != "ed25519" or components != ["@authority", "@path"] + (["content-digest"] if "content-digest" in components else []):
            return reject("signature_invalid")
        if not signature.startswith("sig2=:") or not signature.endswith(":"):
            return reject("signature_invalid")
        raw = base64.b64decode(signature.removeprefix("sig2=:").removesuffix(":"), validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(signing.public_key_b64())).verify(
            raw, signing.signature_base(request.get_host(), request.path, digest if "content-digest" in components else None, params))
        return VerifyResult(True, keyid=keyid, nonce=nonce)
    except (ValueError, InvalidSignature, KeyError, TypeError):
        return VerifyResult(False, "signature_invalid")
