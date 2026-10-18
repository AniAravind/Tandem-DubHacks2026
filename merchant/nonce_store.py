"""Atomic shared-cache replay protection; backend errors intentionally propagate."""
from django.core.cache import cache


def claim(keyid, nonce, ttl):
    return bool(nonce) and cache.add(f"nonce:{keyid}:{nonce}", 1, timeout=max(1, ttl))
