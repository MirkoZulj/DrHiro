"""APK distribution tests — all scenarios run against the mock Telegram server.

Required scenarios covered:
  1. authorized /apk request sends the expected file_id
  2. unauthorized /apk request is denied
  3. missing APK file fails safely
  4. checksum mismatch blocks upload
  5. APK over the configured size limit blocks upload
  6. failed first upload does not save a false file_id
  7. later /apk calls reuse the stored file_id
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from drhiro_bridge.apk_distribution import (
    ApkChecksumError,
    ApkManager,
    ApkTooLargeError,
)
from drhiro_bridge.config import Config
from drhiro_bridge.main import Bridge


def _write_apk(apk_dir, version="0.1.0", payload=b"PK\x03\x04 synthetic apk bytes", size=None):
    os.makedirs(apk_dir, exist_ok=True)
    apk_path = os.path.join(apk_dir, "drhiro-bridge.apk")
    data = payload
    if size is not None:
        data = data + b"\x00" * (size - len(data))
    with open(apk_path, "wb") as f:
        f.write(data)
    sha = hashlib.sha256(data).hexdigest()
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({
            "version": version,
            "sha256": sha,
            "android_requirement": "Android 8.0+",
        }, f)
    return apk_path, sha


def _make_bridge(apk_dir, allowed_username="alice", allowed_user_id="", mock_tg=None, mock_tf=None, tmp_path=None):
    cfg = Config()
    cfg.bot_token = "123456:TESTTOKEN"
    cfg.allowed_username = allowed_username
    cfg.allowed_user_id = allowed_user_id
    cfg.trueforge_url = (mock_tf or {}).get("base", "http://127.0.0.1:1")
    cfg.agent_name = "drhiro"
    cfg.poll_timeout = 2
    cfg.apk_dir = apk_dir
    cfg.apk_max_size_mb = 1  # keep tests fast; overridden where needed
    import tempfile
    cfg.pairing_state_dir = tempfile.mkdtemp(prefix="pairtest")
    b = Bridge(cfg)
    if mock_tg:
        b.tg._api = f"{mock_tg['base']}/bottok"
    return b


@pytest.fixture()
def apk_bridge(tmp_path, mock_tg, mock_tf):
    apk_dir = str(tmp_path / "apk")
    b = _make_bridge(apk_dir, mock_tg=mock_tg, mock_tf=mock_tf)
    yield {"bridge": b, "apk_dir": apk_dir, "mock_tg": mock_tg, "mock_tf": mock_tf}


def _process(b, mock_tg, text, username="alice", user_id=None):
    if user_id is not None:
        mock_tg["state"].enqueue_update_with_id(500 + len(mock_tg["state"].update_queue), 777, text, user_id)
    else:
        mock_tg["state"].enqueue_update(500 + len(mock_tg["state"].update_queue), 777, text, username)
    upd = mock_tg["state"].update_queue.pop(0)
    b._process_update(upd)


# ---------------------------------------------------------------------- #
# 1. Authorized /apk sends the expected file_id
# ---------------------------------------------------------------------- #
def test_authorized_apk_sends_expected_file_id(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)
    mock_tg["state"].upload_file_id = "FILEID-EXPECTED"

    _process(b, mock_tg, "/apk", username="alice")

    # First /apk uploads the file and then sends by file_id.
    assert mock_tg["state"].document_uploads, "expected an upload on first /apk"
    assert len(mock_tg["state"].document_sends) >= 1
    sent = mock_tg["state"].document_sends[-1]
    assert sent.get("document") == "FILEID-EXPECTED"


# ---------------------------------------------------------------------- #
# 2. Unauthorized /apk is denied
# ---------------------------------------------------------------------- #
def test_unauthorized_apk_is_denied(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)

    _process(b, mock_tg, "/apk", username="eve")

    assert not mock_tg["state"].document_uploads, "unauthorized user must not trigger an upload"
    assert not mock_tg["state"].document_sends
    assert "authorized" in mock_tg["state"].last_message_text().lower()


# ---------------------------------------------------------------------- #
# 3. Missing APK fails safely
# ---------------------------------------------------------------------- #
def test_missing_apk_fails_safely(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    # No APK written.

    _process(b, mock_tg, "/apk", username="alice")

    assert not mock_tg["state"].document_uploads, "no upload should happen with no APK"
    assert not mock_tg["state"].document_sends
    assert "not" in mock_tg["state"].last_message_text().lower() or "apk" in mock_tg["state"].last_message_text().lower()


def test_missing_apk_apkinfo_fails_safely(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _process(b, mock_tg, "/apkinfo", username="alice")
    assert "not ready" in mock_tg["state"].last_message_text().lower() or "not found" in mock_tg["state"].last_message_text().lower()


# ---------------------------------------------------------------------- #
# 4. Checksum mismatch blocks upload
# ---------------------------------------------------------------------- #
def test_checksum_mismatch_blocks_upload(apk_bridge, tmp_path):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    path, _ = _write_apk(apk_dir)
    # Corrupt the APK after writing the correct checksum.
    with open(path, "wb") as f:
        f.write(b"tampered-bytes" * 10)
    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(ApkChecksumError):
        manager.validate()

    _process(b, mock_tg, "/apk", username="alice")
    assert not mock_tg["state"].document_uploads, "tampered APK must not be uploaded"


# ---------------------------------------------------------------------- #
# 5. APK over the size limit blocks upload
# ---------------------------------------------------------------------- #
def test_apk_over_size_limit_blocks_upload(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    # 2 MB APK with a 1 MB limit configured on the bridge.
    _write_apk(apk_dir, payload=b"x", size=2 * 1024 * 1024)

    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(ApkTooLargeError):
        manager.validate()

    _process(b, mock_tg, "/apk", username="alice")
    assert not mock_tg["state"].document_uploads, "oversized APK must not be uploaded"


def test_apk_over_telegram_hard_limit_blocks_upload(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    # 51 MB APK exceeds the Telegram 50 MB hard cap even with a large limit.
    _write_apk(apk_dir, payload=b"x", size=51 * 1024 * 1024)
    manager = ApkManager(apk_dir, max_size_mb=100)
    with pytest.raises(ApkTooLargeError):
        manager.validate()


# ---------------------------------------------------------------------- #
# 6. Failed first upload does not save a false file_id
# ---------------------------------------------------------------------- #
def test_failed_first_upload_does_not_save_false_file_id(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)
    mock_tg["state"].fail_upload = True

    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(Exception):  # noqa: BLE001
        manager.register(b.tg)

    # No file_id was persisted.
    assert manager.stored_file_id() is None
    assert "file_id" not in manager.meta()


# ---------------------------------------------------------------------- #
# 7. Later /apk calls reuse the stored file_id
# ---------------------------------------------------------------------- #
def test_later_apk_reuses_stored_file_id(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)

    # First call uploads and stores the file_id.
    _process(b, mock_tg, "/apk", username="alice")
    upload_count_after_first = len(mock_tg["state"].document_uploads)
    assert upload_count_after_first == 1
    assert b.apk.stored_file_id() is not None

    # Second call must NOT re-upload — it resends the stored file_id.
    _process(b, mock_tg, "/apk", username="alice")
    assert len(mock_tg["state"].document_uploads) == upload_count_after_first, "must not re-upload"
    assert len(mock_tg["state"].document_sends) >= 2
    # The last send used the stored file_id.
    assert mock_tg["state"].document_sends[-1].get("document") == b.apk.stored_file_id()


# ---------------------------------------------------------------------- #
# Command routing helpers
# ---------------------------------------------------------------------- #
def test_start_and_help_commands(apk_bridge):
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)
    _process(b, mock_tg, "/help", username="alice")
    assert "/apk" in mock_tg["state"].last_message_text()


def test_resolved_user_id_authorizes_apk(apk_bridge):
    """After first verification, the numeric user id authorizes /apk."""
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    _write_apk(apk_dir)
    # First message from username resolves the id.
    _process(b, mock_tg, "/start", username="alice")
    assert b.cfg.allowed_user_id  # resolved
    # Now a message with that numeric id (no username) is authorized.
    _process(b, mock_tg, "/apk", username="", user_id=int(b.cfg.allowed_user_id))
    assert mock_tg["state"].document_uploads, "resolved id should authorize /apk"


# ---------------------------------------------------------------------- #
# Qodo #8 — missing checksum must be refused, never "accepted as unverified"
# ---------------------------------------------------------------------- #
def test_missing_checksum_is_refused(tmp_path):
    """Qodo #8: an APK whose apk.json omits sha256 must be refused at runtime —
    not accepted because the expected value was absent."""
    apk_dir = str(tmp_path / "apk")
    os.makedirs(apk_dir, exist_ok=True)
    with open(os.path.join(apk_dir, "drhiro-bridge.apk"), "wb") as f:
        f.write(b"PK\x03\x04 bytes")
    # apk.json WITHOUT sha256.
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({"version": "0.1.0"}, f)
    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(ApkChecksumError):
        manager.validate()


def test_malformed_checksum_is_refused(tmp_path):
    """Qodo #8: a non-64-hex sha256 must be refused."""
    apk_dir = str(tmp_path / "apk")
    os.makedirs(apk_dir, exist_ok=True)
    with open(os.path.join(apk_dir, "drhiro-bridge.apk"), "wb") as f:
        f.write(b"PK\x03\x04 bytes")
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({"version": "0.1.0", "sha256": "short"}, f)
    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(ApkChecksumError):
        manager.validate()


def test_missing_checksum_blocks_upload(apk_bridge):
    """Qodo #8: /apk with no recorded checksum must not upload."""
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    os.makedirs(apk_dir, exist_ok=True)
    with open(os.path.join(apk_dir, "drhiro-bridge.apk"), "wb") as f:
        f.write(b"PK\x03\x04 bytes")
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({"version": "0.1.0"}, f)

    _process(b, mock_tg, "/apk", username="alice")
    assert not mock_tg["state"].document_uploads, "APK without checksum must not be uploaded"


# ---------------------------------------------------------------------- #
# Qodo #9 — upgrades must not resend a stale file_id bound to an old APK
# ---------------------------------------------------------------------- #
def test_upgrade_reuploads_instead_of_stale_file_id(apk_bridge):
    """Qodo #9: after the local APK is replaced (upgrade), send() must re-upload
    the new bytes and bind the new file_id — never resend the old file_id with a
    caption claiming the new version."""
    b, apk_dir, mock_tg, _ = apk_bridge["bridge"], apk_bridge["apk_dir"], apk_bridge["mock_tg"], apk_bridge["mock_tf"]
    mock_tg["state"].upload_file_id = "FILEID-V1"
    # First registration of v1.
    _write_apk(apk_dir, version="0.1.0", payload=b"PK\x03\x04 v1 bytes")
    _process(b, mock_tg, "/apk", username="alice")
    assert mock_tg["state"].document_uploads, "first /apk should upload v1"
    # Stored file_id is bound to v1's hash.
    v1_sha = b.apk.meta()["sha256"]
    assert b.apk._stored_file_id_for_hash(v1_sha) == "FILEID-V1"

    # Upgrade: replace the APK with v2 (new bytes + new apk.json hash).
    mock_tg["state"].upload_file_id = "FILEID-V2"
    _write_apk(apk_dir, version="0.1.1", payload=b"PK\x03\x04 v2 bytes")
    v2_sha = b.apk.meta()["sha256"]
    assert v2_sha != v1_sha

    # Next /apk must re-upload (not resend FILEID-V1), and rebind to V2.
    uploads_before = len(mock_tg["state"].document_uploads)
    _process(b, mock_tg, "/apk", username="alice")
    assert len(mock_tg["state"].document_uploads) == uploads_before + 1, \
        "upgrade must trigger a re-upload, not a stale resend"
    assert b.apk._stored_file_id_for_hash(v2_sha) == "FILEID-V2"


# ---------------------------------------------------------------------- #
# Qodo #7 — signing certificate verified against trusted signer
# ---------------------------------------------------------------------- #
def test_signer_cert_mismatch_is_refused(tmp_path, monkeypatch):
    """Qodo #7: when apk.json records a trusted signer fingerprint, an APK whose
    certificate does not match must be refused."""
    apk_dir = str(tmp_path / "apk")
    os.makedirs(apk_dir, exist_ok=True)
    data = b"PK\x03\x04 signed bytes"
    apk_path = os.path.join(apk_dir, "drhiro-bridge.apk")
    with open(apk_path, "wb") as f:
        f.write(data)
    sha = hashlib.sha256(data).hexdigest()
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({
            "version": "0.1.0",
            "sha256": sha,
            "signer_sha256": "a" * 64,  # trusted signer that the stub will NOT match
        }, f)

    # Stub apksigner to report a DIFFERENT cert fingerprint.
    stub = tmp_path / "apksigner"
    stub.write_text("#!/usr/bin/env bash\necho 'Signer #1 certificate SHA-256 digest: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("APKSIGNER", str(stub))

    manager = ApkManager(apk_dir, max_size_mb=1)
    with pytest.raises(ApkChecksumError):
        manager.validate()


def test_signer_cert_match_passes(tmp_path, monkeypatch):
    """Qodo #7 (positive): an APK whose certificate matches the trusted signer
    passes validation."""
    apk_dir = str(tmp_path / "apk")
    os.makedirs(apk_dir, exist_ok=True)
    data = b"PK\x03\x04 signed bytes"
    apk_path = os.path.join(apk_dir, "drhiro-bridge.apk")
    with open(apk_path, "wb") as f:
        f.write(data)
    sha = hashlib.sha256(data).hexdigest()
    trusted = "c" * 64
    with open(os.path.join(apk_dir, "apk.json"), "w") as f:
        json.dump({
            "version": "0.1.0",
            "sha256": sha,
            "signer_sha256": trusted,
        }, f)

    stub = tmp_path / "apksigner"
    stub.write_text(f"#!/usr/bin/env bash\necho 'Signer #1 certificate SHA-256 digest: {trusted}'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("APKSIGNER", str(stub))

    manager = ApkManager(apk_dir, max_size_mb=1)
    assert manager.validate()["ok"] is True
