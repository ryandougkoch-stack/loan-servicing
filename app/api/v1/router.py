"""app/api/v1/router.py"""
from fastapi import APIRouter
from app.api.v1.endpoints import auth, loans, payments, reports, counterparties, document_parsing, clients, portfolios

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(loans.router)
api_router.include_router(payments.router)
api_router.include_router(reports.router)
api_router.include_router(counterparties.router)
api_router.include_router(document_parsing.router)
api_router.include_router(clients.router)
api_router.include_router(portfolios.router)
