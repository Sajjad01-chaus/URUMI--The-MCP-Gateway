import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

API_KEY_PREFIX = "mcpgw_"
_hasher = PasswordHasher()


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext key, display prefix, sha256 hash). Only the hash is stored."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[: len(API_KEY_PREFIX) + 6], hash_token(key)


def hash_token(token: str) -> str:
    # Keys are 256-bit random, so a fast hash is safe and allows an indexed lookup on every request.
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class CredentialBox:
    """Encrypts upstream credentials at rest (Fernet = AES-128-CBC + HMAC-SHA256).

    Accepts comma-separated keys: the first encrypts, all can decrypt (key rotation).
    """

    def __init__(self, keys: str):
        self._fernet = MultiFernet([Fernet(k.strip().encode()) for k in keys.split(",") if k.strip()])

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as exc:
            raise ValueError("stored credential cannot be decrypted with the configured key") from exc
