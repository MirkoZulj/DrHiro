"""drHiro analytics engine — deterministic facts only (spec §3.2).

No DB, no FastAPI, no LLM in decisions. D3 (owner 2026-11-03): every output is
numeric-guarded, wording is banned-list-checked at the renderer level, and the
thresholds are a versioned constant (THRESHOLDS_VERSION).
"""

ENGINE_VERSION = "1.0.0"

from drhiro_analytics.run import analyse  # noqa: E402,F401
from drhiro_analytics.thresholds import THRESHOLDS_VERSION  # noqa: E402,F401
