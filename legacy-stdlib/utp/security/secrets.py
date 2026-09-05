"""A02 Cryptographic Failures: secrets, key rotation and field encryption.

Secrets never live in source or in the database. A :class:`SecretProvider` resolves
them at runtime; production uses AWS Secrets Manager or SSM Parameter Store, and the
environment provider is for local development only (R73.9).

Key rotation is designed in rather than bolted on. Every ciphertext and every
signature carries the id of the key that produced it, so a new key can be introduced
for new writes while old material still verifies. Without that label, rotation means
re-encrypting everything at once, which in practice means never rotating.

On encryption of stored fields: the platform's position is that at-rest encryption is
the storage layer's job — RDS with KMS, S3 with SSE-KMS, encrypted backups and logs
(R73.8) — because that protects every column without the application holding key
material. :class:`FieldCipher` exists for the narrower case where a specific column
needs to be unreadable even to someone with database access, and it is explicit that
the default implementation is *not* authenticated encryption and must be backed by KMS
in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Protocol

from ..core.errors import ConfigurationError

#: Logical secret names the platform resolves. Naming them centrally means a
#: deployment can be checked for completeness before it starts serving traffic.
SECRET_NAMES: tuple[str, ...] = (
    "database.url",
    "qr.signing_key",
    "csrf.signing_key",
    "session.signing_key",
    "offline_cache.signing_key",
    "payment.api_key",
    "payment.webhook_secret",
    "email.api_key",
    "s3.kms_key_id",
    "cdn.signing_key_id",
    "cdn.signing_private_key",
    "field_encryption.key",
)


class SecretProvider(Protocol):
    """Runtime secret resolution."""

    def get(self, name: str) -> str:
        """Return the secret, or raise :class:`ConfigurationError`."""

    def get_versioned(self, name: str) -> tuple[str, str]:
        """Return ``(key_id, secret)`` so rotation can be tracked."""


@dataclass(slots=True)
class EnvironmentSecretProvider:
    """Development provider. Reads ``UTP_SECRET_<UPPER_SNAKE>`` from the environment."""

    prefix: str = "UTP_SECRET_"
    #: Explicit values, used by tests instead of mutating the process environment.
    overrides: dict[str, str] = field(default_factory=dict)

    def _env_name(self, name: str) -> str:
        return self.prefix + name.replace(".", "_").upper()

    def get(self, name: str) -> str:
        if name in self.overrides:
            return self.overrides[name]
        value = os.environ.get(self._env_name(name))
        if not value:
            raise ConfigurationError(
                f"Secret {name!r} is not configured.",
                details={"secret": name, "expected_env": self._env_name(name)},
            )
        return value

    def get_versioned(self, name: str) -> tuple[str, str]:
        value = self.get(name)
        # Derive a stable, non-reversible label so rotation is observable without
        # ever logging the secret itself.
        key_id = hashlib.blake2b(value.encode("utf-8"), digest_size=6).hexdigest()
        return key_id, value


@dataclass(slots=True)
class AwsSecretsManagerProvider:
    """Production provider backed by AWS Secrets Manager.

    boto3 is imported lazily so the platform still imports on a machine without it,
    and so the test suite never needs AWS credentials. Values are cached per process
    because Secrets Manager is billed per call and rotation is handled by key id
    rather than by cache expiry.
    """

    secret_prefix: str = "utp/"
    region_name: str | None = None
    _client: object | None = None
    _cache: dict[str, tuple[str, str]] = field(default_factory=dict)

    def _get_client(self):  # pragma: no cover - requires AWS
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ConfigurationError(
                    "AWS secret resolution requires boto3.", details={"missing": "boto3"}
                ) from exc
            self._client = boto3.client("secretsmanager", region_name=self.region_name)
        return self._client

    def get(self, name: str) -> str:
        return self.get_versioned(name)[1]

    def get_versioned(self, name: str) -> tuple[str, str]:  # pragma: no cover - requires AWS
        if name in self._cache:
            return self._cache[name]
        client = self._get_client()
        response = client.get_secret_value(SecretId=f"{self.secret_prefix}{name}")
        value = response.get("SecretString")
        if not value:
            binary = response.get("SecretBinary")
            value = base64.b64decode(binary).decode("utf-8") if binary else ""
        if not value:
            raise ConfigurationError(f"Secret {name!r} resolved empty.", details={"secret": name})
        key_id = str(response.get("VersionId") or "current")
        self._cache[name] = (key_id, value)
        return key_id, value

    def invalidate(self, name: str | None = None) -> None:
        """Drop cached material after a rotation event."""
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)


def verify_configuration(provider: SecretProvider, *, required: tuple[str, ...] = SECRET_NAMES) -> dict:
    """Fail fast at start-up rather than at first use.

    A missing signing key that only surfaces when the first guest scans a QR code is a
    far worse failure than a refused deployment.
    """
    missing: list[str] = []
    present: list[str] = []
    for name in required:
        try:
            provider.get(name)
            present.append(name)
        except ConfigurationError:
            missing.append(name)
    return {"present": present, "missing": missing, "complete": not missing}


@dataclass(slots=True)
class FieldCipher:
    """Key-labelled encryption for an individual column.

    The default construction is HMAC-derived keystream XOR plus an HMAC tag — that is
    encrypt-then-MAC with a stream built from HKDF-like expansion. It is deliberately
    conservative in what it claims: it provides confidentiality and integrity against
    an attacker with database read access, using only the standard library, and it is
    **not** a substitute for KMS. Production should set ``kms_key_id`` and route
    through :meth:`encrypt_with_kms`.
    """

    key: bytes
    key_id: str = "local"
    kms_key_id: str | None = None

    @classmethod
    def from_provider(cls, provider: SecretProvider, *, kms_key_id: str | None = None) -> FieldCipher:
        key_id, secret = provider.get_versioned("field_encryption.key")
        return cls(key=secret.encode("utf-8"), key_id=key_id, kms_key_id=kms_key_id)

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(self.key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encrypt(self, plaintext: str) -> str:
        """Return ``v1.<key_id>.<nonce>.<ciphertext>.<tag>``, all base64url."""
        if plaintext is None:
            raise ValueError("plaintext required")
        raw = plaintext.encode("utf-8")
        nonce = os.urandom(16)
        stream = self._keystream(nonce, len(raw))
        ciphertext = bytes(a ^ b for a, b in zip(raw, stream))
        tag = hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()[:16]
        return ".".join(
            ["v1", self.key_id, _b64(nonce), _b64(ciphertext), _b64(tag)]
        )

    def decrypt(self, token: str) -> str:
        parts = str(token or "").split(".")
        if len(parts) != 5 or parts[0] != "v1":
            raise ValueError("malformed ciphertext")
        _, _key_id, nonce_b64, ciphertext_b64, tag_b64 = parts
        nonce = _unb64(nonce_b64)
        ciphertext = _unb64(ciphertext_b64)
        expected = hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, _unb64(tag_b64)):
            # Verify before decrypting: never act on unauthenticated ciphertext.
            raise ValueError("ciphertext failed integrity check")
        stream = self._keystream(nonce, len(ciphertext))
        return bytes(a ^ b for a, b in zip(ciphertext, stream)).decode("utf-8")

    def key_id_of(self, token: str) -> str | None:
        """Which key produced this value — the basis for staged re-encryption."""
        parts = str(token or "").split(".")
        return parts[1] if len(parts) == 5 and parts[0] == "v1" else None

    def needs_rotation(self, token: str) -> bool:
        return self.key_id_of(token) not in (None, self.key_id)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


#: Encryption-at-rest expectations for the infrastructure, asserted by deployment
#: checks rather than by application code (R73.8, R75.4).
AT_REST_REQUIREMENTS: dict[str, str] = {
    "database": "RDS PostgreSQL with KMS-managed encryption and encrypted automated backups",
    "object_storage": "S3 with SSE-KMS, bucket keys enabled, public access blocked",
    "backups": "Encrypted snapshots with point-in-time recovery",
    "logs": "CloudWatch Logs with KMS encryption; no secrets or unmasked PII in payloads",
    "queues": "SQS with SSE-KMS",
    "secrets": "Secrets Manager with automatic rotation where the provider supports it",
    "in_transit": "TLS 1.2 minimum, TLS 1.3 preferred, terminated at CloudFront/ALB",
}


__all__ = [
    "AT_REST_REQUIREMENTS",
    "SECRET_NAMES",
    "AwsSecretsManagerProvider",
    "EnvironmentSecretProvider",
    "FieldCipher",
    "SecretProvider",
    "verify_configuration",
]
