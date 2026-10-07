"""Argon2 credentials loaded from a mounted secret or an ignored local file."""

import json
import os
from pathlib import Path
import secrets
import tomllib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
_DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def load_users():
    configured = os.getenv("APP_USERS_FILE")
    local_json = Path("secrets/users.json")
    path = Path(configured) if configured else (local_json if local_json.exists() else Path(".streamlit/secrets.toml"))
    if configured or path == local_json:
        users = json.loads(path.read_text(encoding="utf-8"))
    else:
        users = tomllib.loads(path.read_text(encoding="utf-8")).get("users", {})
    if not isinstance(users, dict) or not users:
        raise ValueError("No users configured")
    if any(not isinstance(k, str) or not isinstance(v, str)
           or not v.startswith("$argon2id$") for k, v in users.items()):
        raise ValueError("All accounts must be reset with Argon2id password hashes")
    return users


def verify_password(password, encoded_hash):
    if not isinstance(password, str) or len(password) > 256:
        return False
    selected = encoded_hash if encoded_hash and encoded_hash.startswith("$argon2id$") else _DUMMY_HASH
    try:
        valid = PASSWORD_HASHER.verify(selected, password)
        return bool(encoded_hash and encoded_hash.startswith("$argon2id$") and valid)
    except (VerificationError, InvalidHashError):
        return False
