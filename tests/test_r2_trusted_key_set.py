"""R2 - trusted-key set: rotation, retirement, and kid abuse.

The review required: "Validate against an explicit trusted-key set; an attacker-
controlled kid must not select arbitrary keys or bypass retirement." Each test below
corresponds to one way that could fail.

No stack required; these are pure verification tests and run in the default suite.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api" / "src"))

from drhiro_api.services import ingress_keys as ik  # noqa: E402


def _b64u(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return priv, pub


@pytest.fixture()
def two_keys():
    priv_old, pub_old = keypair()
    priv_new, pub_new = keypair()
    return priv_old, pub_old, priv_new, pub_new


def _keyst(sets):
    return ik.KeySet.from_public_keys(sets)


class TestTrustedKeySet:
    def test_valid_token_from_active_key_is_accepted(self, two_keys):
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        token = ik.issue_token(priv_new, "k2", subject="u1", scope="ingress:write",
                               audience="drhiro-api")
        claims = ik.verify_token(keys, token, audience="drhiro-api",
                                 required_scope="ingress:write")
        assert claims["sub"] == "u1" and claims["scope"] == "ingress:write"

    def test_unknown_kid_is_refused_not_guessed(self, two_keys):
        """An unknown kid must fail; there is no fallback to 'any trusted key'."""
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        token = ik.issue_token(priv_new, "k2", subject="u1", scope="ingress:write",
                               audience="drhiro-api")
        header, payload, sig = token.split(".")
        forged_header = _b64u(json.dumps({"alg": "EdDSA", "kid": "does-not-exist"}).encode())
        forged = f"{forged_header}.{payload}.{sig}"
        with pytest.raises(ik.UntrustedKey):
            ik.verify_token(keys, forged, audience="drhiro-api")

    def test_retired_kid_is_rejected_even_with_a_valid_signature(self, two_keys):
        """Rotation must actually invalidate: the OLD key still signs correctly, and
        must still be refused because its kid is retired."""
        priv_old, pub_old, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new}).rotate("k3", pub_new, retire="k2")
        assert keys.retired == {"k2": "rotated"}
        assert set(keys.active) == {"k3"}

        # Validly signed by the retired key -> refused.
        old_token = ik.issue_token(priv_old, "k2", subject="u1", scope="ingress:write",
                                   audience="drhiro-api")
        with pytest.raises(ik.RetiredKey):
            ik.verify_token(keys, old_token, audience="drhiro-api")

        # A token under the new key still works.
        new_token = ik.issue_token(priv_new, "k3", subject="u1", scope="ingress:write",
                                   audience="drhiro-api")
        assert ik.verify_token(keys, new_token, audience="drhiro-api")["sub"] == "u1"

    def test_kid_matching_a_retired_name_cannot_resurrect_it(self, two_keys):
        """A caller cannot re-activate a retired kid by asking for it."""
        _po, pub_old, _pn, pub_new = two_keys
        keys = _keyst({"k2": pub_new}).retire("k2")
        with pytest.raises(ik.RetiredKey):
            keys.kid_for_verification("k2")
        with pytest.raises(ik.UntrustedKey):
            keys.kid_for_verification("k2-suffix")

    def test_attacker_kid_cannot_select_an_arbitrary_key(self, two_keys):
        """A kid pointing at a file path / URL must not cause a key to be loaded."""
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        for hostile in ("../../etc/passwd", "/etc/shadow", "http://evil/key.pem",
                        "file:///tmp/k", "k2/../k3", "..", "k2\0", ""):
            with pytest.raises(ik.UntrustedKey):
                keys.kid_for_verification(hostile)

    def test_key_selection_headers_are_refused(self, two_keys):
        """jwk/jku/x5u/x5c must be refused, not silently ignored."""
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        token = ik.issue_token(priv_new, "k2", subject="u1", scope="ingress:write",
                               audience="drhiro-api")
        _h, payload, sig = token.split(".")
        for header_field in ("jwk", "jku", "x5u", "x5c"):
            hostile_header = _b64u(json.dumps(
                {"alg": "EdDSA", "kid": "k2", header_field: {"kty": "OKP"}}
            ).encode())
            with pytest.raises(ik.UntrustedKey):
                ik.verify_token(keys, f"{hostile_header}.{payload}.{sig}",
                                audience="drhiro-api")

    def test_alg_none_is_refused(self, two_keys):
        _po, _pub_o, _pn, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        header = _b64u(json.dumps({"alg": "none", "kid": "k2"}).encode())
        payload = _b64u(json.dumps({"sub": "attacker", "aud": "drhiro-api",
                                    "exp": int(time.time()) + 600}).encode())
        with pytest.raises(ik.AlgorithmNotAllowed):
            ik.verify_token(keys, f"{header}.{payload}.", audience="drhiro-api")

    def test_hmac_confusion_with_public_key_as_secret_is_refused(self, two_keys):
        """Classic attack: sign HS256 using the PUBLIC key bytes as the secret."""
        import base64
        import hashlib
        import hmac as hmac_mod

        _po, _pub_o, _pn, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        pub_bytes = pub_new.public_bytes(Encoding.Raw, PublicFormat.Raw)

        header = _b64u(json.dumps({"alg": "HS256", "kid": "k2"}).encode())
        payload = _b64u(json.dumps({"sub": "attacker", "aud": "drhiro-api",
                                    "exp": int(time.time()) + 600}).encode())
        sig = _b64u(hmac_mod.new(pub_bytes, f"{header}.{payload}".encode(),
                                 hashlib.sha256).digest())
        with pytest.raises(ik.AlgorithmNotAllowed):
            ik.verify_token(keys, f"{header}.{payload}.{sig}", audience="drhiro-api")

    def test_signature_from_a_different_trusted_key_is_refused(self, two_keys):
        """kid says k2 but the signature was made with another trusted key."""
        _po, _pub_o, priv_new, pub_new = two_keys
        priv_other, pub_other = keypair()
        keys = _keyst({"k2": pub_new, "k9": pub_other})
        token = ik.issue_token(priv_other, "k2", subject="u1", scope="ingress:write",
                               audience="drhiro-api")
        with pytest.raises(ik.IngressKeyError):
            ik.verify_token(keys, token, audience="drhiro-api")

    def test_wrong_audience_and_scope_are_refused(self, two_keys):
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        t = ik.issue_token(priv_new, "k2", subject="u1", scope="model:read",
                           audience="other-api")
        with pytest.raises(ik.ClaimRejected):
            ik.verify_token(keys, t, audience="drhiro-api")
        t2 = ik.issue_token(priv_new, "k2", subject="u1", scope="model:read",
                            audience="drhiro-api")
        with pytest.raises(ik.ClaimRejected):
            ik.verify_token(keys, t2, audience="drhiro-api", required_scope="ingress:write")

    def test_expired_token_is_refused(self, two_keys):
        _po, _pub_o, priv_new, pub_new = two_keys
        keys = _keyst({"k2": pub_new})
        t = ik.issue_token(priv_new, "k2", subject="u1", scope="ingress:write",
                           audience="drhiro-api", ttl_s=-10)
        with pytest.raises(ik.ClaimRejected):
            ik.verify_token(keys, t, audience="drhiro-api")

    def test_keyset_forbids_a_kid_both_active_and_retired(self, two_keys):
        _po, _pub_o, _pn, pub_new = two_keys
        with pytest.raises(ik.IngressKeyError):
            ik.KeySet(active={"k2": pub_new}, retired={"k2": "rotated"})

    def test_directory_load_rejects_path_traversal_filenames(self, tmp_path):
        _pn, pub_new = keypair()
        (tmp_path / "good.pub").write_text(
            _b64u(pub_new.public_bytes(Encoding.Raw, PublicFormat.Raw))
        )
        keys = ik.KeySet.from_directory(tmp_path)
        assert set(keys.active) == {"good"}
        # A filename that would escape is refused outright.
        (tmp_path / "..evil.pub").write_text("AAAA")
        with pytest.raises(ik.IngressKeyError):
            ik.KeySet.from_directory(tmp_path)


class TestEnvelope:
    def test_signed_envelope_verifies(self, two_keys):
        priv_old, pub_old, _pn, _pub_n = two_keys
        keys = _keyst({"k1": pub_old})
        env = ik.sign_envelope(priv_old, "k1", {"chat_id": "1", "message_id": "2",
                                                "iat": time.time()})
        out = ik.verify_envelope(keys, env)
        assert out.payload["chat_id"] == "1" and out.kid == "k1"

    def test_retired_envelope_key_is_rejected(self, two_keys):
        priv_old, pub_old, priv_new, pub_new = two_keys
        keys = _keyst({"k1": pub_old}).rotate("k2", pub_new, retire="k1")
        env = ik.sign_envelope(priv_old, "k1", {"chat_id": "1", "iat": time.time()})
        with pytest.raises(ik.RetiredKey):
            ik.verify_envelope(keys, env)

    def test_stale_envelope_is_rejected(self, two_keys):
        priv_old, pub_old, _pn, _pub_n = two_keys
        keys = _keyst({"k1": pub_old})
        env = ik.sign_envelope(priv_old, "k1", {"chat_id": "1", "iat": time.time() - 9999})
        with pytest.raises(ik.ClaimRejected):
            ik.verify_envelope(keys, env)

    def test_tampered_payload_is_rejected(self, two_keys):
        priv_old, pub_old, _pn, _pub_n = two_keys
        keys = _keyst({"k1": pub_old})
        env = ik.sign_envelope(priv_old, "k1", {"chat_id": "1", "iat": time.time()})
        h, _p, s = env.split(".")
        forged = _b64u(json.dumps({"chat_id": "999", "iat": time.time()}).encode())
        with pytest.raises(ik.IngressKeyError):
            ik.verify_envelope(keys, f"{h}.{forged}.{s}")
