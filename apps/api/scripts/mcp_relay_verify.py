"""End-to-end MCP media-relay verification.

Drives the REAL openclaw/mcp/drhiro-mcp-server.js over stdio against a live
API: plants a synthetic photo in a fake gateway media dir, then calls
add_medical_encounter -> add_medical_record(attach_latest_media=true) through
the MCP protocol and verifies the scan was stored server-side and bound to the
record. SYNTHETIC data only.
"""
import json
import os
import subprocess
import sys
import time
import uuid
import datetime as dt

SECRET = sys.argv[1]
BASE = "http://localhost:8099/api/v1"
sys.path.insert(0, "/repo/apps/api/src")
os.environ.setdefault("DRHIRO_JWT_SECRET", SECRET)
from drhiro_api.security import create_service_token
from drhiro_api.config import get_settings
import psycopg

TG = "700555" + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
uid = str(uuid.uuid4())
url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
conn = psycopg.connect(url); conn.autocommit = True
conn.execute("DELETE FROM external_identities WHERE provider='telegram' AND provider_subject=%s", (TG,))
conn.execute(
    "INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
    "VALUES (%s,'MCP Relay Verify','Europe/Zagreb','en','active',now(),now())", (uid,))
conn.execute(
    "INSERT INTO external_identities (id,user_id,provider,provider_subject,created_at,updated_at) "
    "VALUES (%s,%s,'telegram',%s,now(),now())", (str(uuid.uuid4()), uid, TG))
conn.close()

# fake gateway inbound media with a synthetic PNG
media_dir = "/tmp/medialive"
os.makedirs(media_dir, exist_ok=True)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050201f34a24d5"
    "0000000049454e44ae426082")
open(os.path.join(media_dir, "relay-test.png"), "wb").write(png)

env = dict(os.environ,
           DRHIRO_MCP_API=BASE,
           DRHIRO_OPENCLAW_SERVICE_TOKEN=create_service_token(),
           DRHIRO_TELEGRAM_ID=TG,
           DRHIRO_MEDIA_DIR=media_dir,
           DRHIRO_MEDIA_CONSUMED_FILE="/tmp/mcptmp/consumed.json",
           DRHIRO_INBOUND_ID_FILE="/tmp/mcptmp/msgid.json")
os.makedirs("/tmp/mcptmp", exist_ok=True)

proc = subprocess.Popen(["node", "/repo/openclaw/mcp/drhiro-mcp-server.js"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)

def rpc(name, args, req_id=[0]):
    req_id[0] += 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id[0],
                                 "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed")
        msg = json.loads(line)
        if msg.get("id") == req_id[0]:
            return msg

def unwrap(r):
    content = r["result"]["content"]
    return json.loads(content[0]["text"])

proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {}}) + "\n")
proc.stdin.flush()
time.sleep(0.5)
proc.stdout.readline()

ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + label + ((" | " + str(extra)[:160]) if extra else ""))
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

r = unwrap(rpc("add_medical_encounter", {"telegram_id": TG, "encounter_date": "2026-05-11",
                                         "facility": "SYNTHETIC Relay Lab"}))
check("mcp add_medical_encounter", r.get("ok") is True, r)
eid = r["data"]["id"]

r = unwrap(rpc("add_medical_record", {"telegram_id": TG,
                                      "title": "SYNTHETIC relay page",
                                      "content": "SYNTHETIC: glucose 5.2 mmol/L",
                                      "encounter_id": eid,
                                      "attach_latest_media": True}))
check("mcp add_medical_record(+attach_latest_media)", r.get("ok") is True, r)
scan = (r.get("data") or {}).get("scan") or {}
check("scan stored via relay", scan.get("path", "").endswith(".png") and scan.get("record_id"), scan)
rid = scan.get("record_id")

r = unwrap(rpc("get_medical_encounter", {"telegram_id": TG, "encounter_id": eid}))
recs = r["data"]["records"]
check("record bound with source_image_path", len(recs) == 1 and recs[0]["source_image_path"] == scan.get("path"), recs)

# the media file was consumed: a second attach finds nothing
r = unwrap(rpc("add_medical_record", {"telegram_id": TG, "content": "SYNTHETIC second",
                                      "attach_latest_media": True}))
check("no double-attach of consumed media", (r.get("data") or {}).get("scan") is None, r)

# bytes preserved
stored = open("/tmp/scans-live/" + scan["path"], "rb").read()
check("stored bytes == planted bytes", stored == png)

proc.stdin.close(); proc.kill()
print(f"\nRESULT: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
