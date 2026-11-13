"""Strata model-lab client — profile status and switching for Questionnarium.

The lab (``:18001``) owns the single LLM slot on the GPU box. A switch is
GLOBAL (it stops the incumbent model and loads the requested one) and can take
minutes on a cold load, so callers must be prepared to wait. The lab answers
``409`` while another switch is in progress; we wait for that switch to settle
and retry rather than failing the patient's request.

Chat completions themselves go through the OpenAI-compatible gateway
(``:8500``), which always serves whichever profile the lab currently has loaded.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)

# Public model id -> lab profile. "default" is the Qwen profile, "ling" the
# healthcare MoE served by llama.cpp on the same slot.
MODEL_PROFILES = {
    "default": "single-256k-dflash2",
    "ling": "ling-sante-iq3xs",
}

# The Command Center can switch the lab to any of these directly; they all
# serve the same Qwen model family, so they count as "default".
QWEN_PROFILES = {
    "single-256k-dflash2",
    "single-128k-dflash2",
    "dual-2x128k-mtp",
    "dual-2x64k-mtp",
}
LING_PROFILES = {"ling-sante-iq3xs"}

MODEL_LABELS = {
    "default": "Default (Qwen 3.8 Flash Next)",
    "ling": "Ling 3.0 Flash Sante (healthcare)",
}
MODEL_NOTES = {
    "default": "Fast general-purpose model.",
    "ling": "Healthcare-specialised model; slower, first request may load it.",
}


class LabError(RuntimeError):
    """The model lab is unreachable, refused the token, or failed to load."""


def lab_token() -> str:
    """DRHIRO_LAB_TOKEN, then settings, then the lab's token file."""
    token = os.environ.get("DRHIRO_LAB_TOKEN", "").strip()
    if token:
        return token
    settings = get_settings()
    if settings.questionnarium_lab_token:
        return settings.questionnarium_lab_token.strip()
    try:
        with open(settings.questionnarium_lab_token_file) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def model_from_profile(profile: str | None) -> str | None:
    if not profile:
        return None
    if profile in LING_PROFILES:
        return "ling"
    if profile in QWEN_PROFILES:
        return "default"
    return None


def profile_health(health: dict | None) -> bool:
    """Strata reports ``{"loaded": true}``; llama.cpp ``{"status": "ok"}``."""
    if not health:
        return False
    return bool(health.get("loaded") or health.get("status") == "ok")


def lab_status(timeout: float | None = None) -> dict:
    settings = get_settings()
    resp = httpx.get(
        settings.questionnarium_lab_url.rstrip("/") + "/status",
        timeout=timeout or 8.0,
    )
    resp.raise_for_status()
    return resp.json()


def lab_profiles(timeout: float | None = None) -> list[dict]:
    settings = get_settings()
    resp = httpx.get(
        settings.questionnarium_lab_url.rstrip("/") + "/profiles",
        timeout=timeout or 8.0,
    )
    resp.raise_for_status()
    return resp.json().get("profiles", [])


def _wait_until_healthy(deadline: float) -> None:
    """Poll the lab until the slot reports a loaded model (or the deadline)."""
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            status = lab_status(timeout=6)
        except Exception:  # noqa: BLE001 - transient during a restart
            continue
        if profile_health(status.get("health")):
            return


def lab_switch(profile: str, timeout: float | None = None) -> dict:
    """Switch the shared slot to ``profile`` and wait until it reports loaded."""
    settings = get_settings()
    token = lab_token()
    if not token:
        raise LabError("lab token is not configured (DRHIRO_LAB_TOKEN)")
    url = settings.questionnarium_lab_url.rstrip("/") + "/switch/" + profile
    budget = timeout or settings.questionnarium_switch_timeout_s
    deadline = time.monotonic() + budget

    for attempt in range(1, 21):
        if time.monotonic() >= deadline:
            break
        try:
            resp = httpx.post(url, headers={"X-Lab-Token": token}, timeout=max(5.0, deadline - time.monotonic()))
        except httpx.TimeoutException:
            raise LabError(f"timed out after {budget:.0f}s waiting for '{profile}' to load")
        except httpx.HTTPError as exc:
            raise LabError(f"lab is unreachable: {exc}")

        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data
            raise LabError(data.get("error") or "lab switch failed")
        if resp.status_code == 409 and time.monotonic() < deadline:
            # Another switch owns the slot; wait for it to finish, then retry.
            log.info("lab switch busy; waiting for the in-progress switch to settle")
            _wait_until_healthy(deadline)
            continue
        if resp.status_code == 403:
            raise LabError("lab token missing or invalid")
        if resp.status_code == 404:
            raise LabError(f"unknown model profile: {profile}")
        if resp.status_code == 504:
            raise LabError(f"model '{profile}' did not report loaded within the lab's budget")
        raise LabError(f"lab switch failed: HTTP {resp.status_code} {resp.text[:200]}")

    raise LabError(f"could not switch to '{profile}' within {budget:.0f}s")


def ensure_model(model: str) -> dict:
    """Make ``model`` the active lab profile, switching only when necessary.

    Returns ``{"profile", "switched", "current"}``.
    """
    if model not in MODEL_PROFILES:
        raise LabError(f"unknown model: {model}")
    profile = MODEL_PROFILES[model]
    try:
        status = lab_status()
    except Exception as exc:  # noqa: BLE001 - surfaced as a 503 to the patient
        raise LabError(f"model lab is unreachable: {exc}")
    current = status.get("current")
    if model_from_profile(current) == model and profile_health(status.get("health")):
        return {"profile": profile, "switched": False, "current": current}
    result = lab_switch(profile)
    return {"profile": profile, "switched": True, "current": profile, **result}
