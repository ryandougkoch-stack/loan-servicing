"""app/api/v1/endpoints/document_parsing.py
Loan document parsing using Claude.
Reads a PDF (credit agreement, term sheet, promissory note) and extracts
the key economic terms needed to board a loan.
"""
import base64
import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.api.dependencies import require_min_role
from app.core.security import TokenPayload

router = APIRouter(prefix="/document-parsing", tags=["document-parsing"])

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MAX_PDF_SIZE = 30 * 1024 * 1024  # 30 MB


EXTRACTION_PROMPT = """You are a loan servicing specialist reviewing a credit agreement, term sheet, or promissory note. Read this document carefully and extract the key economic terms needed to board this loan into a servicing system.

Return ONLY a single JSON object with these exact fields. Use null for any field you cannot find or are unsure about. Do not invent data.

{
  "borrower_legal_name": "exact legal name of the primary borrower",
  "entity_type": "one of: LLC, Corp, LP, LLP, Trust, Individual",
  "state_of_formation": "two-letter US state code if mentioned, e.g. DE",
  "loan_name": "deal name or facility name if specified",
  "commitment_amount": "total commitment in USD as a number, no symbols or commas",
  "rate_type": "one of: fixed, floating, pik",
  "coupon_rate": "annual interest rate as decimal, e.g. 0.09 for 9%",
  "spread": "spread over index for floating rate, as decimal",
  "index_code": "for floating rate: SOFR or PRIME",
  "day_count": "one of: ACT/360, ACT/365, 30/360",
  "origination_date": "loan funding/closing date in YYYY-MM-DD",
  "maturity_date": "final maturity date in YYYY-MM-DD",
  "payment_frequency": "one of: MONTHLY, QUARTERLY, SEMI_ANNUAL, ANNUAL, BULLET",
  "amortization_type": "one of: bullet, interest_only, amortizing",
  "grace_period_days": "grace period in days as integer, default 5",
  "extraction_notes": "brief note about confidence level or any ambiguity"
}

Return ONLY the JSON object. No markdown, no preamble, no explanation."""


class ExtractedLoanTerms(BaseModel):
    borrower_legal_name: Optional[str] = None
    entity_type: Optional[str] = None
    state_of_formation: Optional[str] = None
    loan_name: Optional[str] = None
    commitment_amount: Optional[float] = None
    rate_type: Optional[str] = None
    coupon_rate: Optional[float] = None
    spread: Optional[float] = None
    index_code: Optional[str] = None
    day_count: Optional[str] = None
    origination_date: Optional[str] = None
    maturity_date: Optional[str] = None
    payment_frequency: Optional[str] = None
    amortization_type: Optional[str] = None
    grace_period_days: Optional[int] = None
    extraction_notes: Optional[str] = None


@router.post("/extract-loan-terms")
async def extract_loan_terms(
    file: UploadFile = File(...),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Upload a credit agreement PDF and get extracted loan terms back."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY environment variable not set on server"
        )

    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    if len(content) > MAX_PDF_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {MAX_PDF_SIZE // 1024 // 1024} MB"
        )

    pdf_b64 = base64.standard_b64encode(content).decode("utf-8")

    payload = {
        "model": "claude-opus-4-7",
        "max_tokens": 1500,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                ANTHROPIC_API_URL,
                json=payload,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach Anthropic API: {str(e)}"
            )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API error ({response.status_code}): {response.text[:500]}"
        )

    data = response.json()
    text_blocks = [b for b in data.get("content", []) if b.get("type") == "text"]
    if not text_blocks:
        raise HTTPException(status_code=500, detail="No text response from Claude")

    raw_text = text_blocks[0]["text"].strip()

    # Strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        terms = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not parse Claude's response as JSON: {str(e)}. Response: {raw_text[:300]}"
        )

    return {
        "extracted": terms,
        "model": data.get("model"),
        "tokens": {
            "input": data.get("usage", {}).get("input_tokens"),
            "output": data.get("usage", {}).get("output_tokens"),
        },
        "filename": file.filename,
    }
