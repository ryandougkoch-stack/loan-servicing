-- scripts/migrations/0003_loan_conversion.sql
--
-- Mid-term boarding (loan conversion) — schema layer.
--
-- Adds the columns and tables needed to board a loan that is already
-- mid-life when it arrives from a prior servicer. Companion to single-loan
-- and batch conversion APIs in subsequent phases.
--
-- Three concerns in one file (per the design discussion):
--   1. loan: boarding_type + accrual_start_date so the accrual worker can
--      tell originated from converted loans without changing existing
--      semantics for funded_at.
--   2. loan_conversion: structured per-loan record of the conversion event
--      (opening balances, prior-servicer refs, optional batch FK).
--   3. conversion_batch: per-upload record so a batch import has a single
--      row that pins down "what was uploaded, by whom, when, with what
--      result" for audit.
--
-- Also adds GL account 2050 'Conversion Suspense' so the opening journal
-- entry can credit a real account instead of stuffing into 2030 generic
-- suspense (which would mix conversion balances with payment-clearing
-- balances and make reconciliation against prior-servicer settlements
-- much harder).
--
-- Apply per tenant (idempotent re-runs are safe):
--   (echo "SET search_path TO tenant_<slug>, shared, public;"; \
--    cat scripts/migrations/0003_loan_conversion.sql) | \
--    docker exec -i loan-servicing-postgres-1 psql -U lsp_user -d loan_servicing \
--      -v ON_ERROR_STOP=1

-- ---------------------------------------------------------------------------
-- 1. loan: boarding_type + accrual_start_date
-- ---------------------------------------------------------------------------

ALTER TABLE loan
    ADD COLUMN IF NOT EXISTS boarding_type TEXT NOT NULL DEFAULT 'originated';

ALTER TABLE loan
    ADD COLUMN IF NOT EXISTS accrual_start_date DATE NULL;

-- 'originated' = funded fresh in this system; accrual starts at funded_at.
-- 'converted'  = transferred mid-life from a prior servicer; accrual starts
--                at accrual_start_date (= as_of_date), not funded_at.
ALTER TABLE loan DROP CONSTRAINT IF EXISTS ck_loan_boarding_type;
ALTER TABLE loan ADD CONSTRAINT ck_loan_boarding_type
    CHECK (boarding_type IN ('originated', 'converted'));

-- Converted loans must carry an accrual_start_date; originated must not.
-- Enforced as a single check so the two columns stay coherent.
ALTER TABLE loan DROP CONSTRAINT IF EXISTS ck_loan_accrual_start_coherent;
ALTER TABLE loan ADD CONSTRAINT ck_loan_accrual_start_coherent
    CHECK (
        (boarding_type = 'originated' AND accrual_start_date IS NULL)
        OR
        (boarding_type = 'converted'  AND accrual_start_date IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS ix_loan_accrual_start_date
    ON loan (accrual_start_date) WHERE accrual_start_date IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 2. conversion_batch: one row per uploaded file
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS conversion_batch (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    uploaded_by        UUID         NOT NULL,
    uploaded_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    file_name          TEXT         NOT NULL,
    file_hash          TEXT         NULL,           -- sha256 of upload, for dedupe / audit
    file_size_bytes    BIGINT       NULL,
    status             TEXT         NOT NULL DEFAULT 'pending',
    total_rows         INTEGER      NOT NULL DEFAULT 0,
    succeeded_rows     INTEGER      NOT NULL DEFAULT 0,
    failed_rows        INTEGER      NOT NULL DEFAULT 0,
    validation_report  JSONB        NULL,
    commit_report      JSONB        NULL,
    notes              TEXT         NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_conversion_batch_status
        CHECK (status IN ('pending', 'validating', 'validated',
                          'committing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_conversion_batch_row_counts
        CHECK (succeeded_rows + failed_rows <= total_rows
               AND total_rows >= 0
               AND succeeded_rows >= 0
               AND failed_rows >= 0)
);

CREATE INDEX IF NOT EXISTS ix_conversion_batch_uploaded_by
    ON conversion_batch (uploaded_by, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS ix_conversion_batch_status
    ON conversion_batch (status) WHERE status NOT IN ('completed', 'failed', 'cancelled');


-- ---------------------------------------------------------------------------
-- 3. loan_conversion: one row per converted loan
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS loan_conversion (
    id                          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    loan_id                     UUID         NOT NULL UNIQUE REFERENCES loan(id),
    batch_id                    UUID         NULL REFERENCES conversion_batch(id),

    -- Cutover
    as_of_date                  DATE         NOT NULL,

    -- Opening balances at as_of_date (canonical numbers from prior servicer)
    current_principal           NUMERIC(18, 2) NOT NULL,
    accrued_interest            NUMERIC(18, 2) NOT NULL DEFAULT 0,
    accrued_fees                NUMERIC(18, 2) NOT NULL DEFAULT 0,

    -- Last-payment context (feeds delinquency engine + schedule generation)
    last_payment_date           DATE         NULL,
    last_payment_amount         NUMERIC(18, 2) NULL,
    next_due_date               DATE         NULL,

    -- Running totals (life-of-loan) — for IRR / 1098 reporting
    paid_to_date_principal      NUMERIC(18, 2) NOT NULL DEFAULT 0,
    paid_to_date_interest       NUMERIC(18, 2) NOT NULL DEFAULT 0,
    paid_to_date_fees           NUMERIC(18, 2) NOT NULL DEFAULT 0,

    -- Prior servicer references
    prior_servicer_name         TEXT         NULL,
    prior_servicer_loan_id      TEXT         NULL,
    conversion_document_id      UUID         NULL,    -- transfer-of-servicing doc, optional

    -- Audit
    suspense_account_id         UUID         NULL REFERENCES ledger_account(id),
    opening_journal_entry_id    UUID         NULL,    -- set after JE posted; no FK because
                                                     -- syndicated loans get N entries (see notes)
    posted_by                   UUID         NULL,
    posted_at                   TIMESTAMPTZ  NULL,
    notes                       TEXT         NULL,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_loan_conv_principal_positive
        CHECK (current_principal >= 0),
    CONSTRAINT ck_loan_conv_accrued_nonneg
        CHECK (accrued_interest >= 0 AND accrued_fees >= 0),
    CONSTRAINT ck_loan_conv_paid_nonneg
        CHECK (paid_to_date_principal >= 0
               AND paid_to_date_interest >= 0
               AND paid_to_date_fees >= 0),
    CONSTRAINT ck_loan_conv_last_payment_before_cutover
        CHECK (last_payment_date IS NULL OR last_payment_date <= as_of_date)
);

-- One conversion record per loan (already enforced by UNIQUE on loan_id).
-- Block double-importing the same prior-servicer loan ID — the canonical
-- defence against accidentally boarding the same loan twice in two batches.
CREATE UNIQUE INDEX IF NOT EXISTS ux_loan_conv_prior_servicer_loan
    ON loan_conversion (prior_servicer_loan_id)
    WHERE prior_servicer_loan_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_loan_conv_batch
    ON loan_conversion (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_loan_conv_as_of_date
    ON loan_conversion (as_of_date);


-- ---------------------------------------------------------------------------
-- 4. ledger_account 2050 'Conversion Suspense'
-- ---------------------------------------------------------------------------
--
-- Liability/credit. Opening JE on conversion:
--     DR 1100 Loans Receivable - Principal     (current_principal)
--     DR 1110 Accrued Interest Receivable      (accrued_interest)
--     CR 2050 Conversion Suspense              (sum)
-- Settles when cash transfers from the prior servicer:
--     DR 2050 Conversion Suspense              (sum)
--     CR 1010 Cash - Operating                 (sum)
-- Kept distinct from 2030 generic Suspense Liability so conversion-clearing
-- balances don't co-mingle with payment-suspense balances on reconciliation.

INSERT INTO ledger_account (code, name, account_type, normal_balance, is_active)
VALUES ('2050', 'Conversion Suspense', 'liability', 'credit', true)
ON CONFLICT (code) DO NOTHING;
