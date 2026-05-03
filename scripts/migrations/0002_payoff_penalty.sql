-- scripts/migrations/0002_payoff_penalty.sql
--
-- Closes the GL gap on loan payoffs:
--   1. Adds payment.applied_to_penalty so a payoff payment carrying a
--      prepayment penalty can record it in the same Payment row as the
--      receivables-clearing portion.
--   2. Adds ledger_account 4040 'Prepayment Penalty Income' so the
--      journal-entry generator can credit penalty cash to a real income
--      account (not stuffed into "fees" or "suspense").
--
-- Apply per tenant (idempotent):
--   (echo "SET search_path TO tenant_<slug>, shared, public;"; \
--    cat scripts/migrations/0002_payoff_penalty.sql) | \
--    docker exec -i loan-servicing-postgres-1 psql -U lsp_user -d loan_servicing \
--      -v ON_ERROR_STOP=1

ALTER TABLE payment
    ADD COLUMN IF NOT EXISTS applied_to_penalty NUMERIC(18, 2) NOT NULL DEFAULT 0;

-- The existing ck_payment_waterfall constraint enforces
--   sum(applied_to_*) + held_in_suspense = gross_amount
-- but predates applied_to_penalty. Drop and recreate to include the new bucket.
ALTER TABLE payment DROP CONSTRAINT IF EXISTS ck_payment_waterfall;
ALTER TABLE payment ADD CONSTRAINT ck_payment_waterfall CHECK (
    applied_to_fees
    + applied_to_interest
    + applied_to_principal
    + applied_to_escrow
    + applied_to_advances
    + applied_to_penalty
    + held_in_suspense
    = gross_amount
);

INSERT INTO ledger_account (code, name, account_type, normal_balance, is_active)
VALUES ('4040', 'Prepayment Penalty Income', 'income', 'credit', true)
ON CONFLICT (code) DO NOTHING;
