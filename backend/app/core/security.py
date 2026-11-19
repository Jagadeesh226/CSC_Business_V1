import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

# Verified against when the email is unknown, so a failed login takes about the
# same time whether or not the account exists.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-only")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and (
            password_hash is not None
        )
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def generate_token() -> str:
    """Random URL-safe token (256 bits) for session and CSRF cookies."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored only as SHA-256 hashes, never in plain text."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)
