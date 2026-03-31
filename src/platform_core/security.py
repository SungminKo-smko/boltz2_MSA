from __future__ import annotations

import hashlib
import secrets


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_key(prefix: str = "b2") -> tuple[str, str]:
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)
