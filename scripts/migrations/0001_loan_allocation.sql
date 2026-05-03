-- scripts/migrations/0001_loan_allocation.sql
--
-- Adds the loan_allocation table for syndication / multi-portfolio ownership.
-- A syndicated loan can be held across several funds, each with a pro-rata
-- ownership_pct that sums to 100 across the active set for a given loan.
--
-- Effective-dated rows: half-open interval [effective_date, end_date).
-- An allocation is "active on date D" iff
--     effective_date <= D AND (end_date IS NULL OR end_date > D)
--
-- Apply per tenant (idempotent). Example:
--   docker exec -i loan-servicing-postgres-1 psql -U lsp_user -d loan_servicing \
--     -v ON_ERROR_STOP=1 -c "SET search_path TO tenant_dev_fund, shared, public" \
--     -f /dev/stdin < scripts/migrations/0001_loan_allocation.sql

CREATE TABLE IF NOT EXISTS loan_allocation (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    loan_id         UUID         NOT NULL REFERENCES loan(id),
    portfolio_id    UUID         NOT NULL REFERENCES portfolio(id),
    ownership_pct   NUMERIC(9,6) NOT NULL,
    effective_date  DATE         NOT NULL,
    end_date        DATE         NULL,
    notes           TEXT         NULL,
    created_by      UUID         NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_loan_alloc_pct_range
        CHECK (ownership_pct > 0 AND ownership_pct <= 100),
    CONSTRAINT ck_loan_alloc_period
        CHECK (end_date IS NULL OR end_date > effective_date)
);

CREATE INDEX IF NOT EXISTS ix_loan_alloc_loan_active
    ON loan_allocation (loan_id) WHERE end_date IS NULL;
CREATE INDEX IF NOT EXISTS ix_loan_alloc_loan_period
    ON loan_allocation (loan_id, effective_date);
CREATE INDEX IF NOT EXISTS ix_loan_alloc_portfolio_active
    ON loan_allocation (portfolio_id) WHERE end_date IS NULL;
CREATE INDEX IF NOT EXISTS ix_loan_alloc_portfolio_period
    ON loan_allocation (portfolio_id, effective_date);

-- Backfill: every existing loan gets exactly one 100% active allocation row
-- matching its current portfolio_id, effective from its origination_date.
-- Skip loans that already have an allocation row (idempotent re-runs).
INSERT INTO loan_allocation (loan_id, portfolio_id, ownership_pct, effective_date)
SELECT l.id, l.portfolio_id, 100.0, l.origination_date
FROM loan l
WHERE NOT EXISTS (
    SELECT 1 FROM loan_allocation a WHERE a.loan_id = l.id
);
