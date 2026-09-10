"""In-stack credential rotation and retired-key rejection (disposable stack).

Runs INSIDE the stack, so the behaviour is demonstrated on the deployed containers
rather than only in unit tests. Proves:

  1. an envelope signed by the active key verifies;
  2. after rotation, the retired key's envelope is REJECTED even though its signature
     is still cryptographically valid;
  3. the new key's envelope verifies;
  4. an attacker-controlled kid cannot select a key (unknown kid, path traversal,
     or a key-selection header);
  5. algorithm substitution (alg=none, and HMAC using the public key as the secret)
     is refused.

Emits JSON with a pass/fail per check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time

sys.path.insert(0, "/app/drhiro_src")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from drhiro_api.services import ingress_keys as ik  # noqa: E402


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    checks: dict[str, str] = {}

    priv_old, pub_old = Ed25519PrivateKey.generate(), None
    pub_old = priv_old.public_key()
    priv_new = Ed25519PrivateKey.generate()
    pub_new = priv_new.public_key()

    # 1. active key verifies
    keys = ik.KeySet.from_public_keys({"k1": pub_old})
    env_old = ik.sign_envelope(priv_old, "k1", {"chat_id": "1", "iat": time.time()})
    try:
        ik.verify_envelope(keys, env_old)
        checks["active_key_verifies"] = "pass"
    except Exception as exc:
        checks["active_key_verifies"] = f"FAIL: {exc!r}"

    # 2. rotate: retire k1, activate k2
    rotated = keys.rotate("k2", pub_new, retire="k1")
    try:
        ik.verify_envelope(rotated, env_old)
        checks["retired_key_rejected"] = "FAIL: retired key still accepted"
    except ik.RetiredKey:
        checks["retired_key_rejected"] = "pass"
    except Exception as exc:
        checks["retired_key_rejected"] = f"FAIL (wrong error): {exc!r}"

    # 3. new key verifies
    env_new = ik.sign_envelope(priv_new, "k2", {"chat_id": "1", "iat": time.time()})
    try:
        out = ik.verify_envelope(rotated, env_new)
        checks["new_key_verifies"] = "pass" if out.kid == "k2" else "FAIL: wrong kid"
    except Exception as exc:
        checks["new_key_verifies"] = f"FAIL: {exc!r}"

    # 4. kid abuse
    h, p, s = env_new.split(".")
    hostile_kids = ["unknown-kid", "../../etc/passwd", "/etc/shadow", ""]
    bad = []
    for kid in hostile_kids:
        forged = f"{b64u(json.dumps({'alg': 'EdDSA', 'kid': kid}).encode())}.{p}.{s}"
        try:
            ik.verify_envelope(rotated, forged)
            bad.append(kid)
        except ik.IngressKeyError:
            pass
    checks["attacker_kid_cannot_select_key"] = "pass" if not bad else f"FAIL: accepted {bad}"

    forged_header = b64u(json.dumps({"alg": "EdDSA", "kid": "k2",
                                     "jwk": {"kty": "OKP"}}).encode())
    try:
        ik.verify_envelope(rotated, f"{forged_header}.{p}.{s}")
        checks["key_selection_header_refused"] = "FAIL: jwk honoured"
    except ik.UntrustedKey:
        checks["key_selection_header_refused"] = "pass"
    except Exception as exc:
        checks["key_selection_header_refused"] = f"FAIL (wrong error): {exc!r}"

    # 5. algorithm substitution
    payload = b64u(json.dumps({"chat_id": "1", "iat": time.time()}).encode())
    none_header = b64u(json.dumps({"alg": "none", "kid": "k2"}).encode())
    try:
        ik.verify_envelope(rotated, f"{none_header}.{payload}.")
        checks["alg_none_refused"] = "FAIL: accepted"
    except ik.AlgorithmNotAllowed:
        checks["alg_none_refused"] = "pass"

    pub_bytes = pub_new.public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.Raw,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.Raw,
    )
    hs_header = b64u(json.dumps({"alg": "HS256", "kid": "k2"}).encode())
    hs_sig = b64u(hmac.new(pub_bytes, f"{hs_header}.{payload}".encode(), hashlib.sha256).digest())
    try:
        ik.verify_envelope(rotated, f"{hs_header}.{payload}.{hs_sig}")
        checks["hmac_confusion_refused"] = "FAIL: accepted"
    except ik.AlgorithmNotAllowed:
        checks["hmac_confusion_refused"] = "pass"

    print(json.dumps(checks, indent=2))
    return 0 if all(v == "pass" for v in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
