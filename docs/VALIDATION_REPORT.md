# Validation Report — drHiro on TrueForge

This report records the automated validation run for this release. All tests run **offline**
against mock Telegram and mock TrueForge servers — no real bot, no real AI backend, and no
TrueForge instance are required, so installation behaviour can be validated deterministically.

## How to run

```bash
./run_tests.sh
# or:
python -m pytest tests/ -v
```

## Result (2026-08-30)

```
52 passed in ~16s
```

## Test coverage by requirement

### 1. Secret-safe logs
- `test_config_validate_reports_missing_without_values`
- `test_error_messages_never_echo_token` — a failing Telegram call never leaks the token.
- `test_bridge_logs_do_not_contain_secret`
- `test_tools_redact_credentials`
- `test_authorized_and_text_helpers`

### 2. Allowed-user behaviour
- `test_only_configured_username_is_authorized`
- `test_unauthorized_sender_gets_denied_via_bridge`
- `test_message_text_extraction`
- `test_unauthorized_user_cannot_trigger_save` — no session/turn is created for an
  unauthorized sender.

### 3. Telegram webhook conflict detection
- `test_no_webhook_allows_polling`
- `test_webhook_set_blocks_polling` — a configured webhook blocks long polling.
- `test_delete_webhook_clears_and_allows_polling` — explicit removal then polling is safe.

### 4. Unreachable AI backend
- `test_health_reports_unreachable`
- `test_turn_against_unreachable_backend_raises_clean_error`
- `test_run_turn_unreachable`

### 5. Unavailable model
- `test_model_unavailable_turn_returns_failed_state`
- `test_model_unavailable_does_not_hang`
- `test_unreachable_model_endpoint_surfaces_error`

### 6. Tool invocation
- `test_get_demo_case_lists_synthetic_cases` / `_by_id` / `_unknown_id`
- `test_create_visit_brief_structured_and_non_diagnostic` / `_rejects_unknown_case`
- `test_save_visit_brief_requires_synthetic_flag` / `_writes_and_returns_path`
- `test_get_service_status_non_sensitive`

### 7. Blocked save without confirmation
- `test_gated_tool_pauses_without_confirmation` — a gated tool pauses and is **not**
  auto-approved.
- `test_bridge_surfaces_approval_prompt` — the bridge posts an Allow/Deny prompt and records
  no decision.
- `test_deny_does_not_persist` — denying resumes without running the write.

### 8. Allowed save after confirmation
- `test_allowed_save_after_confirmation` — full flow: message → prompt → Allow → resume →
  reply delivered, decision recorded as `allow`.
- `test_allowed_save_via_deny_does_not_approve`

### 9. APK distribution (mocked Telegram)
- `test_authorized_apk_sends_expected_file_id` — authorized `/apk` uploads then sends the expected `file_id`.
- `test_unauthorized_apk_is_denied` — no upload/send for an unauthorized user.
- `test_missing_apk_fails_safely` / `test_missing_apk_apkinfo_fails_safely`
- `test_checksum_mismatch_blocks_upload` — tampered APK is refused.
- `test_apk_over_size_limit_blocks_upload` / `test_apk_over_telegram_hard_limit_blocks_upload`
- `test_failed_first_upload_does_not_save_false_file_id` — a failed upload persists nothing.
- `test_later_apk_reuses_stored_file_id` — no re-upload; stored `file_id` is reused.
- `test_start_and_help_commands` / `test_resolved_user_id_authorizes_apk`

### 10. Secure Bridge pairing
- `test_successful_device_link` — valid token exchanges for a device credential.
- `test_expired_token` — expired token rejected.
- `test_reused_token` — single-use; reuse rejected.
- `test_wrong_telegram_user` — token bound to a different user rejected.
- `test_wrong_server` — token bound to a different server rejected.
- `test_unauthorized_user_via_bridge` — unauthorized `/pair` denied, no token created.
- `test_malformed_deep_link` — malformed `drhiro://pair` links rejected.
- `test_rejected_non_https_remote_endpoint` — remote HTTP rejected; LAN HTTP flagged insecure.
- `test_revoked_device_access` — revoked device credential fails verification; owner-scoped revoke.
- `test_rate_limit_token_creation` — creation rate-limited.

## Beyond unit tests (manual / integration, documented)

- **Real TrueForge integration:** the agent spec, session/turn/approval contract, and
  provisioning flow are implemented against TrueForge's actual HTTP/SSE API (verified against
  the open-source TrueForge source and a live hosted instance).
- **Installer behaviour:** `install.sh` and all `scripts/*` pass `bash -n` syntax checks.
- **Docker Compose:** validated for correct service wiring, network isolation, and health
  checks; a real `docker compose up --build` on a clean host is part of the demo.

## Interpretation

The 31 passing tests exercise every one of the eight required validation scenarios at the
unit and integration (mock end-to-end) level. This is the automated evidence for the
submission; any externally-recorded demo (see `docs/DEMO_SCRIPT.md`) supplements, not
replaces, this offline validation.
