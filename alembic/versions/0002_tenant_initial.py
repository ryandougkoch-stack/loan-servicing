"""Initial tenant schema

Revision ID: 0002_tenant_initial
Revises: (none - tenant branch starts fresh per schema)
Create Date: 2024-01-01 00:00:00

Creates all business tables inside a tenant schema.
The search_path is set to the target tenant schema before this runs
(handled by alembic/env.py reading the TENANT_SLUG env var).

Tables created:
  Reference:  rate_index, day_count_convention, payment_frequency, currency
  Core:       portfolio, counterparty, loan
  Loan detail: loan_guarantor, loan_participation, collateral, covenant,
               rate_reset, payment_schedule
  Ledger:     ledger_account, journal_entry, journal_line
  Payments:   payment, suspense_item, fee, interest_accrual
  Collections: delinquency_record, collections_activity, workout_plan
  Servicing:  loan_modification, escrow_account, escrow_disbursement,
              payoff_quote, investor_remittance
  Ops:        document, notice, workflow_task, audit_log
  Reporting:  portfolio_snapshot
  Integration: investran_sync_log
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_tenant_initial"
down_revision = None
branch_labels = ("tenant",)
depends_on = None


def upgrade() -> None:
    # search_path is already set by env.py — no need to set it here
    _create_reference_tables()
    _create_portfolio()
    _create_counterparty()
    _create_loan()
    _create_loan_detail()
    _create_ledger()
    _create_payments()
    _create_collections()
    _create_servicing()
    _create_ops()
    _create_reporting()
    _create_integration()


def downgrade() -> None:
    # Drop in reverse dependency order
    tables = [
        "investran_sync_log", "portfolio_snapshot", "audit_log",
        "workflow_task", "notice", "document", "investor_remittance",
        "payoff_quote", "escrow_disbursement", "escrow_account",
        "loan_modification", "workout_plan", "collections_activity",
        "delinquency_record", "interest_accrual", "fee", "suspense_item",
        "payment", "journal_line", "journal_entry", "ledger_account",
        "payment_schedule", "rate_reset", "covenant", "collateral",
        "loan_participation", "loan_guarantor", "loan", "counterparty",
        "portfolio", "payment_frequency", "day_count_convention",
        "rate_index", "currency",
    ]
    for t in tables:
        op.drop_table(t)


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

def _create_reference_tables():
    op.create_table(
        "currency",
        sa.Column("code", sa.CHAR(3), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("decimal_places", sa.Integer(), nullable=False, server_default="2"),
        sa.PrimaryKeyConstraint("code"),
    )
    op.execute("INSERT INTO currency VALUES ('USD','US Dollar',2),('EUR','Euro',2),('GBP','British Pound',2)")

    op.create_table(
        "day_count_convention",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.execute("""
        INSERT INTO day_count_convention VALUES
        ('ACT/360','Actual days / 360'),
        ('ACT/365','Actual days / 365'),
        ('30/360','30-day months / 360-day year'),
        ('ACT/ACT','Actual days / actual days in year')
    """)

    op.create_table(
        "payment_frequency",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("periods_per_year", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    op.execute("""
        INSERT INTO payment_frequency VALUES
        ('MONTHLY','Monthly',12),
        ('QUARTERLY','Quarterly',4),
        ('SEMI_ANNUAL','Semi-annual',2),
        ('ANNUAL','Annual',1),
        ('BULLET','Bullet / balloon at maturity',null)
    """)

    op.create_table(
        "rate_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.execute("""
        INSERT INTO rate_index (code, description) VALUES
        ('SOFR','Secured Overnight Financing Rate'),
        ('PRIME','US Prime Rate'),
        ('LIBOR_3M','3-Month LIBOR (legacy)'),
        ('LIBOR_6M','6-Month LIBOR (legacy)')
    """)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def _create_portfolio():
    op.create_table(
        "portfolio",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("fund_type", sa.Text(), nullable=True),
        sa.Column("base_currency", sa.CHAR(3), nullable=False, server_default="USD"),
        sa.Column("inception_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("investran_entity_id", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["base_currency"], ["currency.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.CheckConstraint(
            "status IN ('active','closed','winding_down')",
            name="ck_portfolio_status",
        ),
    )


# ---------------------------------------------------------------------------
# Counterparty
# ---------------------------------------------------------------------------

def _create_counterparty():
    op.create_table(
        "counterparty",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("short_name", sa.Text(), nullable=True),
        sa.Column("tax_id", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("jurisdiction", sa.CHAR(2), nullable=True),
        sa.Column("state_of_formation", sa.CHAR(2), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("postal_code", sa.Text(), nullable=True),
        sa.Column("country", sa.CHAR(2), nullable=False, server_default="US"),
        sa.Column("primary_contact_name", sa.Text(), nullable=True),
        sa.Column("primary_contact_email", sa.Text(), nullable=True),
        sa.Column("primary_contact_phone", sa.Text(), nullable=True),
        sa.Column("kyc_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("kyc_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kyc_reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_counterparty_legal_name", "counterparty", ["legal_name"])
    op.create_index("ix_counterparty_type", "counterparty", ["type"])


# ---------------------------------------------------------------------------
# Loan (core)
# ---------------------------------------------------------------------------

def _create_loan():
    op.create_table(
        "loan",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loan_number", sa.Text(), nullable=False),
        sa.Column("loan_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="boarding"),
        sa.Column("primary_borrower_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="USD"),
        sa.Column("original_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("commitment_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("current_principal", sa.Numeric(18, 2), nullable=False),
        sa.Column("accrued_interest", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("accrued_fees", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("rate_type", sa.Text(), nullable=False),
        sa.Column("coupon_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("pik_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("rate_floor", sa.Numeric(8, 6), nullable=True),
        sa.Column("rate_cap", sa.Numeric(8, 6), nullable=True),
        sa.Column("spread", sa.Numeric(8, 6), nullable=True),
        sa.Column("index_code", sa.Text(), nullable=True),
        sa.Column("day_count", sa.Text(), nullable=False, server_default="ACT/360"),
        sa.Column("origination_date", sa.Date(), nullable=False),
        sa.Column("first_payment_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("payment_frequency", sa.Text(), nullable=False, server_default="QUARTERLY"),
        sa.Column("amortization_type", sa.Text(), nullable=False, server_default="bullet"),
        sa.Column("interest_only_period_months", sa.Integer(), nullable=True),
        sa.Column("balloon_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("grace_period_days", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("late_fee_type", sa.Text(), nullable=True),
        sa.Column("late_fee_amount", sa.Numeric(12, 4), nullable=True),
        sa.Column("default_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("default_triggered_at", sa.Date(), nullable=True),
        sa.Column("investran_loan_id", sa.Text(), nullable=True),
        sa.Column("investran_last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("boarding_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("funded_at", sa.Date(), nullable=True),
        sa.Column("paid_off_at", sa.Date(), nullable=True),
        sa.Column("servicer_notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.ForeignKeyConstraint(["primary_borrower_id"], ["counterparty.id"]),
        sa.ForeignKeyConstraint(["currency"], ["currency.code"]),
        sa.ForeignKeyConstraint(["day_count"], ["day_count_convention.code"]),
        sa.ForeignKeyConstraint(["payment_frequency"], ["payment_frequency.code"]),
        sa.ForeignKeyConstraint(["index_code"], ["rate_index.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_number"),
        sa.CheckConstraint(
            "status IN ('boarding','approved','funded','modified','delinquent','default',"
            "'workout','payoff_pending','paid_off','charged_off','transferred')",
            name="ck_loan_status",
        ),
        sa.CheckConstraint(
            "rate_type IN ('fixed','floating','mixed','pik','step','zero_coupon')",
            name="ck_loan_rate_type",
        ),
    )
    op.create_index("ix_loan_portfolio_id", "loan", ["portfolio_id"])
    op.create_index("ix_loan_borrower_id", "loan", ["primary_borrower_id"])
    op.create_index("ix_loan_status", "loan", ["status"])
    op.create_index("ix_loan_maturity_date", "loan", ["maturity_date"])
    op.create_index("ix_loan_number", "loan", ["loan_number"])


# ---------------------------------------------------------------------------
# Loan detail tables
# ---------------------------------------------------------------------------

def _create_loan_detail():
    op.create_table(
        "loan_guarantor",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guarantor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guarantee_type", sa.Text(), nullable=True),
        sa.Column("guarantee_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["guarantor_id"], ["counterparty.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_id", "guarantor_id"),
    )

    op.create_table(
        "loan_participation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("participation_pct", sa.Numeric(8, 6), nullable=False),
        sa.Column("participation_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("remittance_day", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["investor_id"], ["counterparty.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_participation_loan", "loan_participation", ["loan_id"])

    op.create_table(
        "collateral",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collateral_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("estimated_value", sa.Numeric(18, 2), nullable=True),
        sa.Column("valuation_date", sa.Date(), nullable=True),
        sa.Column("valuation_source", sa.Text(), nullable=True),
        sa.Column("lien_position", sa.Integer(), nullable=True),
        sa.Column("ucc_filed", sa.Boolean(), server_default="false"),
        sa.Column("ucc_filing_number", sa.Text(), nullable=True),
        sa.Column("ucc_filed_date", sa.Date(), nullable=True),
        sa.Column("ucc_expiry_date", sa.Date(), nullable=True),
        sa.Column("insurance_required", sa.Boolean(), server_default="false"),
        sa.Column("insurance_provider", sa.Text(), nullable=True),
        sa.Column("insurance_policy_number", sa.Text(), nullable=True),
        sa.Column("insurance_expiry_date", sa.Date(), nullable=True),
        sa.Column("insurance_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collateral_loan_id", "collateral", ["loan_id"])

    op.create_table(
        "covenant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("covenant_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metric", sa.Text(), nullable=True),
        sa.Column("threshold_operator", sa.Text(), nullable=True),
        sa.Column("threshold_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("measurement_frequency", sa.Text(), nullable=True),
        sa.Column("next_test_date", sa.Date(), nullable=True),
        sa.Column("last_test_date", sa.Date(), nullable=True),
        sa.Column("last_test_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("last_test_result", sa.Text(), nullable=True),
        sa.Column("waiver_granted", sa.Boolean(), server_default="false"),
        sa.Column("waiver_expiry_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_covenant_loan_next_test", "covenant",
                    ["loan_id", "next_test_date"])

    op.create_table(
        "rate_reset",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reset_date", sa.Date(), nullable=False),
        sa.Column("index_value", sa.Numeric(8, 6), nullable=True),
        sa.Column("spread", sa.Numeric(8, 6), nullable=True),
        sa.Column("new_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("floor_applied", sa.Boolean(), server_default="false"),
        sa.Column("cap_applied", sa.Boolean(), server_default="false"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rate_reset_loan_date", "rate_reset", ["loan_id", "reset_date"])

    op.create_table(
        "payment_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_number", sa.Integer(), nullable=False),
        sa.Column("period_start_date", sa.Date(), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("scheduled_principal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("scheduled_interest", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("scheduled_fees", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("scheduled_escrow", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_scheduled", sa.Numeric(18, 2),
                  sa.Computed("scheduled_principal + scheduled_interest + scheduled_fees + scheduled_escrow",
                              persisted=True)),
        sa.Column("days_in_period", sa.Integer(), nullable=True),
        sa.Column("interest_rate_used", sa.Numeric(8, 6), nullable=True),
        sa.Column("beginning_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("ending_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_id", "period_number", "is_current"),
    )
    op.create_index("ix_payment_schedule_loan_due", "payment_schedule", ["loan_id", "due_date"])
    op.create_index("ix_payment_schedule_status_due", "payment_schedule", ["status", "due_date"])


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _create_ledger():
    op.create_table(
        "ledger_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("normal_balance", sa.Text(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("gl_account_code", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["ledger_account.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    # Seed standard chart of accounts
    op.execute("""
        INSERT INTO ledger_account (code, name, account_type, normal_balance) VALUES
        ('1010','Cash - Operating','asset','debit'),
        ('1020','Cash - Suspense','asset','debit'),
        ('1030','Cash - Escrow','asset','debit'),
        ('1100','Loans Receivable - Principal','asset','debit'),
        ('1110','Accrued Interest Receivable','asset','debit'),
        ('1120','Fees Receivable','asset','debit'),
        ('1130','Advances Receivable','asset','debit'),
        ('2010','Investor Payable','liability','credit'),
        ('2020','Escrow Liability','liability','credit'),
        ('2030','Suspense Liability','liability','credit'),
        ('2040','Unearned Fees','liability','credit'),
        ('4010','Interest Income','income','credit'),
        ('4020','Fee Income','income','credit'),
        ('4030','Late Fee Income','income','credit'),
        ('4040','Prepayment Fee Income','income','credit'),
        ('5010','Provision for Loan Loss','expense','debit'),
        ('5020','Charge-off Expense','expense','debit')
    """)

    op.create_table(
        "journal_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("entry_number", sa.Text(), nullable=False),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_type", sa.Text(), nullable=True),
        sa.Column("is_reversed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reversed_by_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reversal_of_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="posted"),
        sa.Column("posted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("investran_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investran_batch_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_number"),
    )
    op.create_index("ix_je_loan_date", "journal_entry",
                    ["loan_id", sa.text("entry_date DESC")])
    op.create_index("ix_je_portfolio_date", "journal_entry",
                    ["portfolio_id", sa.text("entry_date DESC")])
    op.create_index("ix_je_status", "journal_entry", ["status"])

    op.create_table(
        "journal_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("debit_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("credit_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="USD"),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["ledger_account.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0)",
            name="ck_journal_line_one_side",
        ),
    )
    op.create_index("ix_journal_line_entry", "journal_line", ["journal_entry_id"])
    op.create_index("ix_journal_line_account", "journal_line", ["account_id"])


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def _create_payments():
    op.create_table(
        "payment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_number", sa.Text(), nullable=False),
        sa.Column("payment_type", sa.Text(), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("gross_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("applied_to_fees", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("applied_to_interest", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("applied_to_principal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("applied_to_escrow", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("applied_to_advances", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("held_in_suspense", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_number", sa.Text(), nullable=True),
        sa.Column("bank_account_last4", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("return_reason", sa.Text(), nullable=True),
        sa.Column("late_fee_assessed", sa.Numeric(18, 2), server_default="0"),
        sa.Column("days_late", sa.Integer(), server_default="0"),
        sa.Column("posted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["period_id"], ["payment_schedule.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_number"),
        sa.CheckConstraint(
            "applied_to_fees + applied_to_interest + applied_to_principal + "
            "applied_to_escrow + applied_to_advances + held_in_suspense = gross_amount",
            name="ck_payment_waterfall_balance",
        ),
    )
    op.create_index("ix_payment_loan_date", "payment",
                    ["loan_id", sa.text("effective_date DESC")])
    op.create_index("ix_payment_status", "payment", ["status"])
    op.create_index("ix_payment_received_date", "payment", ["received_date"])

    op.create_table(
        "suspense_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payment.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "fee",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fee_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("accrual_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("amount_waived", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("amount_outstanding", sa.Numeric(18, 2),
                  sa.Computed("amount - amount_waived - amount_paid", persisted=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default="accrued"),
        sa.Column("waiver_reason", sa.Text(), nullable=True),
        sa.Column("waived_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payment.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fee_loan_status", "fee", ["loan_id", "status"])

    op.create_table(
        "interest_accrual",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accrual_date", sa.Date(), nullable=False),
        sa.Column("beginning_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("daily_rate", sa.Numeric(12, 10), nullable=False),
        sa.Column("accrued_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("pik_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("day_count_used", sa.Text(), nullable=False),
        sa.Column("rate_snapshot", sa.Numeric(8, 6), nullable=False),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_reversed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_id", "accrual_date"),
    )
    op.create_index("ix_interest_accrual_loan_date", "interest_accrual",
                    ["loan_id", sa.text("accrual_date DESC")])


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def _create_collections():
    op.create_table(
        "delinquency_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("days_past_due", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delinquency_bucket", sa.Text(), nullable=False),
        sa.Column("principal_past_due", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("interest_past_due", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("fees_past_due", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_past_due", sa.Numeric(18, 2),
                  sa.Computed("principal_past_due + interest_past_due + fees_past_due",
                              persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_id", "as_of_date"),
    )
    op.create_index("ix_delinquency_loan_date", "delinquency_record",
                    ["loan_id", sa.text("as_of_date DESC")])
    op.create_index("ix_delinquency_bucket_date", "delinquency_record",
                    ["delinquency_bucket", "as_of_date"])

    op.create_table(
        "collections_activity",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activity_type", sa.Text(), nullable=False),
        sa.Column("activity_date", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("contact_method", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("follow_up_date", sa.Date(), nullable=True),
        sa.Column("promise_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("promise_date", sa.Date(), nullable=True),
        sa.Column("promise_kept", sa.Boolean(), nullable=True),
        sa.Column("performed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collections_loan_date", "collections_activity",
                    ["loan_id", sa.text("activity_date DESC")])

    op.create_table(
        "workout_plan",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("terms_description", sa.Text(), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


# ---------------------------------------------------------------------------
# Servicing
# ---------------------------------------------------------------------------

def _create_servicing():
    op.create_table(
        "loan_modification",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("modification_type", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("prior_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("new_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("prior_maturity", sa.Date(), nullable=True),
        sa.Column("new_maturity", sa.Date(), nullable=True),
        sa.Column("prior_payment_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("new_payment_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("principal_adjusted", sa.Numeric(18, 2), server_default="0"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("document_reference", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_mod_loan_date", "loan_modification",
                    ["loan_id", sa.text("effective_date DESC")])

    op.create_table(
        "escrow_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("current_balance", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("monthly_payment", sa.Numeric(18, 2), nullable=True),
        sa.Column("shortage_amount", sa.Numeric(18, 2), server_default="0"),
        sa.Column("surplus_amount", sa.Numeric(18, 2), server_default="0"),
        sa.Column("last_analysis_date", sa.Date(), nullable=True),
        sa.Column("next_analysis_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("loan_id"),
    )

    op.create_table(
        "escrow_disbursement",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("escrow_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("disbursement_type", sa.Text(), nullable=False),
        sa.Column("disbursement_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("payee", sa.Text(), nullable=True),
        sa.Column("reference_number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["escrow_account_id"], ["escrow_account.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "payoff_quote",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("good_through_date", sa.Date(), nullable=False),
        sa.Column("principal_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("accrued_interest", sa.Numeric(18, 2), nullable=False),
        sa.Column("per_diem", sa.Numeric(18, 4), nullable=False),
        sa.Column("fees_outstanding", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("prepayment_penalty", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("escrow_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("total_payoff", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("generated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payoff_quote_loan_date", "payoff_quote",
                    ["loan_id", sa.text("quote_date DESC")])

    op.create_table(
        "investor_remittance",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("remittance_date", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("principal_collected", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("interest_collected", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("fees_collected", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("servicing_fee", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("net_remittance", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("investran_exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.ForeignKeyConstraint(["investor_id"], ["counterparty.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_remittance_portfolio_date", "investor_remittance",
                    ["portfolio_id", sa.text("remittance_date DESC")])


# ---------------------------------------------------------------------------
# Ops tables
# ---------------------------------------------------------------------------

def _create_ops():
    op.create_table(
        "document",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("storage_provider", sa.Text(), server_default="s3"),
        sa.Column("checksum_sha256", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current_version", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("prior_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("requires_esign", sa.Boolean(), server_default="false"),
        sa.Column("esign_status", sa.Text(), nullable=True),
        sa.Column("esign_provider", sa.Text(), nullable=True),
        sa.Column("esign_envelope_id", sa.Text(), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_loan", "document", ["loan_id"])
    op.create_index("ix_document_type", "document", ["document_type"])
    op.create_index("ix_document_expiry", "document", ["expiry_date"],
                    postgresql_where=sa.text("expiry_date IS NOT NULL"))

    op.create_table(
        "notice",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notice_type", sa.Text(), nullable=False),
        sa.Column("delivery_method", sa.Text(), nullable=False),
        sa.Column("recipient_name", sa.Text(), nullable=True),
        sa.Column("recipient_address", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notice_loan_date", "notice",
                    ["loan_id", sa.text("created_at DESC")])

    op.create_table(
        "workflow_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False, server_default="normal"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("sla_hours", sa.Integer(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["loan_id"], ["loan.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_task_status_due", "workflow_task", ["status", "due_date"])
    op.create_index("ix_workflow_task_loan", "workflow_task", ["loan_id"])
    op.create_index("ix_workflow_task_assigned", "workflow_task",
                    ["assigned_to", "status"])

    # Append-only audit log — no UPDATE/DELETE grants in production
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("old_values", postgresql.JSONB(), nullable=True),
        sa.Column("new_values", postgresql.JSONB(), nullable=True),
        sa.Column("changed_fields", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_loan_date", "audit_log",
                    ["loan_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_log_table_record", "audit_log",
                    ["table_name", "record_id"])
    op.create_index("ix_audit_log_user_date", "audit_log",
                    ["user_id", sa.text("created_at DESC")])


# ---------------------------------------------------------------------------
# Reporting + integration
# ---------------------------------------------------------------------------

def _create_reporting():
    op.create_table(
        "portfolio_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("loan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_committed", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_outstanding", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_accrued_interest", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_past_due", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("count_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_1_30", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_31_60", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_61_90", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_90_plus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_default", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("weighted_avg_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("weighted_avg_maturity", sa.Numeric(6, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portfolio_id", "snapshot_date"),
    )


def _create_integration():
    op.create_table(
        "investran_sync_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("sync_type", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(), nullable=True),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
