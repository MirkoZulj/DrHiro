"""Trusted-key verification for model-originated and ingress-originated credentials.

Design constraints (from review correction R2):

* **Asymmetric only.** The ingress signs with a private key that exists only in the
  trusted ingress. Verifiers hold public keys. A verifier therefore never possesses
  signing capability, which an HMAC shared secret cannot offer. When HMAC is used,
  every verifier *is* a potential signer.
* **Explicit trusted key set.** Verification selects a key by `kid` from an
  operator-provisioned set ONLY. Nothing in the token can introduce a key: no `jwk`,
  `jku`, `x5u`, or `x5c` header is honoured, and a `kid` is never interpreted as a
  path, URL or filename. An attacker-controlled `kid` can therefore only choose
  among keys the operator already trusts - or fail.
* **Retirement is enforced.** A retired `kid` is rejected even when the signature
  verifies under the corresponding public key. Rotation would otherwise be
  decorative: the old key would keep working for as long as anyone kept using it.
* **Algorithm is pinned by policy, not by the token.** `none` and HMAC-family
  algorithms are refused, so the classic public-key-as-HMAC-secret confusion cannot
  apply.

This module performs no I/O to obtain keys and never logs key material.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Only these algorithms are accepted. Deliberately excludes 'none' and any
# HMAC family ('HS256'/'HS512'), which are the two classic confusion attacks.
ALLOWED_ALGS = frozenset({"EdDSA", "Ed25519"})

# Claims whose presence in a header would let the caller choose the key. Refused
# outright rather than ignored, so a downgrade attempt is visible.
KEY_SELECTION_HEADERS = frozenset({"jwk", "jku", "x5u", "x5c", "x5t", "kid_path"})


class IngressKeyError(Exception):
    """Base class for every key/credential rejection."""


class UntrustedKey(IngressKeyError):
    """The kid is not in the operator-provisioned trusted set."""


class RetiredKey(IngressKeyError):
    """The kid was trusted once and has been retired."""


class AlgorithmNotAllowed(IngressKeyError):
    """The token asks for an algorithm outside the allowed set."""


class ClaimRejected(IngressKeyError):
    """A scope, audience, issuer or validity check failed."""


def _b64u_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class KeySet:
    """Operator-provisioned trusted keys.

    `active` maps kid -> Ed25519PublicKey. `retired` maps kid -> retirement reason
    (or timestamp). A retired kid is refused even with a valid signature.
    """

    active: dict[str, Ed25519PublicKey] = field(default_factory=dict)
    retired: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        overlap = set(self.active) & set(self.retired)
        if overlap:
            raise IngressKeyError(
                f"kid(s) present in both active and retired sets: {sorted(overlap)}"
            )

    # -- construction ----------------------------------------------------- #

    @classmethod
    def from_public_keys(cls, keys: dict[str, Ed25519PublicKey], retired: dict[str, str] | None = None) -> "KeySet":
        return cls(active=dict(keys), retired=dict(retired or {}))

    @classmethod
    def from_directory(cls, directory: str | Path, retired: dict[str, str] | None = None) -> "KeySet":
        """Load `<kid>.pub` files from a directory.

        The directory is operator-provisioned. kid values are validated before use,
        so a crafted filename cannot escape the directory.
        """
        base = Path(directory)
        active: dict[str, Ed25519PublicKey] = {}
        for path in sorted(base.glob("*.pub")):
            kid = path.stem
            if not kid or any(c in kid for c in ("/", "\\", ".", "\0")) or ".." in kid:
                raise IngressKeyError(f"unsafe kid filename: {path.name!r}")
            active[kid] = Ed25519PublicKey.from_public_bytes(_b64u_decode(path.read_text().strip()))
        return cls.from_public_keys(active, retired)

    # -- rotation --------------------------------------------------------- #

    def retire(self, kid: str, reason: str = "rotated") -> "KeySet":
        """Return a new KeySet with `kid` moved to retired (active set unchanged
        otherwise). Retired keys can never be re-activated by a caller."""
        if kid not in self.active:
            raise UntrustedKey(f"cannot retire unknown kid {kid!r}")
        active = {k: v for k, v in self.active.items() if k != kid}
        retired = dict(self.retired)
        retired[kid] = reason
        return KeySet(active=active, retired=retired)

    def rotate(self, new_kid: str, new_key: Ed25519PublicKey, retire: str | None = None) -> "KeySet":
        """Add `new_kid` and optionally retire the previous kid in one step."""
        base = self.retire(retire) if retire else self
        if new_kid in base.active or new_kid in base.retired:
            raise IngressKeyError(f"kid {new_kid!r} already known")
        active = dict(base.active)
        active[new_kid] = new_key
        return KeySet(active=active, retired=dict(base.retired))

    def kid_for_verification(self, kid: Any) -> Ed25519PublicKey:
        """Resolve a kid to a trusted public key, or refuse.

        This is the single chokepoint: the token can only ever select a key the
        operator provisioned, and never one that has been retired.
        """
        if not isinstance(kid, str) or not kid:
            raise UntrustedKey("missing or non-string kid")
        if kid in self.retired:
            raise RetiredKey(f"kid {kid!r} is retired ({self.retired[kid]})")
        key = self.active.get(kid)
        if key is None:
            raise UntrustedKey(f"kid {kid!r} is not in the trusted key set")
        return key


# --------------------------------------------------------------------------- #
# envelope signing (ingress -> API)
# --------------------------------------------------------------------------- #

def sign_envelope(private_key: Ed25519PrivateKey, kid: str, payload: dict) -> str:
    """Compact signed envelope: `<b64u(header)>.<b64u(payload)>.<b64u(sig)>`."""
    header = {"alg": "EdDSA", "kid": kid}
    signing_input = (
        _b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    ).encode()
    signature = private_key.sign(signing_input)
    return signing_input.decode() + "." + _b64u_encode(signature)


@dataclass
class VerifiedEnvelope:
    kid: str
    payload: dict


def verify_envelope(keys: KeySet, envelope: str, *, now: float | None = None, max_age_s: int = 300) -> VerifiedEnvelope:
    parts = envelope.split(".")
    if len(parts) != 3:
        raise IngressKeyError("malformed envelope")
    header_b64, payload_b64, signature_b64 = parts

    header = json.loads(_b64u_decode(header_b64))
    payload = json.loads(_b64u_decode(payload_b64))

    for forbidden in KEY_SELECTION_HEADERS:
        if forbidden in header:
            raise UntrustedKey(f"header {forbidden!r} may not be used to select a key")

    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        raise AlgorithmNotAllowed(f"algorithm {alg!r} not allowed")

    # Resolve the key through the single chokepoint, using the trusted set only.
    key = keys.kid_for_verification(header.get("kid"))

    try:
        key.verify(_b64u_decode(signature_b64), f"{header_b64}.{payload_b64}".encode())
    except InvalidSignature as exc:
        raise IngressKeyError("signature verification failed") from exc

    issued = payload.get("iat")
    if issued is not None:
        clock = time.time() if now is None else now
        if abs(clock - float(issued)) > max_age_s:
            raise ClaimRejected("envelope outside the accepted time window")

    return VerifiedEnvelope(kid=header["kid"], payload=payload)


# --------------------------------------------------------------------------- #
# token issuing/verification (trusted component -> API)
# --------------------------------------------------------------------------- #

def issue_token(
    private_key: Ed25519PrivateKey,
    kid: str,
    *,
    subject: str,
    scope: str,
    audience: str,
    ttl_s: int = 300,
    issuer: str = "drhiro-trusted-ingress",
    now: float | None = None,
) -> str:
    clock = time.time() if now is None else now
    header = {"alg": "EdDSA", "kid": kid, "typ": "JWT"}
    payload = {
        "sub": subject,
        "scope": scope,
        "aud": audience,
        "iss": issuer,
        "iat": int(clock),
        "exp": int(clock) + ttl_s,
        "jti": base64.urlsafe_b64encode(__import__("os").urandom(12)).decode().rstrip("="),
    }
    signing_input = (
        _b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    ).encode()
    signature = private_key.sign(signing_input)
    return signing_input.decode() + "." + _b64u_encode(signature)


def verify_token(
    keys: KeySet,
    token: str,
    *,
    audience: str,
    required_scope: str | None = None,
    now: float | None = None,
) -> dict:
    """Verify a token against the trusted key set.

    Note the honest limitation this does NOT solve: for any token that verifies
    under a current trusted key, the API cannot tell WHICH holder minted it. That is
    exactly why the model runtime must not hold a valid signing key at all.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise IngressKeyError("malformed token")
    header_b64, payload_b64, signature_b64 = parts

    header = json.loads(_b64u_decode(header_b64))
    payload = json.loads(_b64u_decode(payload_b64))

    for forbidden in KEY_SELECTION_HEADERS:
        if forbidden in header:
            raise UntrustedKey(f"header {forbidden!r} may not be used to select a key")

    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        # Covers alg='none' and HMAC confusion with a public key as the secret.
        raise AlgorithmNotAllowed(f"algorithm {alg!r} not allowed")

    key = keys.kid_for_verification(header.get("kid"))

    try:
        key.verify(_b64u_decode(signature_b64), f"{header_b64}.{payload_b64}".encode())
    except InvalidSignature as exc:
        raise IngressKeyError("signature verification failed") from exc

    clock = time.time() if now is None else now
    if float(payload.get("exp", 0)) < clock:
        raise ClaimRejected("token expired")
    if payload.get("aud") != audience:
        raise ClaimRejected(f"wrong audience {payload.get('aud')!r}")
    if required_scope is not None:
        granted = set(str(payload.get("scope", "")).split())
        if required_scope not in granted:
            raise ClaimRejected(f"missing scope {required_scope!r}")

    return payload
