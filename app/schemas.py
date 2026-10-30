"""Pydantic models used as Gemini response schemas.

No defaults and no Optional fields on purpose: the Gemini API rejects
default values in response schemas. Unknown values are returned as "".
"""
from pydantic import BaseModel, Field


class Metric(BaseModel):
    name: str = Field(description="e.g. Revenue, EBITDA, Net debt, Cash, Gross margin, NPL ratio")
    value: str = Field(description="The figure with its unit/currency exactly as supported by the documents")
    period: str = Field(description="Period or date it refers to, or empty string")
    source_file: str = Field(description="File name the figure comes from")


class Finding(BaseModel):
    category: str = Field(description="One of: financial, credit_portfolio, customer_concentration, deal_terms, legal_regulatory, operational, data_quality")
    title: str = Field(description="Short headline of the issue")
    severity: str = Field(description="One of: Low, Medium, High")
    explanation: str = Field(description="Why it matters for the deal, in 1-3 sentences")
    evidence: str = Field(description="Specific figures or a short paraphrase from the documents that support this")
    source_file: str = Field(description="File name that contains the evidence")


class Analysis(BaseModel):
    company_summary: str
    key_metrics: list[Metric]
    findings: list[Finding]
    missing_information: list[str] = Field(description="Documents or data that are needed but not provided")
    questions_for_management: list[str]
    recommendation: str = Field(description="One of: Proceed, Proceed with conditions, Pause, Decline")
    recommendation_rationale: str
