import os
from cryptography.fernet import Fernet

_f = Fernet(os.environ["FIELD_ENCRYPTION_KEY"].encode()) if os.getenv("FIELD_ENCRYPTION_KEY") else None


def encrypt_int(v):
    if v is None:
        return ""
    # "plain:" is only used when no key is set (local development)
    return _f.encrypt(str(v).encode()).decode() if _f else f"plain:{v}"


def decrypt_int(s):
    if not s:
        return None
    if s.startswith("plain:"):
        return int(s[6:])
    return int(_f.decrypt(s.encode()).decode())
