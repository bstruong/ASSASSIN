-- Canonical PostgreSQL Schema Definitions for ASSASSIN
-- All monetary values are integer cents (BIGINT). No floating-point arithmetic.
-- All raw pages, tokens, and payloads are append-only.

CREATE TABLE IF NOT EXISTS raw_payloads (
  raw_payload_id     UUID PRIMARY KEY,
  content_sha256     CHAR(64) NOT NULL,
  byte_length        BIGINT NOT NULL CHECK (byte_length > 0),
  original_basename  TEXT NOT NULL,
  ingested_at        TIMESTAMPTZ NOT NULL,
  UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
  run_id             UUID PRIMARY KEY,
  raw_payload_id     UUID NOT NULL REFERENCES raw_payloads (raw_payload_id),
  adapter_id         TEXT NOT NULL,
  adapter_version    TEXT NOT NULL,
  started_at         TIMESTAMPTZ NOT NULL,
  status             TEXT NOT NULL CHECK (status IN (
                       'raw_stored', 'extracted', 'validated',
                       'canonical_persisted', 'failed')),
  error_code         TEXT,
  error_message      TEXT,
  CHECK (
    (status = 'failed') = (error_code IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS raw_pages (
  raw_page_id        UUID PRIMARY KEY,
  run_id             UUID NOT NULL REFERENCES extraction_runs (run_id),
  page_number        INT NOT NULL CHECK (page_number >= 1),
  page_text          TEXT NOT NULL,
  UNIQUE (run_id, page_number)
);

CREATE TABLE IF NOT EXISTS raw_tokens (
  raw_token_id       UUID PRIMARY KEY,
  raw_page_id        UUID NOT NULL REFERENCES raw_pages (raw_page_id),
  token_kind         TEXT NOT NULL CHECK (token_kind IN ('word', 'cell')),
  token_text         TEXT NOT NULL,
  x0_mp              BIGINT NOT NULL,
  y0_mp              BIGINT NOT NULL,
  x1_mp              BIGINT NOT NULL,
  y1_mp              BIGINT NOT NULL,
  table_index        INT,
  row_index          INT,
  col_index          INT,
  CHECK (x1_mp > x0_mp AND x0_mp >= 0),
  CHECK (y1_mp > y0_mp AND y0_mp >= 0)
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id         UUID PRIMARY KEY,
  institution        TEXT NOT NULL,
  account_mask       TEXT NOT NULL,
  account_domain     TEXT NOT NULL CHECK (account_domain IN (
                       'depository', 'revolving_credit', 'custodial_brokerage')),
  account_type       TEXT NOT NULL,
  currency           CHAR(3) NOT NULL CHECK (currency = 'USD'),
  UNIQUE (institution, account_mask, account_type),
  CHECK (
    (account_domain = 'depository' AND account_type IN ('checking', 'savings'))
    OR (account_domain = 'revolving_credit' AND account_type = 'credit_card')
    OR (account_domain = 'custodial_brokerage'
        AND account_type IN ('brokerage_cash', 'brokerage_margin'))
  )
);

CREATE TABLE IF NOT EXISTS statements (
  statement_id           UUID PRIMARY KEY,
  account_id             UUID NOT NULL REFERENCES accounts (account_id),
  run_id                 UUID NOT NULL REFERENCES extraction_runs (run_id),
  raw_payload_id         UUID NOT NULL REFERENCES raw_payloads (raw_payload_id),
  statement_start_date   DATE NOT NULL,
  statement_end_date     DATE NOT NULL,
  opening_balance_cents  BIGINT NOT NULL,
  closing_balance_cents  BIGINT NOT NULL,
  net_change_cents       BIGINT NOT NULL,
  CHECK (statement_start_date <= statement_end_date),
  CHECK (net_change_cents = closing_balance_cents - opening_balance_cents),
  UNIQUE (account_id, statement_start_date, statement_end_date, run_id)
);

CREATE TABLE IF NOT EXISTS statement_depository_summaries (
  statement_id              UUID PRIMARY KEY REFERENCES statements (statement_id),
  deposits_cents            BIGINT NOT NULL CHECK (deposits_cents >= 0),
  withdrawals_cents         BIGINT NOT NULL CHECK (withdrawals_cents >= 0),
  interest_paid_cents       BIGINT NOT NULL CHECK (interest_paid_cents >= 0),
  fees_cents                BIGINT NOT NULL CHECK (fees_cents >= 0)
);

CREATE TABLE IF NOT EXISTS statement_credit_summaries (
  statement_id                  UUID PRIMARY KEY REFERENCES statements (statement_id),
  previous_balance_cents        BIGINT NOT NULL,
  payments_credits_cents        BIGINT NOT NULL CHECK (payments_credits_cents >= 0),
  purchases_cents               BIGINT NOT NULL CHECK (purchases_cents >= 0),
  cash_advances_cents           BIGINT NOT NULL CHECK (cash_advances_cents >= 0),
  balance_transfers_cents       BIGINT NOT NULL CHECK (balance_transfers_cents >= 0),
  fees_charged_cents            BIGINT NOT NULL CHECK (fees_charged_cents >= 0),
  interest_charged_cents        BIGINT NOT NULL CHECK (interest_charged_cents >= 0),
  new_balance_cents             BIGINT NOT NULL,
  minimum_payment_due_cents     BIGINT NOT NULL CHECK (minimum_payment_due_cents >= 0),
  payment_due_date              DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS statement_brokerage_summaries (
  statement_id                      UUID PRIMARY KEY REFERENCES statements (statement_id),
  opening_cash_cents                BIGINT NOT NULL,
  closing_cash_cents                BIGINT NOT NULL,
  opening_portfolio_cents           BIGINT,
  closing_portfolio_cents           BIGINT,
  realized_gains_cents              BIGINT,
  unrealized_gains_cents            BIGINT,
  income_dividends_cents            BIGINT,
  transfers_in_cents                BIGINT,
  transfers_out_cents               BIGINT,
  CHECK (
    (opening_portfolio_cents IS NULL) = (closing_portfolio_cents IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS transactions (
  transaction_id         UUID PRIMARY KEY,
  statement_id           UUID NOT NULL REFERENCES statements (statement_id),
  post_date              DATE NOT NULL,
  transaction_date       DATE,
  amount_cents           BIGINT NOT NULL CHECK (amount_cents <> 0),
  description            TEXT NOT NULL CHECK (char_length(description) >= 1),
  transaction_category   TEXT NOT NULL CHECK (transaction_category IN (
                           'deposit', 'withdrawal', 'interest_paid', 'fee',
                           'fee_reversal', 'other_credit', 'other_debit',
                           'purchase', 'payment', 'credit', 'cash_advance',
                           'balance_transfer', 'interest_charged',
                           'dividend', 'interest', 'transfer_in',
                           'transfer_out', 'trade_cash'
                         )),
  balance_after_cents    BIGINT
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_payload ON extraction_runs (raw_payload_id);
CREATE INDEX IF NOT EXISTS idx_raw_pages_run ON raw_pages (run_id);
CREATE INDEX IF NOT EXISTS idx_raw_tokens_page ON raw_tokens (raw_page_id);
CREATE INDEX IF NOT EXISTS idx_statements_account ON statements (account_id);
CREATE INDEX IF NOT EXISTS idx_statements_run ON statements (run_id);
CREATE INDEX IF NOT EXISTS idx_statements_payload ON statements (raw_payload_id);
CREATE INDEX IF NOT EXISTS idx_transactions_statement ON transactions (statement_id);
