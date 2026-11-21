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
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = (
            FORMAT_PREFIX
            + nonce
            + self._aead.encrypt(nonce, data, storage_key.encode())
        )
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(sealed)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return storage_key

    def load(self, storage_key: str) -> bytes:
        sealed = self._path(storage_key).read_bytes()
        if not sealed.startswith(FORMAT_PREFIX):
            raise ValueError("Unknown stored file format.")
        body = sealed[len(FORMAT_PREFIX) :]
        nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
        # The storage key is bound as associated data, so files cannot be
        # swapped between keys without detection.
        return self._aead.decrypt(nonce, ciphertext, storage_key.encode())


def get_storage() -> Storage:
    root = os.getenv("DOCUMENT_STORAGE_DIR")
    key_b64 = os.getenv("DOCUMENT_ENCRYPTION_KEY")
    if not root or not key_b64:
        raise StorageNotConfigured(
            "DOCUMENT_STORAGE_DIR and DOCUMENT_ENCRYPTION_KEY must be set."
        )
    key = base64.urlsafe_b64decode(key_b64)
    if len(key) != 32:
        raise StorageNotConfigured("DOCUMENT_ENCRYPTION_KEY must be 32 bytes.")
    return LocalEncryptedStorage(Path(root), key)
