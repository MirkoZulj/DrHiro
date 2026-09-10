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
    kind              text NOT NULL DEFAULT 'created',
    received_at       timestamptz NOT NULL DEFAULT now(),
    claim_token       text,
    claimed_by        text,
    lease_expires_at  timestamptz,
    status            text NOT NULL DEFAULT 'received',
    operation_id      uuid,
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
    in_flight_at      timestamptz,
    last_error        text,
    resolved_by       text,
    resolved_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_outbox_state
        CHECK (reply_state IN ('pending', 'in_flight', 'sent', 'failed', 'unknown'))
);

CREATE INDEX IF NOT EXISTS ix_outbox_state ON reply_outbox (reply_state);
