# Card 8 — Medical record wrong title: "Krvna kartica - zapis 2000" shown for Eye exam

Status: **reproduced at baseline** (`origin/main` @ `f88e154`, detached worktree
`/tmp/kim-baseline`, throwaway DB `kim_title_bug`, synthetic content only).
No prod edits, no prod data touched. Fix scope proposed for Jerry — **do not
implement here**.

## Root-cause hypothesis (confirmed at API level)

There is **no classifier, OCR, or filename derivation of titles anywhere in the
backend**. Every title is caller-supplied verbatim:

- `store_scan` (`routers/medical.py:48`) discards the uploaded filename and
  stores bytes as `<uuid>.<ext>` — filename can never become a title.
- `tool_add_medical_record` / `tool_add_medical_encounter` (`openclaw_tools.py:1072,1202`):
  `title = req.title.strip() or None` — pure pass-through.
- `ingest_medical_batch` (`openclaw_tools.py:1660-1691`) stamps the **same**
  `title` form field on **both** the encounter and the staged record
  (fallback `"Medical photo batch (n photos)"` if absent).
- `update_medical_record` (`openclaw_tools.py:1378`) updates **only the
  record's** title — the encounter title is frozen at staging time forever.
- `Records.tsx` renders the **encounter's** `title` as the card headline
  (lines 197, 250); the record's own title only appears inside the popup.

So the failure chain is deterministic: stale/wrong `title` at staging time
(e.g. the agent reusing a previous batch's document title from context, or
guessing before transcription) → encounter card permanently shows
"Krvna kartica - zapis 2000" → the agent later enriches the record to
"Eye exam" via `update_medical_record` (exactly as SKILL.md step 4 instructs:
"`title`/`recorded_at` as read from the document") → the card still shows the
blood-card title because nothing ever updates `encounter.title`.

The LLM choosing the stale title at staging is the trigger, but the
**structural bug is backend**: two independent title fields, one write path
that only updates one of them, and the UI displaying the one that never gets
corrected.

## Evidence

Repro run (baseline worktree, throwaway DB printed before any write):

```
THROWAWAY DB (resolved before any write): postgresql+psycopg://drhiro:drhiro@localhost:5435/kim_title_bug
ingest: 200 {... "encounter_id":"c1dce53d-...","record_id":"7b91d51b-...","photo_count":1 ...}
update: 200 {... "title":"Eye exam","content":"SYNTHETIC EYE EXAM\nVisual acuity: 1.0\nRefraction: -0.5 D" ...}
ENCOUNTER title (card title in Records.tsx): 'Krvna kartica - zapis 2000'
RECORD title: 'Eye exam'
BUG REPRODUCED: card shows stale blood-card title for Eye exam record
EXIT=0
```

Negative evidence (grepped at baseline): no `ocr`/`classif` references in
`medical.py`/`openclaw_tools.py` title paths (the only classifier import is
`ledger_classifier` for food logging); `filename` in scan paths is only the
Mi Fitness archive path handling. `_merge_placeholder_siblings` unions scans
only — it does not copy titles.

## Proposed fix scope (for Jerry)

1. `update_medical_record`: when `title` is provided and the record's
   encounter has no other records, propagate the title to `encounter.title`
   (or add an explicit `encounter_title` field). Smallest correct fix.
2. Consider: `ingest_medical_batch` should not accept a guessed `title` at
   staging — default to the generic batch title and let `update_medical_record`
   set the real one (flows through fix 1).
3. Regression test: stage batch with title A → enrich record with title B →
   assert encounter title == B. Synthetic content only.
4. Data check (owner-approved, read-only first): count encounters whose title
   matches a *different* record's title pattern — informational only, no
   prod writes without approval.

## Reproduction script

Full script (run inside a detached baseline worktree; requires the dev
Postgres on :5435 and an editable venv — `uv venv apps/api/.venv`, then
`uv pip install -e packages/health-schema -e packages/rule-engine
-e packages/nutrition-core -e packages/analytics-engine -e apps/api httpx pytest`):

```python
"""Card 8 repro (Kim QA, baseline origin/main, detached worktree).

Reproduces: 'Krvna kartica - zapis 2000' shown for an Eye exam.
Hypothesis under test: titles are caller-supplied verbatim (no OCR/classifier/
filename derivation in backend). ingest_medical_batch stamps the SAME title on
encounter + record; update_medical_record later changes ONLY the record title,
never the encounter title; Records.tsx renders encounter.title as the card
title. So a stale title at staging time permanently mislabels the card even
after the record is corrected to 'Eye exam'.
"""
import os, sys
DB = os.environ["DRHIRO_DATABASE_URL"]
print(f"THROWAWAY DB (resolved before any write): {DB}")
assert "kim_title_bug" in DB, "refusing non-throwaway DB"
os.environ.setdefault("DRHIRO_REDIS_URL", "redis://localhost:6382/0")
os.environ.setdefault("DRHIRO_JWT_SECRET", "test-secret-key-0123456789abcdefghijklmnopqrstuv")
os.environ.setdefault("DRHIRO_TELEGRAM_BOT_TOKEN", "123456:test")
os.environ["DRHIRO_MEDICAL_SCANS_DIR"] = "/tmp/kim-title-scans"

import psycopg
admin = psycopg.connect(DB.replace("postgresql+psycopg://", "postgresql://").rsplit("/", 1)[0] + "/postgres", autocommit=True)
admin.execute("DROP DATABASE IF EXISTS kim_title_bug")
admin.execute("CREATE DATABASE kim_title_bug")
admin.close()

from sqlalchemy import create_engine
from drhiro_api.db import Base
from drhiro_api.models import User, ExternalIdentity
from sqlalchemy.orm import sessionmaker
eng = create_engine(DB)
Base.metadata.create_all(eng)
S = sessionmaker(bind=eng)
db = S()
u = User(display_name="QA Title Bug", timezone="Europe/Zagreb"); db.add(u); db.flush()
db.add(ExternalIdentity(provider="telegram", provider_subject="700999888", user_id=u.id)); db.commit()

from drhiro_api.security import create_service_token
from fastapi.testclient import TestClient
from drhiro_api.main import app
H = {"X-Service-Token": create_service_token(), "X-Telegram-Id": "700999888"}
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-scan-bytes" * 10

with TestClient(app) as c:
    r = c.post("/api/v1/tools/ingest_medical_batch",
               files=[("files", ("eye.jpg", JPEG, "image/jpeg"))],
               data={"title": "Krvna kartica - zapis 2000", "encounter_date": "2026-05-11"},
               headers=H)
    print("ingest:", r.status_code, r.text[:300]); assert r.status_code == 200, r.text
    d = r.json()["data"]; eid, rid = d["encounter_id"], d["record_id"]

    r = c.post("/api/v1/tools/update_medical_record",
               json={"record_id": rid, "title": "Eye exam",
                     "content": "SYNTHETIC EYE EXAM\nVisual acuity: 1.0\nRefraction: -0.5 D"},
               headers=H)
    print("update:", r.status_code, r.text[:300]); assert r.status_code == 200, r.text

    r = c.post("/api/v1/tools/list_medical_encounters", json={"limit": 5}, headers=H)
    enc = r.json()["data"]["encounters"][0]
    print("ENCOUNTER title (card title in Records.tsx):", repr(enc["title"]))
    r = c.post("/api/v1/tools/get_medical_encounter", json={"encounter_id": eid}, headers=H)
    rec = r.json()["data"]["records"][0]
    print("RECORD title:", repr(rec["title"]))
    print("RECORD content head:", rec["content"][:60].replace("\n", " | "))

bug = enc["title"] == "Krvna kartica - zapis 2000" and rec["title"] == "Eye exam"
print("BUG REPRODUCED: card shows stale blood-card title for Eye exam record" if bug else "NOT REPRODUCED")
sys.exit(0 if bug else 1)
```
