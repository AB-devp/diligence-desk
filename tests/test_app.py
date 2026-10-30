"""Offline tests: everything except the real Gemini call (which is stubbed)."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app import llm


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(llm, "ocr_image", lambda data, mime="image/png": "SCANNED PAGE revenue 5,000,000 customer Acme Corp")
    from app.main import app
    with TestClient(app) as c:
        yield c


LOANS = (
    "Loan ID,Borrower,Outstanding Balance,Days Past Due,Interest Rate,Status\n"
    "1,Acme Ltd,1000,0,0.10,Current\n"
    "2,Acme Ltd,1000,0,0.10,Current\n"
    "3,Beta LLC,2000,15,0.12,Current\n"
    "4,Gamma Inc,2000,45,0.12,Delinquent\n"
    "5,Delta SA,1000,75,0.14,Delinquent\n"
    "6,Eps Co,2000,120,0.14,Default\n"
    "7,Zeta Co,1000,0,0.10,Current\n"
)


def make_files():
    import openpyxl, pymupdf, docx, pptx
    from PIL import Image
    files = {}
    files["loans.csv"] = LOANS.encode()
    files["notes.txt"] = b"Management notes: the largest customer Zorbex represents 40% of revenue."
    files["page.html"] = b"<html><body><script>x=1</script><h1>Term sheet</h1><p>Change of control clause</p></body></html>"

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "P&L"
    ws.append(["Acme Holdings - Income statement"])
    ws.append(["Line", "FY2023", "FY2024"]); ws.append(["Revenue", 1000000, 1250000]); ws.append(["EBITDA", 150000, 90000])
    b = io.BytesIO(); wb.save(b); files["pnl.xlsx"] = b.getvalue()

    d = docx.Document(); d.add_paragraph("Credit agreement: covenant on net debt / EBITDA below 3.0x.")
    t = d.add_table(rows=2, cols=2); t.cell(0, 0).text = "Facility"; t.cell(0, 1).text = "USD 5,000,000"
    b = io.BytesIO(); d.save(b); files["credit.docx"] = b.getvalue()

    prs = pptx.Presentation(); s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.title.text = "Investor deck: ARR 12M"
    b = io.BytesIO(); prs.save(b); files["deck.pptx"] = b.getvalue()

    pdf = pymupdf.open(); page = pdf.new_page(); page.insert_text((72, 72), "Audited statements. Total debt 4.2 million. " * 3)
    files["audited.pdf"] = pdf.tobytes()

    img = Image.new("RGB", (200, 100), "white"); b = io.BytesIO(); img.save(b, "PNG"); png = b.getvalue()
    scan = pymupdf.open(); sp = scan.new_page(); sp.insert_image(sp.rect, stream=png)
    files["scanned.pdf"] = scan.tobytes()
    files["receipt.png"] = png
    return files


def upload_all(client, pid, files):
    r = client.post(f"/api/projects/{pid}/documents", files=[("files", (n, c)) for n, c in files.items()])
    assert r.status_code == 200, r.text
    return {d["filename"]: d for d in client.get(f"/api/projects/{pid}/documents").json()}


def new_project(client, name="Acme Holdings"):
    return client.post("/api/projects", json={"company_name": name, "deal_type": "Acquisition", "deal_size": "$20M"}).json()["id"]


def test_all_formats_extract(client):
    pid = new_project(client)
    docs = upload_all(client, pid, make_files())
    bad = {n: d["error"] for n, d in docs.items() if d["status"] != "ready"}
    assert not bad, bad
    assert docs["loans.csv"]["loan_tape"] is True
    assert docs["pnl.xlsx"]["loan_tape"] is False
    # text really reached the database
    from app.db import db
    with db() as con:
        text = {r["filename"]: r["text"] for r in con.execute("SELECT filename, text FROM documents")}
    assert "1250000" in text["pnl.xlsx"] and "EBITDA" in text["pnl.xlsx"]
    assert "net debt / EBITDA" in text["credit.docx"] and "USD 5,000,000" in text["credit.docx"]
    assert "ARR 12M" in text["deck.pptx"]
    assert "Total debt 4.2 million" in text["audited.pdf"]
    assert "Acme Corp" in text["scanned.pdf"] and "Acme Corp" in text["receipt.png"]  # OCR path
    assert "Change of control" in text["page.html"] and "x=1" not in text["page.html"]
    assert "Zorbex" in text["notes.txt"]


def test_loan_tape_math(client):
    pid = new_project(client)
    upload_all(client, pid, {"loans.csv": LOANS.encode()})
    from app.db import db
    with db() as con:
        s = json.loads(con.execute("SELECT loan_stats FROM documents").fetchone()["loan_stats"])[0]
    assert s["total_balance"] == 10000 and s["rows"] == 7
    assert s["dpd_buckets"]["90+"]["balance"] == 2000 and s["npl_ratio_90plus_pct"] == 20.0
    assert s["dpd_buckets"]["31-60"]["balance"] == 2000 and s["dpd_buckets"]["current"]["count"] == 3 and s["dpd_buckets"]["1-30"]["count"] == 1
    assert s["largest_borrower_pct"] == 20.0 and s["borrower_count"] == 6
    assert s["weighted_avg_rate_pct"] == pytest.approx(12.0, abs=0.01)  # (100+100+240+240+140+280+100)/10000
    assert s["impaired_status_pct"] == 50.0  # Delinquent 3000 + Default 2000


def test_unsupported_and_empty_files(client):
    pid = new_project(client)
    docs = upload_all(client, pid, {"old.doc": b"binary", "empty.txt": b"", "blank.txt": b"   \n ", "tool.exe": b"MZ"})
    assert all(d["status"] == "failed" for d in docs.values())
    assert "docx" in docs["old.doc"]["error"] and "supported" in docs["tool.exe"]["error"]


FAKE_ANALYSIS = {
    "company_summary": "Acme makes things.",
    "key_metrics": [{"name": "Revenue", "value": "USD 1.25M", "period": "FY2024", "source_file": "pnl.xlsx"}],
    "findings": [
        {"category": "Credit Portfolio", "title": "Low", "severity": "low", "explanation": "e", "evidence": "x", "source_file": "loans.csv"},
        {"category": "financial", "title": "EBITDA down", "severity": "HIGH", "explanation": "e", "evidence": "150k to 90k", "source_file": "pnl.xlsx"},
        {"category": "weird", "title": "Mid", "severity": "Medium", "explanation": "e", "evidence": "x", "source_file": "a"},
    ],
    "missing_information": ["Audited FY2022"], "questions_for_management": ["Why did EBITDA fall?"],
    "recommendation": "Proceed with conditions", "recommendation_rationale": "Because.",
}


def test_analysis_memo_chat(client, monkeypatch):
    pid = new_project(client)
    upload_all(client, pid, make_files())
    seen = {}

    def fake_json(prompt, schema, system=None):
        seen["prompt"] = prompt
        return FAKE_ANALYSIS
    monkeypatch.setattr(llm, "generate_json", fake_json)
    monkeypatch.setattr(llm, "generate_text", lambda prompt, system=None, temperature=0.3: "## Executive summary\nok\n" if "memo" in prompt.lower() else "ANSWER\n" + prompt)

    r = client.post(f"/api/projects/{pid}/analyze"); assert r.status_code == 200, r.text
    a = r.json()
    assert [f["severity"] for f in a["findings"]] == ["High", "Medium", "Low"]          # sorted + normalised
    assert a["findings"][1]["category"] == "operational" and a["findings"][2]["category"] == "credit_portfolio"
    assert a["counts"] == {"High": 1, "Medium": 1, "Low": 1} and a["risk_score"] == round(100 * (1 - 2.718281828 ** (-12 / 20)))
    assert "npl_ratio_90plus_pct" in seen["prompt"] and "=== FILE: pnl.xlsx ===" in seen["prompt"]  # stats + docs reach the model
    assert client.get(f"/api/projects/{pid}/analysis").json()["risk_score"] == a["risk_score"]

    m = client.post(f"/api/projects/{pid}/memo").json()["markdown"]
    assert "Executive summary" in m and "Verify every figure" in m
    assert client.get(f"/api/projects/{pid}/memo.md").headers["content-disposition"].startswith("attachment")

    answer = client.post(f"/api/projects/{pid}/chat", json={"message": "How much of revenue is the Zorbex customer?"}).json()["content"]
    assert "Zorbex" in answer and "notes.txt" in answer     # retrieval found the right file
    assert len(client.get(f"/api/projects/{pid}/chat").json()) == 2


def test_chat_is_scoped_to_project(client, monkeypatch):
    a, b = new_project(client, "A Co"), new_project(client, "B Co")
    upload_all(client, a, {"a.txt": b"Alpha financials revenue 100"})
    upload_all(client, b, {"b.txt": b"SECRET Bravo revenue 999 confidential"})
    monkeypatch.setattr(llm, "generate_text", lambda prompt, system=None, temperature=0.3: prompt)
    ans = client.post(f"/api/projects/{a}/chat", json={"message": "what is the revenue?"}).json()["content"]
    assert "Alpha" in ans and "SECRET" not in ans


def test_errors_are_readable(client, monkeypatch):
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/analyze")
    assert r.status_code == 400 and "No processed documents" in r.json()["detail"]   # plain string, not an object
    upload_all(client, pid, {"a.txt": b"hello world revenue"})
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("Gemini rejected the API key.")))
    r = client.post(f"/api/projects/{pid}/analyze")
    assert r.status_code == 502 and r.json()["detail"] == "Gemini rejected the API key."
    assert client.post("/api/projects", json={"company_name": ""}).status_code == 422
    assert client.get("/api/projects/nope").status_code == 404


def test_delete_document_and_project(client):
    pid = new_project(client)
    docs = upload_all(client, pid, {"a.txt": b"hello revenue"})
    assert client.delete(f"/api/documents/{docs['a.txt']['id']}").status_code == 200
    assert client.get(f"/api/projects/{pid}/documents").json() == []
    upload_all(client, pid, {"b.txt": b"more text here"})
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


# ---------- llm wrapper logic (no network) ----------
def test_backend_selection(monkeypatch):
    """AI Studio keys (AQ. and AIza) use the Gemini API. Vertex only when explicitly requested."""
    from app.config import settings
    calls = []
    monkeypatch.setattr(llm.genai, "Client", lambda **kw: calls.append(kw) or object())
    for key, backend, expect_vertex in (("AQ.abc", "gemini", False), ("AIzaabc", "gemini", False),
                                        ("AQ.abc", "vertex", True), ("AQ.abc", " Vertex ", True)):
        monkeypatch.setattr(settings, "gemini_api_key", key); monkeypatch.setattr(settings, "gemini_backend", backend)
        monkeypatch.setattr(llm, "_client_obj", None)
        llm._client()
        assert calls[-1] == {"vertexai": expect_vertex, "api_key": key}, (key, backend)


def test_aq_key_goes_to_gemini_api_route_in_header(monkeypatch):
    """Wire-level: a stand-in server records what the real SDK sends for an AQ. key."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from google.genai import types
    from app.config import settings

    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append((self.path, self.headers.get("x-goog-api-key")))
            body = b'{"candidates":[{"content":{"parts":[{"text":"OK"}],"role":"model"},"finishReason":"STOP"}]}'
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = llm.genai.Client
    monkeypatch.setattr(llm.genai, "Client", lambda **kw: real(http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{srv.server_port}"), **kw))
    monkeypatch.setattr(settings, "gemini_api_key", "AQ.Ab8TESTKEY"); monkeypatch.setattr(settings, "gemini_backend", "gemini")
    monkeypatch.setattr(llm, "_client_obj", None); monkeypatch.setattr(llm, "_model_override", None)
    try:
        assert llm.generate_text("hi") == "OK"
    finally:
        srv.shutdown()
    path, header = seen[0]
    assert header == "AQ.Ab8TESTKEY" and "AQ." not in path          # key in header, never in the URL
    assert path.startswith("/v1beta/models/") and "publishers" not in path   # Gemini API route, not Vertex


def test_retired_model_falls_back(monkeypatch):
    class M:
        def __init__(self, name): self.name, self.supported_actions = "models/" + name, ["generateContent"]

    class Models:
        def generate_content(self, model, contents, config):
            if model == "old-flash":
                raise Exception("404 NOT_FOUND. models/old-flash is not found")
            return type("R", (), {"text": f"ok from {model}"})()
        def list(self):
            return [M("gemini-9-flash-lite"), M("gemini-9-flash"), M("gemini-9-flash-image"), M("gemini-8-flash"), M("gemini-9-pro")]

    fake = type("C", (), {"models": Models()})()
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_model", "old-flash"); monkeypatch.setattr(llm, "_client_obj", fake); monkeypatch.setattr(llm, "_model_override", None)
    assert llm.generate_text("hi") == "ok from gemini-9-flash"
    assert llm.model_in_use() == "gemini-9-flash"


def test_disabled_api_gets_an_accurate_message():
    raw = ("403 PERMISSION_DENIED. {'error': {'code': 403, 'message': 'Agent Platform API has not been used in project 123 "
           "before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/x.googleapis.com/overview?project=123 then retry.'}}")
    msg = llm._friendly(raw)
    assert "isn't enabled" in msg and "https://console.developers.google.com/apis/api/x.googleapis.com/overview?project=123" in msg
    assert "aistudio.google.com/apikey" in msg and "rejected" not in msg
    assert "rejected the API key" in llm._friendly("400 API key not valid. Please pass a valid API key.")  # genuine bad keys unchanged


def test_other_auth_errors_have_specific_messages():
    m = llm._friendly("401 UNAUTHENTICATED. reason: ACCESS_TOKEN_TYPE_UNSUPPORTED")
    assert "pip install -U google-genai" in m
    assert "AQ." in llm._friendly("400 API key not valid. Please pass a valid API key.")
