"""Private, encrypted document storage.

Files are encrypted with AES-256-GCM before they touch the disk and are only
ever read back through the backend after a permission check. The database
keeps the storage key, size and SHA-256 of the plain file; the storage key
is an internal reference, never a way to access a file.

Configuration (environment variables, never committed):
- DOCUMENT_STORAGE_DIR: directory for encrypted files (outside the repo).
- DOCUMENT_ENCRYPTION_KEY: base64 of 32 random bytes. Generate one with
  ``uv run python -m app.cli generate-storage-key``. Losing it makes every
  stored document unreadable, so keep it in the secrets store and backups.

The ``Storage`` interface allows a later move to private object storage.
"""

import base64
import os
import secrets
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
FORMAT_PREFIX = b"CSC1"  # identifies the file format/version on disk


class StorageNotConfigured(RuntimeError):
    pass


class Storage(Protocol):
    def save(self, data: bytes) -> str: ...

    def load(self, storage_key: str) -> bytes: ...

    def delete(self, storage_key: str) -> None: ...

    def exists(self, storage_key: str) -> bool: ...


def generate_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


class LocalEncryptedStorage:
    def __init__(self, root: Path, key: bytes) -> None:
        self._root = root
        self._aead = AESGCM(key)

    def _path(self, storage_key: str) -> Path:
        # Keys are generated here (hex only); reject anything else so a stored
        # value can never point outside the storage directory.
        if len(storage_key) != 35 or not all(
            c in "0123456789abcdef/" for c in storage_key
        ):
            raise ValueError("Invalid storage key.")
        return self._root / storage_key

    def save(self, data: bytes) -> str:
        name = secrets.token_hex(16)
        storage_key = f"{name[:2]}/{name}"
        path = self._path(storage_key)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.mkdir(exist_ok=True, mode=0o700)
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = (
            FORMAT_PREFIX
            + nonce
            + self._aead.encrypt(nonce, data, storage_key.encode())
        )
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(sealed)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return storage_key

    def delete(self, storage_key: str) -> None:
        """Best effort: only used to undo a write whose database row failed."""
        try:
            self._path(storage_key).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).exists()

    def load(self, storage_key: str) -> bytes:
        sealed = self._path(storage_key).read_bytes()
        if not sealed.startswith(FORMAT_PREFIX):
            raise ValueError("Unknown stored file format.")
        body = sealed[len(FORMAT_PREFIX) :]
        nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
        # The storage key is bound as associated data, so files cannot be
        # swapped between keys without detection.
        return self._aead.decrypt(nonce, ciphertext, storage_key.encode())


def _load_key() -> bytes:
    key_b64 = os.getenv("DOCUMENT_ENCRYPTION_KEY")
    if not key_b64:
        raise StorageNotConfigured("DOCUMENT_ENCRYPTION_KEY must be set.")
    try:
        key = base64.b64decode(key_b64, altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise StorageNotConfigured(
            "DOCUMENT_ENCRYPTION_KEY is not valid base64."
        ) from None
    if len(key) != 32:
        raise StorageNotConfigured("DOCUMENT_ENCRYPTION_KEY must be 32 bytes.")
    return key


def get_storage() -> Storage:
    root = os.getenv("DOCUMENT_STORAGE_DIR")
    if not root:
        raise StorageNotConfigured("DOCUMENT_STORAGE_DIR must be set.")
    return LocalEncryptedStorage(Path(root), _load_key())


def seal(data: bytes, context: str) -> bytes:
    """Encrypt a small value for a database column (e.g. OCR results).

    ``context`` names what the value belongs to and is bound as associated
    data, so a sealed value cannot be moved to another row unnoticed.
    """
    nonce = secrets.token_bytes(NONCE_BYTES)
    return (
        FORMAT_PREFIX
        + nonce
        + AESGCM(_load_key()).encrypt(nonce, data, context.encode())
    )


def unseal(blob: bytes, context: str) -> bytes:
    if not blob.startswith(FORMAT_PREFIX):
        raise ValueError("Unknown sealed value format.")
    body = blob[len(FORMAT_PREFIX) :]
    return AESGCM(_load_key()).decrypt(
        body[:NONCE_BYTES], body[NONCE_BYTES:], context.encode()
    )
