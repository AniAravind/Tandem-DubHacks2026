"""Print fresh local secrets. Paste them into your own .env file (never commit, never share in chat).

Usage:  python scripts/gen_secrets.py
"""
import base64
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

raw = Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())

print("DJANGO_SECRET_KEY=" + secrets.token_urlsafe(50))
print("FIELD_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("AGENT_SIGNING_KEY_B64=" + base64.b64encode(raw).decode())
