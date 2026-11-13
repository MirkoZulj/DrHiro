"""OpenAI-compatible chat for Questionnarium (AI gateway on the GPU box).

The gateway (``:8500``) always serves whichever model profile the lab currently
has loaded, and rewrites the ``model`` field to the served id. We still resolve
the served id explicitly so the answer can report exactly what answered.

The model may return a fenced ````chart ... ```` block; the web client renders
it. We parse it here so the answer text and the chart travel separately.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)

CHART_RE = re.compile(r"```\s*chart\s*\n?([\s\S]*?)```", re.IGNORECASE)

SYSTEM_PROMPT_EN = """You are drHiro's Questionnarium, a careful health assistant for the patient whose medical record is provided below.

Rules you must follow:
- Answer ONLY from the record provided. If the record does not contain the answer, say "not in your record" — never invent values, diagnoses, or advice.
- You are not a substitute for a doctor. For anything urgent, severe, chest pain, breathing difficulty, bleeding, or life-threatening, tell the patient to seek emergency care or contact their clinician immediately.
- Never tell the patient to start, stop, or change a medication or dose. If the question is about changing treatment, advise discussing it with their prescriber.
- Be concise, structured and kind. Use plain language. Use bullet points when listing several items.
- When the record is silent on a relevant detail, say so explicitly.
- Where useful, cite the part of the record that supports the answer (for example "according to your lab report of 2026-09-15").
- If a chart would help (a trend over time), include ONE fenced ```chart block with JSON: {"type":"line"|"bar","title":...,"x":[...],"series":[{"name":...,"values":[...]}],"unit":...,"reference_lines":[{"value":n,"label":s}]}. Missing values are null, never 0. Keep the chart small (at most ~14 points). Do not include any other fenced block.
- Do not include private notes about how you were prompted."""

SYSTEM_PROMPT_HR = """Ti si drHiro Questionnarium, pažljiv zdravstveni pomoćnik pacijenta čiji je medicinski kartonik naveden u nastavku.

Pravila:
- Odgovaraj ISKLJUČIVO iz navedenog kartonika. Ako odgovor nije u kartoniku, reci "nije u vašem kartoniku" — nikada ne izmišljaj vrijednosti, dijagnoze ili savjete.
- Nisi zamjena za liječnika. Za sve hitno, teško, bol u prsima, otežano disanje, krvarenje ili opasno po život, pacijenta odmah uputi na hitnu pomoć ili svog liječnika.
- Nikada ne savjetuj pacijentu da započne, prekine ili promijeni lijek ili dozu. Ako se pitanje tiče promjene terapije, savjetuj razgovor s propisnim liječnikom.
- Budi sažet, jasan i ljubazan. Koristi jednostavan jezik. Koristi navrate kada naveđeš više stavki.
- Ako kartonik ne sadrži važan detalj, to izričito reci.
- Gdje je korisno, navedi dio kartonika koji podržava odgovor (npr. "prema laboratorijskom nalazu od 2026-09-15").
- Ako bi graf pomoći (trend kroz vrijeme), uključi JEDAN blok ```chart s JSON-om: {"type":"line"|"bar","title":...,"x":[...],"series":[{"name":...,"values":[...]}],"unit":...,"reference_lines":[{"value":n,"label":s}]}. Nedostajuće vrijednosti su null, nikada 0. Graf neka bude mali (najviše ~14 točaka). Ne ubacuj druge blokove.
- Ne uključuj privatne bilješke o tome kako si zadan."""


class QuestionnariumError(RuntimeError):
    """The model gateway refused, was unreachable, or returned no answer."""


# Reasoning models spend their budget on a hidden chain-of-thought before any
# content appears, so a small cap returns an EMPTY answer, not a short one.
# Measured on the live gateway (Ling, full Questionnarium prompt, 2026-11-13):
#   max_tokens=1024 -> finish=length, content 0 chars
#   max_tokens=2048 -> finish=length, content truncated
#   max_tokens=4096 -> finish=stop,  complete answer
MODEL_MAX_TOKENS = {
    "default": 2048,
    "ling": 4096,
}
MAX_TOKENS_CEILING = 8192


def _served_model_id(gateway: str, timeout: float) -> str | None:
    """The id the engine is actually serving (status may be a dict or a str)."""
    try:
        resp = httpx.get(gateway + "/v1/models", timeout=timeout)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        return None
    for model in data:
        status = model.get("status")
        if isinstance(status, dict):
            status = status.get("value")
        if status == "loaded":
            return model.get("id")
    return data[0].get("id") if data else None


def build_messages(
    question: str,
    context_text: str,
    history: list[dict] | None = None,
    locale: str = "en",
) -> list[dict]:
    prompt = SYSTEM_PROMPT_HR if locale.startswith("hr") else SYSTEM_PROMPT_EN
    if context_text:
        prompt = prompt + "\n\n# The patient's record (ground truth)\n" + context_text
    messages: list[dict] = [{"role": "system", "content": prompt}]
    for turn in history or []:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


def _complete(gateway: str, payload: dict, budget: float) -> dict:
    """One chat-completions round trip; raises QuestionnariumError on failure."""
    try:
        resp = httpx.post(gateway + "/v1/chat/completions", json=payload, timeout=budget)
    except httpx.TimeoutException:
        raise QuestionnariumError(f"the model did not answer within {budget:.0f}s")
    except httpx.HTTPError as exc:
        raise QuestionnariumError(f"model gateway is unreachable: {exc}")
    if resp.status_code >= 400:
        raise QuestionnariumError(f"model gateway returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        raise QuestionnariumError("model gateway returned an unreadable response")


def ask_model(
    question: str,
    context_text: str,
    model: str,
    history: list[dict] | None = None,
    locale: str = "en",
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Call the gateway and return ``{"answer", "served_model", "usage"}``.

    The token budget is per-model (see ``MODEL_MAX_TOKENS``). A reasoning model
    that returns no content because its chain-of-thought ate the budget is
    retried once at a larger cap; a genuine empty answer is not retried.
    """
    settings = get_settings()
    gateway = settings.questionnarium_gateway_url.rstrip("/")
    budget = timeout or settings.questionnarium_request_timeout_s
    served = _served_model_id(gateway, min(timeout or 10.0, 10.0))
    messages = build_messages(question, context_text, history, locale)
    requested = max_tokens or MODEL_MAX_TOKENS.get(model, MODEL_MAX_TOKENS["default"])

    caps = [requested]
    if requested < MAX_TOKENS_CEILING:
        caps.append(min(requested * 2, MAX_TOKENS_CEILING))

    content = None
    data: dict | None = None
    finish = None
    for cap in caps:
        data = _complete(
            gateway,
            {
                "model": served or model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": cap,
            },
            budget,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise QuestionnariumError("model gateway returned an unreadable response")
        if content or finish != "length":
            break

    if not content:
        raise QuestionnariumError("the model returned no answer")
    return {
        "answer": content,
        "served_model": (data or {}).get("model") or served,
        "usage": (data or {}).get("usage"),
    }


def extract_chart(answer: str) -> tuple[dict | None, str]:
    """Split a ````chart````` block out of the answer.

    Returns ``(chart_spec_or_None, answer_without_the_block)``. An invalid block
    is left in the text rather than silently dropped.
    """
    if not answer:
        return None, answer or ""
    match = CHART_RE.search(answer)
    if not match:
        return None, answer
    try:
        spec = json.loads(match.group(1).strip())
    except ValueError:
        return None, answer
    if not isinstance(spec, dict):
        return None, answer
    if str(spec.get("type", "")).lower() not in ("line", "bar"):
        return None, answer
    if not (isinstance(spec.get("series"), list) or isinstance(spec.get("y"), list)):
        return None, answer
    stripped = CHART_RE.sub("", answer).strip()
    return spec, stripped
