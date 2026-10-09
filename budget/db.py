from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrollments (
    id              TEXT PRIMARY KEY,
    institution     TEXT,
    access_token    TEXT NOT NULL,          -- Fernet-encrypted
    status          TEXT NOT NULL DEFAULT 'ok',
    last_error      TEXT,
    last_synced_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    enrollment_id     TEXT NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    institution       TEXT,
    name              TEXT,
    type              TEXT,                 -- depository | credit
    subtype           TEXT,                 -- checking | savings | credit_card ...
    last_four         TEXT,
    currency          TEXT,
    status            TEXT,
    include_in_budget BOOLEAN NOT NULL DEFAULT TRUE,
    ledger_balance    NUMERIC(14,2),
    available_balance NUMERIC(14,2),
    history_synced    BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date              DATE NOT NULL,
    description       TEXT,
    merchant          TEXT,
    merchant_key      TEXT,
    raw_amount        NUMERIC(14,2) NOT NULL,   -- exactly as Teller sent it
    flow              NUMERIC(14,2) NOT NULL,   -- + money in, - money out
    type              TEXT,
    status            TEXT,
    teller_category   TEXT,
    auto_category     TEXT NOT NULL,
    override_category TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS transactions_date_idx ON transactions (date);
CREATE INDEX IF NOT EXISTS transactions_account_date_idx ON transactions (account_id, date);
CREATE INDEX IF NOT EXISTS transactions_merchant_key_idx ON transactions (merchant_key);

-- "Always categorize this merchant as X"
CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_key TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id         BIGSERIAL PRIMARY KEY,
    ip         TEXT,
    success    BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_created_idx ON login_attempts (created_at);
"""

_schema_ready = False


@contextmanager
def connect():
    global _schema_ready
    conn = psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=False)
    try:
        if not _schema_ready:
            conn.execute(SCHEMA)
            conn.commit()
            _schema_ready = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
