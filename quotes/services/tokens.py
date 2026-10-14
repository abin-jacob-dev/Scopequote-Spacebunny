from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

TOKEN_BYTES = 32
NONCE_BYTES = 12
ASSOCIATED_DATA_PREFIX = "scopequote-share-token-v1"


class TokenConfigurationError(ImproperlyConfigured):
    pass


class TokenDecryptionError(Exception):
    pass


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    if not isinstance(token, str):
        raise TypeError("token must be a string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hashes_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(hash_token(left), hash_token(right))


def build_associated_data(share_link_id: UUID, quote_version_id: UUID) -> str:
    link_id = UUID(str(share_link_id))
    version_id = UUID(str(quote_version_id))
    return f"{ASSOCIATED_DATA_PREFIX}:{link_id}:{version_id}"


def _key_bytes() -> bytes:
    encoded = settings.SHARE_TOKEN_ENCRYPTION_KEY
    if not isinstance(encoded, (str, bytes)) or not encoded:
        raise TokenConfigurationError("Share-token encryption is not configured.")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise TokenConfigurationError(
            "Share-token encryption is not configured."
        ) from exc
    if len(key) != TOKEN_BYTES:
        raise TokenConfigurationError("Share-token encryption is not configured.")
    return key


def current_key_version() -> str:
    version = settings.SHARE_TOKEN_ENCRYPTION_KEY_VERSION
    if not isinstance(version, str) or not version.strip():
        raise TokenConfigurationError("Share-token encryption is not configured.")
    return version


def _selected_key_version(
    key_version: str | None,
    encryption_key_version: str | None,
) -> str:
    if (
        key_version is not None
        and encryption_key_version is not None
        and key_version != encryption_key_version
    ):
        raise TokenConfigurationError("Share-token encryption is not configured.")
    selected = key_version or encryption_key_version or current_key_version()
    if not isinstance(selected, str) or not selected.strip():
        raise TokenConfigurationError("Share-token encryption is not configured.")
    return selected


def _ids(share_link_id: UUID, quote_version_id: UUID) -> tuple[UUID, UUID]:
    try:
        return UUID(str(share_link_id)), UUID(str(quote_version_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Share-token identifiers are invalid.") from exc


def encrypt_token(
    token: str,
    *,
    share_link_id: UUID,
    quote_version_id: UUID,
    key_version: str | None = None,
    encryption_key_version: str | None = None,
) -> bytes:
    if not isinstance(token, str):
        raise TypeError("token must be a string")
    link_id, version_id = _ids(share_link_id, quote_version_id)
    _selected_key_version(key_version, encryption_key_version)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(_key_bytes()).encrypt(
        nonce,
        token.encode("utf-8"),
        build_associated_data(link_id, version_id).encode("utf-8"),
    )
    return nonce + ciphertext


def decrypt_token(
    encrypted_token: bytes,
    *,
    share_link_id: UUID,
    quote_version_id: UUID,
    key_version: str | None = None,
    encryption_key_version: str | None = None,
) -> str:
    link_id, version_id = _ids(share_link_id, quote_version_id)
    selected_version = _selected_key_version(key_version, encryption_key_version)
    if selected_version != current_key_version():
        raise TokenDecryptionError("The encrypted share token could not be read.")
    if not isinstance(encrypted_token, (bytes, bytearray, memoryview)):
        raise TokenDecryptionError("The encrypted share token could not be read.")
    payload = bytes(encrypted_token)
    if len(payload) <= NONCE_BYTES:
        raise TokenDecryptionError("The encrypted share token could not be read.")
    nonce = payload[:NONCE_BYTES]
    ciphertext = payload[NONCE_BYTES:]
    try:
        plaintext = AESGCM(_key_bytes()).decrypt(
            nonce,
            ciphertext,
            build_associated_data(link_id, version_id).encode("utf-8"),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise TokenDecryptionError(
            "The encrypted share token could not be read."
        ) from exc


def decrypt_encrypted_token(*args, **kwargs) -> str:
    return decrypt_token(*args, **kwargs)
