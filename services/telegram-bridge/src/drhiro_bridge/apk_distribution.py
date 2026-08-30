"""APK distribution for the drHiro Bridge Android companion app.

Manages the signed drHiro Bridge APK as a Telegram document:
  - metadata (version, SHA-256, size) read from a protected JSON sidecar,
  - checksum + size validation (APKs over the Telegram upload limit are refused),
  - persistent `file_id` so later /apk calls resend by file_id, not re-upload,
  - first upload on setup stores the Telegram-returned file_id securely,
  - delivery status reported WITHOUT revealing secrets.

The APK artifact itself is placed in a protected directory by the installer /
release workflow. It must contain NO secrets: no bot token, AI credential,
server URL, user data, health data, signing key, or private config is ever
compiled into the APK. The bridge's own connection target (TrueForge) is
configured via environment, never hardcoded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("drhiro_bridge.apk")

DEFAULT_MAX_SIZE_MB = 45
_TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB Bot API hard cap


class ApkError(RuntimeError):
    pass


class ApkTooLargeError(ApkError):
    pass


class ApkChecksumError(ApkError):
    pass


class ApkManager:
    def __init__(
        self,
        apk_dir: str | os.PathLike,
        max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    ) -> None:
        self.apk_dir = Path(apk_dir)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.apk_path = self.apk_dir / "drhiro-bridge.apk"
        self.meta_path = self.apk_dir / "apk.json"

    # ------------------------------------------------------------------ #
    # Metadata
    # ------------------------------------------------------------------ #
    def _load_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("apk.json unreadable; treating as empty")
            return {}

    def _save_meta(self, meta: dict) -> None:
        self.apk_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.meta_path)

    def meta(self) -> dict:
        return self._load_meta()

    def stored_file_id(self) -> str | None:
        m = self._load_meta()
        fid = m.get("file_id")
        return str(fid) if fid else None

    def _stored_file_id_for_hash(self, current_sha256: str) -> str | None:
        """Qodo #9: return the stored file_id ONLY if it was bound to the current
        APK hash. If the local APK was replaced (upgrade), the old file_id refers
        to stale bytes and must not be reused."""
        m = self._load_meta()
        if str(m.get("sha256", "")).lower() != current_sha256.lower():
            return None
        fid = m.get("file_id")
        return str(fid) if fid else None

    def set_file_id(self, file_id: str, apk_sha256: str) -> None:
        m = self._load_meta()
        m["file_id"] = file_id
        # Qodo #9: bind the file_id to the exact APK it was uploaded from, so an
        # upgrade invalidates the stale id instead of resending old bytes.
        m["sha256"] = apk_sha256
        m["file_id_set_at"] = __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()
        )
        self._save_meta(m)

    # ------------------------------------------------------------------ #
    # Checksum + size
    # ------------------------------------------------------------------ #
    def compute_sha256(self, path: Path | None = None) -> str:
        p = path or self.apk_path
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def validate(self, path: Path | None = None) -> dict:
        """Validate the APK exists, is under the size limit, matches the recorded
        SHA-256 (REQUIRED), and — when the trusted signer fingerprint is
        configured — its Android signing certificate.

        Qodo #7/#8: a missing or malformed checksum is REFUSED (never "accepted
        because the expected value was absent"); a substituted APK that fails the
        trusted-signer check is refused. Returns {version, size, sha256, ok}.
        """
        p = path or self.apk_path
        if not p.exists():
            raise ApkError("APK file not found — place drhiro-bridge.apk in the APK dir first.")
        size = p.stat().st_size
        if size > self.max_size_bytes:
            raise ApkTooLargeError(
                f"APK is {size} bytes (> {self.max_size_bytes}) — over the configured "
                f"size limit ({self.max_size_bytes // (1024*1024)} MB). Refusing to serve."
            )
        if size > _TELEGRAM_UPLOAD_LIMIT:
            raise ApkTooLargeError(
                "APK exceeds the Telegram Bot API 50 MB upload limit. Refusing to serve."
            )

        meta = self._load_meta()
        expected = meta.get("sha256")
        if not expected or not isinstance(expected, str) or len(str(expected)) != 64:
            raise ApkChecksumError(
                "apk.json is missing a valid sha256 — refusing to serve an unverified "
                "APK. Regenerate the metadata with the signed release checksum."
            )
        actual = self.compute_sha256(p)
        if actual.lower() != str(expected).lower():
            raise ApkChecksumError(
                "APK SHA-256 mismatch — refusing to serve a tampered artifact."
            )

        # Qodo #7: verify the Android signing certificate against the trusted
        # signer fingerprint, when one is configured in apk.json.
        trusted_signer = meta.get("signer_sha256")
        if trusted_signer:
            cert = self._verify_signing_cert(p)
            if not cert or str(cert).lower() != str(trusted_signer).lower():
                raise ApkChecksumError(
                    "APK signing certificate does not match the trusted signer — "
                    "refusing to serve a substituted artifact."
                )

        version = meta.get("version", "unknown")
        return {
            "ok": True,
            "path": str(p),
            "version": version,
            "size": size,
            "size_mb": round(size / (1024 * 1024), 2),
            "sha256": actual,
            "android_requirement": meta.get("android_requirement", "Android 8.0+"),
        }

    def _verify_signing_cert(self, path: Path) -> str | None:
        """Return the first signer's certificate SHA-256 fingerprint using
        apksigner, or None if apksigner is unavailable. Used to authenticate the
        Android signing identity (Qodo #7)."""
        import subprocess

        candidates = (
            os.environ.get("APKSIGNER"),
            "/usr/bin/apksigner",
            "/usr/lib/android-sdk/build-tools/current/apksigner",
            "/android-sdk/build-tools/36.0.0/apksigner",
        )
        apksigner = next((c for c in candidates if c and os.path.exists(c)), None)
        if not apksigner:
            return None
        try:
            out = subprocess.run(
                [apksigner, "verify", "--print-certs", str(path)],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # Lines look like: "Signer #1 certificate SHA-256 digest: <hex>"
        for line in (out.stdout or "").splitlines():
            low = line.lower()
            if "certificate sha-256 digest" in low:
                return line.split(":", 1)[-1].strip()
        return None

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def register(self, tg, force_upload: bool = True) -> str:
        """Upload the APK to Telegram as a document and persist the returned
        file_id bound to the current APK hash. Returns the file_id. Raises on any
        failure; a failed upload never persists a file_id."""
        info = self.validate()
        file_id = tg.send_document_file(
            info["path"], filename="drhiro-bridge.apk", caption=self._caption(info)
        )
        self.set_file_id(file_id, info["sha256"])
        log.info("APK registered with Telegram; file_id stored.")
        return file_id

    def send(self, tg, chat_id: int) -> str:
        """Send the APK to the chat.

        Qodo #9: reuse the stored file_id ONLY if it is bound to the current APK
        hash. After an upgrade the old file_id refers to stale bytes, so we
        re-upload and bind the new file_id instead of sending an old APK with a
        caption claiming the new version.
        """
        info = self.validate()
        fid = self._stored_file_id_for_hash(info["sha256"])
        if fid:
            tg.send_document_file_id(chat_id, fid, caption=self._caption(info))
            return f"sent by file_id (v{info['version']})"
        # Stale/missing file_id or APK upgraded: re-upload and bind.
        fid = self.register(tg)
        tg.send_document_file_id(chat_id, fid, caption=self._caption(info))
        return f"uploaded and sent (v{info['version']})"

    def _caption(self, info: dict) -> str:
        v = info["version"]
        return (
            f"drHiro Bridge v{v}\n"
            "This is the official signed Android companion APK from your "
            "self-hosted drHiro server.\n"
            f"SHA-256: {info['sha256']}\n"
            "Install:\n"
            "Download the attached APK.\n"
            "Open it from Telegram or your Downloads folder.\n"
            "If Android asks, allow installation from this source.\n"
            "Open drHiro Bridge.\n"
            "Only install APK files sent by your own authorized drHiro bot."
        )

    def status(self) -> dict:
        """Non-sensitive status for /status. Never reveals secrets."""
        try:
            info = self.validate()
        except ApkError as e:
            return {"apk": "missing/invalid", "detail": str(e)}
        m = self._load_meta()
        return {
            "apk": "ready",
            "version": info["version"],
            "size_mb": info["size_mb"],
            "sha256": info["sha256"],
            "registered": bool(m.get("file_id")),
        }
