# drHiro — Phase 0 Discovery Checklist

Fill in the blanks. This is the exit criterion for Phase 0: a recorded
matrix of source → metric → Health Connect availability for both users.
Without it, the Android bridge and OMRON integration are guesswork.

Status: IN PROGRESS — answers recorded 2026-08-10 (user provided)

---

## A. Devices (need physical access / the phones in hand)

### A1. Xiaomi Mi Band — User A
- [x] Exact model: **Mi Smart Band 9** (assumed both users)
- [x] Phone model + Android version: **Xiaomi 11T Pro / Android 14 (build UKQ1.231207.02)**
- [ ] Mi Fitness app version: __________
- [ ] Does Mi Fitness offer "Health Connect" sync in settings?
      YES / NO / CANNOT FIND  → **user to verify on phone**
- [ ] If YES: which record types appear as available to share? __________

### A2. Xiaomi Mi Band — User B
- [x] Exact model: **Mi Smart Band 9** (assumed both users)
- [x] Phone model + Android version: **Samsung Galaxy S22 / Android (version to confirm)**
- [ ] Mi Fitness app version: __________
- [ ] Does Mi Fitness offer "Health Connect" sync? YES / NO / CANNOT FIND
- [ ] If YES: which record types appear as available to share? __________

### A3. OMRON blood pressure device — User A
- [x] Exact model: **OMRON BP5350** (Bluetooth, OMRON Connect app)
- [x] Country/region: **USA**
- [ ] OMRON Connect app version: __________
- [x] After a measurement, does the BP record appear in Health Connect?
      **NO — CONFIRMED NOT AVAILABLE (2026-07-30 evidence).** US OMRON Connect syncs with
      Apple HealthKit, Google Fit, Samsung Health — NO Health Connect integration, and
      Google Fit is being retired end of 2026. → BP must use **Route D (manual `/bp` +
      photo OCR with confirmation)** as primary path; Route A (Health Connect) is blocked.
- [ ] Does the device have two user slots (User1/User2)? YES / NO / single-user

### A4. OMRON blood pressure device — User B
- [x] Exact model: **same BP5350 or second unit — confirm**
- [x] Country/region: USA
- [x] Health Connect write: **NO (same app limitation as A3)**

### A5. Phones
- [x] User A phone: **Xiaomi 11T Pro, Android 14**
- [x] User B phone: **Samsung Galaxy S22, Android (confirm version)**
- [ ] Health Connect app installed on both? YES / NO / N/A

---

## B. Existing infrastructure

### B1. Existing drHiro bot
- [x] Old prototype at /home/mirko/projects/DrHiro — running anywhere? **Not running (shouldn't be)**
- [ ] Telegram bot: create a NEW bot via @BotFather? **assumed yes — confirm**
- [ ] WhatsApp channel required? **YES — user wants the app to work via Telegram AND WhatsApp on any Android**

### B2. VPS / hosting
- [x] VPS provider: **Contabo (<vps-host>)**
- [x] Domain: **use VPS IP / existing hostname vmi3413468.contaboserver.net** — NOTE: Telegram webhooks
      require HTTPS; Let's Encrypt will NOT issue certs for a bare IP. Options: reuse Contabo
      hostname, or Cloudflare tunnel (already in use on Pi). **decision needed**
- [ ] Docker installed on VPS? **user doesn't know — I will check**
- [ ] Existing services that must not break: decksmith:8300, honcho:8000, llm-wiki:8083 (PM2)

### B3. Food photos / vision model (Phase 5 decision, affects architecture)
- [x] Cloud AI for meal photos? **NO**
- [x] Self-hosted vision: **Qwen on the Windows box (100.119.202.68:8000)** — photos stay self-hosted

---

## C. Product decisions

### C1. Scope
- [x] **Public-ready eventually** — design for multi-user + GDPR from day one;
      legal review required before external users beyond household.

### C2. Diet objective(s)
- [x] **User-defined** (no single fixed objective; goals are per-user configurable)

### C3. Languages
- [x] **English** (bot + UI)

### C4. Timezone(s)
- [x] **Europe/Zagreb** (confirmed correct)

---

## D. Phase 0 proof-of-concept (Section 22) — to run on the phones

1. [ ] Enable Mi Fitness → Health Connect on BOTH phones.
2. [ ] Note which permissions each phone offers (screenshot the permission list).
3. [ ] Install the minimal Android Bridge build on each phone.
4. [ ] Pair each phone with the drHiro API (device-code flow).
5. [ ] Import only steps, heart rate, sleep, weight, blood pressure.
6. [ ] Verify each user's data stays separate for 7 days (check via API).
7. [ ] OMRON behavior test: measure BP with BP5350, check if a Health Connect record appears,
      or whether OMRON API / manual / photo-OCR fallback is required. **CONFIRMED: US OMRON app
      has NO Health Connect write (HealthKit/Google Fit/Samsung Health only, Google Fit retiring).
      Plan for Route D (manual `/bp` + photo OCR with confirmation) as the primary BP path.**
8. [ ] Record the final matrix below.

---

## E. Source → Metric → Health Connect availability matrix (EXIT CRITERION)

| Source | Metric | Health Connect available? | Notes |
|--------|--------|---------------------------|-------|
| Mi Band 9 A | Steps | TBD | Xiaomi 11T Pro |
| Mi Band 9 A | Heart rate | TBD | |
| Mi Band 9 A | Sleep | TBD | |
| Mi Band 9 A | Weight | TBD | band has no scale — likely manual |
| Mi Band 9 A | SpO2 | TBD | Mi Band 9 has SpO2 sensor |
| Mi Band 9 B | Steps | TBD | Samsung S22 |
| Mi Band 9 B | Heart rate | TBD | |
| Mi Band 9 B | Sleep | TBD | |
| OMRON BP5350 A | Blood pressure | **NO (confirmed)** | US app: HealthKit/Google Fit/Samsung Health only; use Route D manual+OCR |
| OMRON BP5350 B | Blood pressure | **NO (confirmed)** | |
| Manual | Weight | n/a | |

**Phase 0 exit criterion: this matrix is complete and recorded.**

---

## G. Answers that change the architecture (2026-08-10)

1. **WhatsApp as a first-class channel** — the user-facing assistant must work via
   Telegram AND WhatsApp on any Android. OpenClaw gateway must add the WhatsApp
   adapter; the bot commands contract stays identical (same drHiro tools).
2. **Public-ready posture** — household is the test bed, but schema, consent, audit,
   and security must hold for external users from the start. GDPR legal review
   gated before public launch.
3. **Vision is self-hosted (Qwen on Windows box)** — no meal photos to cloud AI.
   The vision worker must call the Windows Qwen endpoint (100.119.202.68:8000),
   which has known constraints: grammar broken, use_grammar=false, enable_thinking=false.
4. **OMRON US app risk — CONFIRMED**: the BP5350 (US) app has NO Health Connect integration
   (HealthKit / Google Fit / Samsung Health only; Google Fit retiring end of 2026). BP must use
   **Route D (manual `/bp` + photo OCR with confirmation)** as the primary path. Route A (Health
   Connect) is blocked; Route B (OMRON partner API) is a commercial option if automatic BP sync
   becomes essential.
5. **No domain (yet)** — VPS IP/hostname only. HTTPS for Telegram webhooks needs
   either the Contabo hostname or a Cloudflare tunnel. Decision deferred.

*Last updated: 2026-08-10*
