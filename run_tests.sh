#!/usr/bin/env bash
# drHiro on TrueForge — run the full automated validation suite.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -d .venv ]]; then
  echo "[tests] creating .venv and installing deps..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q 'mcp<2' pydantic pytest pytest-asyncio httpx
else
  source .venv/bin/activate
fi

echo "[tests] running pytest..."
python -m pytest tests/ -v
