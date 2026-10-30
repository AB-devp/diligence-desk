"""Due diligence analysis, memo generation and document Q&A."""
import json
import math
import re

from . import llm
from .config import settings
from .db import db, new_id, now
from .schemas import Analysis

SYSTEM = (
    "You are a senior due diligence analyst at an investment firm reviewing a target company's "
    "financials, deal documents and customer/loan data. You are precise and skeptical. "
    "You only state what the documents support: every figure and finding must come from the provided "
    "documents and name the file it came from. If something is not in the documents, say it is missing "
    "instead of estimating. Computed loan portfolio statistics provided by the system are authoritative; "
    "use them rather than recalculating."
)

CATEGORIES = {"financial", "credit_portfolio", "customer_concentration", "deal_terms",
              "legal_regulatory", "operational", "data_quality"}
SEVERITY_WEIGHT = {"Low": 1, "Medium": 3, "High": 8}
SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

TASK = """Perform due diligence on the documents above and return JSON matching the schema.

Cover, where the documents allow:
1. Financial health: revenue and growth, profitability and margins, cash flow, debt and leverage, liquidity,
   working capital, one-off items, quality of earnings, inconsistencies between documents.
2. Credit / loan portfolio: delinquency and NPLs, concentration, provisioning vs. losses, underwriting
   and collateral, rate and tenor mix, covenant compliance.
3. Customers: concentration, churn, contract length and terms, receivable aging.
4. Deal and legal: purchase price and structure, change-of-control, guarantees, indemnities, related-party
   dealings, litigation, regulatory or licensing issues.
5. Data quality: unaudited or stale statements, missing periods, numbers that don't reconcile.

Rules:
- Severity: High = could change price or whether to proceed; Medium = needs a mitigation or a follow-up; Low = minor.
- Report at most 25 findings, most severe first. Do not invent risks to fill space.
- key_metrics: only figures actually stated in the documents (or provided computed statistics), with their period.
- missing_information: documents or data a diligence team would still need.
- recommendation must be one of: Proceed, Proceed with conditions, Pause, Decline. Base it on the evidence only.
- Use an empty string where a text field is unknown."""


def _project(pid: str) -> dict:
    with db() as con:
        r = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not r:
        raise KeyError(pid)
    return dict(r)


def build_context(pid: str):
    with db() as con:
        docs = con.execute(
            "SELECT filename, text, loan_stats FROM documents WHERE project_id=? AND status='ready' "
            "ORDER BY created_at", (pid,)).fetchall()
    total = sum(len(d["text"] or "") for d in docs)
    ratio = min(1.0, settings.max_context_chars / total) if total else 1.0
    blocks, portfolios = [], []
    for d in docs:
        t = d["text"] or ""
        if ratio < 1.0:
            t = t[: int(len(t) * ratio)] + "\n[... truncated to fit the model's context ...]"
        blocks.append(f"=== FILE: {d['filename']} ===\n{t}")
        for s in json.loads(d["loan_stats"] or "[]"):
            portfolios.append({"file": d["filename"], **s})
    return "\n\n".join(blocks), portfolios, len(docs)


def portfolios(pid: str) -> list:
    with db() as con:
        docs = con.execute("SELECT filename, loan_stats FROM documents WHERE project_id=? AND status='ready' "
                           "ORDER BY created_at", (pid,)).fetchall()
    return [{"file": d["filename"], **s} for d in docs for s in json.loads(d["loan_stats"] or "[]")]


def _portfolio_block(portfolios: list) -> str:
    if not portfolios:
        return ""
    return ("COMPUTED LOAN PORTFOLIO STATISTICS (calculated by code from the full files; authoritative):\n"
            + json.dumps(portfolios, indent=1) + "\n\n")


def _normalize(raw: dict) -> dict:
    a = Analysis.model_validate(raw).model_dump()
    for f in a["findings"]:
        s = f["severity"].strip().lower()
        f["severity"] = "High" if s.startswith(("h", "c")) else "Low" if s.startswith("l") else "Medium"
        c = re.sub(r"[^a-z]+", "_", f["category"].strip().lower()).strip("_")
        f["category"] = c if c in CATEGORIES else "operational"
    a["findings"].sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return a


def risk_summary(findings: list) -> dict:
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for f in findings:
        counts[f["severity"]] += 1
    weight = sum(SEVERITY_WEIGHT[k] * v for k, v in counts.items())
    # Saturating heuristic (0-100). Not calibrated against real outcomes.
    return {"counts": counts, "risk_score": round(100 * (1 - math.exp(-weight / 20)))}


def run_analysis(pid: str) -> dict:
    p = _project(pid)
    context, portfolios, n = build_context(pid)
    if n == 0:
        raise ValueError("No processed documents yet. Upload files and wait until they show 'ready'.")
    prompt = (
        f"DEAL: {p['company_name']} | type: {p['deal_type'] or 'not stated'} | size: {p['deal_size'] or 'not stated'}\n"
        f"ANALYST NOTES: {p['notes'] or 'none'}\n\n"
        f"{_portfolio_block(portfolios)}DOCUMENTS ({n} files):\n{context}\n\n{TASK}"
    )
    result = _normalize(llm.generate_json(prompt, Analysis, SYSTEM))
    result.update(risk_summary(result["findings"]))
    result["loan_portfolios"] = portfolios
    result["document_count"] = n
    result["model"] = llm.model_in_use()
    result["created_at"] = now()
    with db() as con:
        con.execute("INSERT INTO analyses(id, project_id, created_at, data) VALUES (?,?,?,?)",
                    (new_id(), pid, result["created_at"], json.dumps(result)))
    return result


def latest_analysis(pid: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT data FROM analyses WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
    return json.loads(r["data"]) if r else None


MEMO_TASK = """Write an investment due diligence memo in Markdown for the investment committee.

Use exactly these sections as '##' headings:
Executive summary, Company and deal overview, Financial analysis, Credit and loan portfolio, Customers and concentration,
Deal terms and legal, Key risks, Information gaps and next steps, Recommendation.

Rules:
- Ground every number in the documents or the analysis JSON and name the source file in parentheses.
- In 'Key risks' use a Markdown table: Risk | Severity | Evidence | Mitigation.
- If a section has no supporting material, write one line saying what is missing instead of filling it with generic text.
- Professional, direct tone. No filler. No invented figures."""


def generate_memo(pid: str) -> str:
    p = _project(pid)
    analysis = latest_analysis(pid) or run_analysis(pid)
    context, portfolios, _ = build_context(pid)
    brief = {k: analysis[k] for k in ("company_summary", "key_metrics", "findings", "missing_information",
                                      "questions_for_management", "recommendation", "recommendation_rationale",
                                      "risk_score", "counts")}
    prompt = (
        f"DEAL: {p['company_name']} | type: {p['deal_type'] or 'not stated'} | size: {p['deal_size'] or 'not stated'}\n\n"
        f"ANALYSIS (JSON):\n{json.dumps(brief, indent=1)}\n\n{_portfolio_block(portfolios)}"
        f"SOURCE DOCUMENTS:\n{context[:300_000]}\n\n{MEMO_TASK}"
    )
    md = llm.generate_text(prompt, SYSTEM, temperature=0.3).strip()
    md += ("\n\n---\n*Draft generated by AI from the uploaded documents. Verify every figure against the "
           "source files before relying on it.*")
    with db() as con:
        con.execute("INSERT INTO reports(id, project_id, created_at, markdown) VALUES (?,?,?,?)",
                    (new_id(), pid, now(), md))
    return md


def latest_memo(pid: str) -> str | None:
    with db() as con:
        r = con.execute("SELECT markdown FROM reports WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
    return r["markdown"] if r else None


# ---------- Q&A over the deal's documents ----------
STOP = set("the and for are was were what which who whom how why when where does did has have had this that these those "
           "with from into about over under than then them they their there will would could should can any all our your "
           "its not but you per".split())

CHAT_TASK = """Answer the question using ONLY the document excerpts below. Cite the file name in square brackets
after each fact, e.g. [financials_2024.xlsx]. If the excerpts don't contain the answer, say what is missing
rather than guessing. For arithmetic, show the numbers you used."""


def retrieve(pid: str, question: str, k: int = 8) -> list[dict]:
    terms = [t for t in re.findall(r"[a-z0-9]{3,}", question.lower()) if t not in STOP][:12]
    rows = []
    with db() as con:
        if terms:
            q = " OR ".join(f'"{t}"' for t in terms)
            rows = con.execute(
                "SELECT text, filename FROM chunks WHERE chunks MATCH ? AND project_id = ? ORDER BY rank LIMIT ?",
                (q, pid, k)).fetchall()
        if not rows:
            rows = con.execute("SELECT text, filename FROM chunks WHERE project_id = ? LIMIT ?", (pid, 4)).fetchall()
    return [dict(r) for r in rows]


def chat(pid: str, question: str) -> str:
    _project(pid)
    hits = retrieve(pid, question)
    if not hits:
        raise ValueError("No processed documents yet. Upload files and wait until they show 'ready'.")
    with db() as con:
        hist = con.execute("SELECT role, content FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 6", (pid,)).fetchall()
    history = "\n".join(f"{h['role']}: {h['content']}" for h in reversed(hist))
    excerpts = "\n\n".join(f"[{h['filename']}]\n{h['text']}" for h in hits)
    prompt = f"{CHAT_TASK}\n\nEXCERPTS:\n{excerpts}\n\nCONVERSATION SO FAR:\n{history or '(none)'}\n\nQUESTION: {question}"
    answer = llm.generate_text(prompt, SYSTEM, temperature=0.2).strip()
    with db() as con:
        con.execute("INSERT INTO messages(project_id, role, content, created_at) VALUES (?,?,?,?)", (pid, "user", question, now()))
        con.execute("INSERT INTO messages(project_id, role, content, created_at) VALUES (?,?,?,?)", (pid, "assistant", answer, now()))
    return answer
