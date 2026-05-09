# Import all models so SQLAlchemy can resolve relationships
from app.models.loan import Loan
from app.models.ledger import Payment, Fee, JournalEntry, JournalLine, InterestAccrual, LedgerAccount
from app.models.schedule import PaymentSchedule
from app.models.portfolio import Portfolio, Client, Counterparty, LoanGuarantor, Collateral, Covenant, RateReset, LoanModification, DelinquencyRecord, WorkoutPlan, PayoffQuote, WorkflowTask, Document, LoanAllocation
from app.models.conversion import LoanConversion, ConversionBatch
