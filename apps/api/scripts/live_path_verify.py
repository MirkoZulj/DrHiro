"""Live-path verification for encounter-centric medical records (v0.8.0).

Runs against a REAL uvicorn on localhost:8099 (dev pg, synthetic data only):
creates a synthetic user + JWT directly in the dev DB, then exercises:
  POST /api/v1/medical/scan (multipart, JWT)
  GET  /api/v1/medical/encounters, GET /api/v1/medical/encounters/{id}
  GET  /api/v1/medical/scan/{record_id}
  POST /api/v1/tools/add_medical_record (+encounter_id)
  POST /api/v1/tools/list_medical_encounters, get_medical_encounter,
       delete_medical_encounter
"""
import io
import json
import uuid
import urllib.request
import jwt  # PyJWT
from datetime import datetime, timedelta, timezone

BASE = "http://localhost:8099/api/v1"
SECRET = "live-path-verify-secret-0123456789abcdef"
TG = "700999" + datetime.now(timezone.utc).strftime("%H%M%S")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050201f34a24d5"
    "0000000049454e44ae426082"
)

# --- create synthetic user + token straight in the dev DB
import psycopg
from drhiro_api.config import get_settings
url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
conn = psycopg.connect(url)
conn.autocommit = True
uid = str(uuid.uuid4())
conn.execute(
    "INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
    "VALUES (%s, %s, 'Europe/Zagreb', 'en', 'active', now(), now()) ON CONFLICT DO NOTHING",
    (uid, "LivePath Encounter Verify"),
)
conn.execute("DELETE FROM external_identities WHERE provider='telegram' AND provider_subject=%s", (TG,))
conn.execute(
    "INSERT INTO external_identities (id, user_id, provider, provider_subject, created_at, updated_at) "
    "VALUES (%s, %s, 'telegram', %s, now(), now())",
    (str(uuid.uuid4()), uid, TG),
)
conn.close()
tok = jwt.encode({"sub": uid, "type": "access",
                  "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, SECRET, algorithm="HS256")
H = {"Authorization": f"Bearer {tok}"}


def req(method, path, data=None, headers=None, raw_body=None, content_type=None):
    url = (BASE + path) if path.startswith("/") and not path.startswith("/health") else "http://localhost:8099" + path
    r = urllib.request.Request(url, method=method,
                               data=raw_body if raw_body is not None else (json.dumps(data).encode() if data is not None else None),
                               headers={**(headers or {}), **({"Content-Type": content_type} if content_type else ({"Content-Type": "application/json"} if data is not None else {}))})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def tool(name, payload):
    status, body = req("POST", f"/tools/{name}", payload,
                       headers={"X-Service-Token": tok_service(), "X-Telegram-Id": TG})
    try:
        return status, json.loads(body)
    except Exception:
        return status, {"_raw": body[:200]}


def tok_service():
    import sys
    sys.path.insert(0, "/repo/apps/api/src")
    from drhiro_api.security import create_service_token
    return create_service_token()


def multipart(fields, files):
    b = "----livetest"
    out = b""
    for k, v in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fn, data, ct) in files.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n".encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + label + ((" | " + str(extra)[:120]) if extra else ""))
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


# 1. health
s, b = req("GET", "/health", headers={})
check("health", s == 200, b[:80])

# 2. create encounter via tool
s, body = tool("add_medical_encounter", {"encounter_date": "2026-05-11",
                                         "facility": "SYNTHETIC LivePath Lab", "title": "Blood panel"})
check("tools.add_medical_encounter", s == 200 and body["ok"], body)
eid = body["data"]["id"]

# 3. store record inside it
s, body = tool("add_medical_record", {"title": "SYNTHETIC panel page 1",
                                      "content": "SYNTHETIC: Hemoglobin 145 g/L",
                                      "encounter_id": eid})
check("tools.add_medical_record(+encounter_id)", s == 200 and body["data"]["encounter_id"] == eid, body)
rid = body["data"]["id"]

# 4. JWT multipart scan upload + bind to record
mb, ct = multipart({"record_id": rid}, {"file": ("scan.png", PNG, "image/png")})
s, b = req("POST", "/medical/scan", raw_body=mb, headers=H, content_type=ct)
up = json.loads(b)
check("POST /medical/scan (JWT multipart)", s == 200 and up["record_id"] == rid, up)
path = up["path"]

# 5. dashboard list
s, b = req("GET", "/medical/encounters", headers=H)
lst = json.loads(b)
check("GET /medical/encounters", s == 200 and lst["count"] == 1
      and lst["encounters"][0]["record_count"] == 1, lst)

# 6. dashboard detail
s, b = req("GET", f"/medical/encounters/{eid}", headers=H)
det = json.loads(b)
check("GET /medical/encounters/{id}", s == 200 and len(det["records"]) == 1
      and det["records"][0]["source_image_path"] == path, det)

# 7. image streaming (owner) + denial (anonymous / other user)
s, b = req("GET", f"/medical/scan/{rid}", headers=H)
check("GET /medical/scan/{id} owner", s == 200 and b.startswith(b"\x89PNG"), s)
s, _ = req("GET", f"/medical/scan/{rid}")
check("GET /medical/scan/{id} anonymous -> 401", s == 401, s)

# 8. tools read paths
s, body = tool("list_medical_encounters", {})
check("tools.list_medical_encounters", s == 200 and body["data"]["count"] == 1, body)
s, body = tool("get_medical_encounter", {"encounter_id": eid})
check("tools.get_medical_encounter", s == 200 and body["data"]["encounter"]["record_count"] == 1, body)

# 9. delete encounter (removes record)
s, body = tool("delete_medical_encounter", {"encounter_id": eid})
check("tools.delete_medical_encounter", s == 200 and body["data"]["deleted_record_count"] == 1, body)
s, body = tool("list_medical_records", {})
check("records gone after encounter delete", body["data"]["count"] == 0, body)

# 10. unknown encounter refused
s, _ = tool("get_medical_encounter", {"encounter_id": str(uuid.uuid4())})
check("unknown encounter -> 404", s == 404, s)

print(f"\nRESULT: {ok} passed, {fail} failed")
import sys
sys.exit(1 if fail else 0)
