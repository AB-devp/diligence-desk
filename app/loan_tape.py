"""Deterministic loan-tape statistics.

If a spreadsheet looks like a loan tape (a balance column plus delinquency /
status / borrower / rate columns) we compute portfolio statistics in code, so
the headline numbers never depend on an LLM's arithmetic.
"""
import re

import pandas as pd

ROLES = {
    "balance": ["outstanding", "current_balance", "os_balance", "principal_balance", "balance",
                "principal", "exposure", "loan_amount", "amount"],
    "dpd": ["dpd", "days_past_due", "days_overdue", "past_due_days", "days_delinquent", "delinquency_days"],
    "status": ["loan_status", "status", "stage", "classification", "performance"],
    "borrower": ["borrower", "customer", "client", "obligor", "debtor", "account_holder", "customer_name", "name"],
    "rate": ["interest_rate", "rate", "apr", "coupon", "yield"],
}
IMPAIRED = re.compile(r"default|non.?perform|npl|charged.?off|write.?off|written.?off|delinq|impair|stage.?3|doubtful|loss", re.I)


def _norm(c) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).lower()).strip("_")


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(r"[,$€£₨\s%]", "", regex=True), errors="coerce")


def _find(cols: dict, role: str, used: set):
    for pat in ROLES[role]:
        for norm, orig in cols.items():
            if orig in used:
                continue
            if norm == pat or (len(pat) > 4 and pat in norm):
                return orig
    return None


def analyze(df: pd.DataFrame, sheet: str = "") -> dict | None:
    if df is None or len(df) < 3 or df.shape[1] < 3:
        return None
    df = df.copy()
    cols = {_norm(c): c for c in df.columns if str(c).strip() and not str(c).lower().startswith("unnamed")}
    used: set = set()
    found = {}
    for role in ("balance", "dpd", "status", "borrower", "rate"):
        c = _find(cols, role, used)
        if c is not None:
            found[role] = c
            used.add(c)
    if "balance" not in found:
        return None
    bal = _num(df[found["balance"]])
    if bal.notna().mean() < 0.5:
        return None
    if not any(r in found for r in ("dpd", "status", "borrower", "rate")):
        return None

    df["_bal"] = bal
    df = df[df["_bal"].notna()]
    total = float(df["_bal"].sum())
    if total <= 0:
        return None
    out: dict = {"sheet": sheet, "rows": int(len(df)), "total_balance": round(total, 2),
                 "columns_used": {k: str(v) for k, v in found.items()}}

    if "rate" in found:
        r = _num(df[found["rate"]])
        ok = r.notna()
        if ok.any() and df.loc[ok, "_bal"].sum() > 0:
            w = float((r[ok] * df.loc[ok, "_bal"]).sum() / df.loc[ok, "_bal"].sum())
            out["weighted_avg_rate_pct"] = round(w * 100 if abs(w) <= 1 else w, 2)

    if "dpd" in found:
        d = _num(df[found["dpd"]]).fillna(0)
        buckets = [("current", d <= 0), ("1-30", (d >= 1) & (d <= 30)), ("31-60", (d >= 31) & (d <= 60)),
                   ("61-90", (d >= 61) & (d <= 90)), ("90+", d > 90)]
        out["dpd_buckets"] = {
            name: {"count": int(m.sum()), "balance": round(float(df.loc[m, "_bal"].sum()), 2),
                   "pct_of_balance": round(float(df.loc[m, "_bal"].sum()) / total * 100, 2)}
            for name, m in buckets
        }
        out["npl_ratio_90plus_pct"] = out["dpd_buckets"]["90+"]["pct_of_balance"]

    if "status" in found:
        st = df[found["status"]].astype(str).str.strip()
        grp = df.groupby(st)["_bal"].agg(["count", "sum"]).sort_values("sum", ascending=False).head(12)
        out["status_breakdown"] = {k: {"count": int(v["count"]), "balance": round(float(v["sum"]), 2),
                                       "pct_of_balance": round(float(v["sum"]) / total * 100, 2)}
                                   for k, v in grp.iterrows()}
        imp = st.str.contains(IMPAIRED)
        out["impaired_status_pct"] = round(float(df.loc[imp, "_bal"].sum()) / total * 100, 2)

    if "borrower" in found:
        by = df.groupby(df[found["borrower"]].astype(str).str.strip())["_bal"].sum().sort_values(ascending=False)
        out["borrower_count"] = int(len(by))
        out["largest_borrower_pct"] = round(float(by.iloc[0]) / total * 100, 2)
        out["top10_borrowers_pct"] = round(float(by.head(10).sum()) / total * 100, 2)
        out["top_borrowers"] = [{"name": str(k), "balance": round(float(v), 2),
                                 "pct": round(float(v) / total * 100, 2)} for k, v in by.head(5).items()]
    return out
