-- Trusted-side tables for the isolated ingress vertical slice.
--
-- These live in the TRUSTED database, reachable only from the `trusted` network.
-- The T1 application tables themselves (consumption_operations, meals, ...) are
-- created by the real Alembic chain, not here.
--
-- `telegram_receipts` is the durable receipt: written by the single trusted
-- consumer before any processing, unique per (bot_id, chat_id, message_id), so a
-- redelivered update can never produce a second consumption.
--
-- `reply_outbox` is the transactional reply intent: its row is inserted in the SAME
-- transaction as the consumption commit, then driven through
-- pending -> in_flight -> sent | failed | unknown.
-- `unknown` is the honest state for an ambiguous send and is NEVER auto-resent.

CREATE TABLE IF NOT EXISTS telegram_consumer_offset (
    consumer          text PRIMARY KEY,
    last_update_id    bigint NOT NULL DEFAULT 0,
    owner_id          text,
    lease_expires_at  timestamptz
);

CREATE TABLE IF NOT EXISTS telegram_receipts (
    id                bigserial PRIMARY KEY,
    event_key         text NOT NULL UNIQUE,
    bot_id            text NOT NULL,
    chat_id           text NOT NULL,
    message_id        text NOT NULL,
    update_id         bigint NOT NULL,
    content_digest    text NOT NULL,
    raw_text          text NOT NULL DEFAULT '',
    kind              text NOT NULL DEFAULT 'created',
    received_at       timestamptz NOT NULL DEFAULT now(),
    claim_token       text,
    claimed_by        text,
    lease_expires_at  timestamptz,
    status            text NOT NULL DEFAULT 'received',
    operation_id      uuid,
    attempts          integer NOT NULL DEFAULT 0,
    last_error        text,
    CONSTRAINT ck_receipts_status
        CHECK (status IN ('received', 'processing', 'completed'))
);

CREATE INDEX IF NOT EXISTS ix_receipts_status_lease
    ON telegram_receipts (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS reply_outbox (
    operation_id      uuid PRIMARY KEY,
    event_key         text NOT NULL REFERENCES telegram_receipts (event_key),
    chat_id           text NOT NULL,
    body              text NOT NULL,
    reply_state       text NOT NULL DEFAULT 'pending',
    attempts          integer NOT NULL DEFAULT 0,
    current_attempt_id uuid,
    in_flight_at      timestamptz,
    last_error        text,
    resolved_by       text,
    resolved_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_outbox_state
        CHECK (reply_state IN ('pending', 'in_flight', 'sent', 'failed', 'unknown',
                               'resolved_acknowledged', 'resolved_resent'))
);

CREATE INDEX IF NOT EXISTS ix_outbox_state ON reply_outbox (reply_state);


-- ---------------------------------------------------------------------------
-- Reply resolution: audit trail for the `unknown` state.
--
-- `unknown` means a send was attempted and we cannot know whether it arrived. An
-- operator or the owning user may resolve it, but resolution is RECORDED, never
-- silent, and a resend is explicitly acknowledged as possibly duplicating.
--
-- Every row is an immutable fact: who did what, to which reply, and the outcome.
-- `delivery_attempt` counts real network sends of this reply, including resends
-- after an ambiguous result, so duplicates are traceable rather than mysterious.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reply_audit (
    id                 bigserial PRIMARY KEY,
    operation_id       uuid NOT NULL REFERENCES reply_outbox (operation_id),
    actor              text NOT NULL,          -- authenticated principal
    action             text NOT NULL,          -- acknowledge | resend | auto_recover
    from_state         text NOT NULL,
    to_state           text NOT NULL,
    delivery_attempt   integer NOT NULL DEFAULT 0,
    detail             text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_reply_audit_action
        CHECK (action IN ('acknowledge', 'resend', 'auto_recover'))
);

CREATE INDEX IF NOT EXISTS ix_reply_audit_operation
    ON reply_audit (operation_id, created_at);

-- Resolution is ownership-checked: the resolving principal must be bound to the
-- chat that owns the reply. Kept as a table so the binding is data, not code.
CREATE TABLE IF NOT EXISTS reply_owners (
    chat_id      text PRIMARY KEY,
    owner_id     text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
