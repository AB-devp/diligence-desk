import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import analysis, db as dbm, llm
from .config import settings
from .db import data_path, db, new_id, now
from .extract import ExtractError, SUPPORTED, chunk_text, extract

log = logging.getLogger("dd")


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbm.init()
    yield


app = FastAPI(title="Diligence Desk", lifespan=lifespan)
INDEX = Path(__file__).parent / "static" / "index.html"


@app.exception_handler(llm.LLMError)
async def llm_error(_, exc: llm.LLMError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


def _require_project(pid: str) -> dict:
    with db() as con:
        r = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "Deal not found")
    return dict(r)


def _doc(r) -> dict:
    return {"id": r["id"], "filename": r["filename"], "kind": r["kind"], "status": r["status"],
            "error": r["error"], "pages": r["pages"], "chars": r["chars"],
            "loan_tape": bool(json.loads(r["loan_stats"] or "[]")), "created_at": r["created_at"]}


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": llm.configured(), "model": llm.model_in_use(), "supported": SUPPORTED}


# ---------- deals ----------
class ProjectIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    deal_type: str = Field(default="", max_length=100)
    deal_size: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=5000)


@app.post("/api/projects")
def create_project(body: ProjectIn):
    pid = new_id()
    with db() as con:
        con.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                    (pid, body.company_name.strip(), body.deal_type.strip(), body.deal_size.strip(), body.notes.strip(), now()))
    return _require_project(pid)


@app.get("/api/projects")
def list_projects():
    with db() as con:
        rows = con.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _require_project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    _require_project(pid)
    with db() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM documents WHERE project_id=?", (pid,))]
        con.execute("DELETE FROM chunks WHERE project_id=?", (pid,))
        for t in ("documents", "analyses", "reports", "messages"):
            con.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        con.execute("DELETE FROM projects WHERE id=?", (pid,))
    for did in ids:
        _remove_files(did)
    return {"ok": True}


# ---------- documents ----------
def _remove_files(did: str):
    for f in (data_path() / "uploads").glob(f"{did}*"):
        f.unlink(missing_ok=True)


def _fail(did: str, msg: str):
    with db() as con:
        con.execute("UPDATE documents SET status='failed', error=? WHERE id=?", (msg, did))


def process_document(did: str, path: Path, name: str):
    with db() as con:
        row = con.execute("SELECT project_id FROM documents WHERE id=?", (did,)).fetchone()
        if not row:
            return
        pid = row["project_id"]
        con.execute("UPDATE documents SET status='processing' WHERE id=?", (did,))
    try:
        ex = extract(path, name)
        text = ex.text.strip()
        if not text:
            raise ExtractError("No readable text found in this file.")
        if len(text) > settings.max_doc_chars:
            text = text[: settings.max_doc_chars] + "\n[... truncated: document is very long ...]"
        chunks = chunk_text(text)
        with db() as con:
            if not con.execute("SELECT 1 FROM documents WHERE id=?", (did,)).fetchone():
                return  # deleted while processing
            con.execute("DELETE FROM chunks WHERE document_id=?", (did,))
            con.executemany("INSERT INTO chunks(text, project_id, document_id, filename) VALUES (?,?,?,?)",
                            [(c, pid, did, name) for c in chunks])
            con.execute(
                "UPDATE documents SET status='ready', error=NULL, kind=?, pages=?, chars=?, text=?, loan_stats=? WHERE id=?",
                (ex.kind, ex.pages, len(text), text, json.dumps(ex.loan_stats), did))
    except ExtractError as e:
        _fail(did, str(e))
    except Exception as e:
        log.exception("Processing %s failed", name)
        _fail(did, f"Couldn't read this file ({type(e).__name__}: {str(e)[:200]}).")


@app.post("/api/projects/{pid}/documents")
async def upload(pid: str, background: BackgroundTasks, files: list[UploadFile] = File(...)):
    _require_project(pid)
    limit = settings.max_upload_mb * 1024 * 1024
    out = []
    for f in files:
        name = Path(f.filename or "file").name[:200] or "file"
        did = new_id()
        dest = data_path() / "uploads" / f"{did}{Path(name).suffix.lower()[:10]}"
        size, too_big = 0, False
        with open(dest, "wb") as fh:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    too_big = True
                    break
                fh.write(chunk)
        status, err = "queued", None
        if too_big:
            status, err = "failed", f"File is larger than {settings.max_upload_mb} MB."
            dest.unlink(missing_ok=True)
        elif size == 0:
            status, err = "failed", "The file is empty."
        with db() as con:
            con.execute("INSERT INTO documents(id, project_id, filename, status, error, created_at) VALUES (?,?,?,?,?,?)",
                        (did, pid, name, status, err, now()))
            r = con.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if status == "queued":
            background.add_task(process_document, did, dest, name)
        out.append(_doc(r))
    return out


@app.get("/api/projects/{pid}/documents")
def list_documents(pid: str):
    _require_project(pid)
    with db() as con:
        rows = con.execute("SELECT * FROM documents WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    return [_doc(r) for r in rows]


@app.delete("/api/documents/{did}")
def delete_document(did: str):
    with db() as con:
        con.execute("DELETE FROM chunks WHERE document_id=?", (did,))
        con.execute("DELETE FROM documents WHERE id=?", (did,))
    _remove_files(did)
    return {"ok": True}


# ---------- analysis / memo / chat ----------
def _guard(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/projects/{pid}/portfolios")
def get_portfolios(pid: str):
    _require_project(pid)
    return analysis.portfolios(pid)


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str):
    _require_project(pid)
    return _guard(analysis.run_analysis, pid)


@app.get("/api/projects/{pid}/analysis")
def get_analysis(pid: str):
    _require_project(pid)
    return analysis.latest_analysis(pid)


@app.post("/api/projects/{pid}/memo")
def make_memo(pid: str):
    _require_project(pid)
    return {"markdown": _guard(analysis.generate_memo, pid)}


@app.get("/api/projects/{pid}/memo")
def get_memo(pid: str):
    _require_project(pid)
    return {"markdown": analysis.latest_memo(pid)}


@app.get("/api/projects/{pid}/memo.md")
def download_memo(pid: str):
    p = _require_project(pid)
    md = analysis.latest_memo(pid)
    if not md:
        raise HTTPException(404, "No memo yet")
    slug = re.sub(r"[^a-z0-9]+", "-", p["company_name"].lower()).strip("-") or "deal"
    return Response(md, media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-dd-memo.md"'})


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.post("/api/projects/{pid}/chat")
def chat(pid: str, body: ChatIn):
    _require_project(pid)
    return {"role": "assistant", "content": _guard(analysis.chat, pid, body.message)}


@app.get("/api/projects/{pid}/chat")
def chat_history(pid: str):
    _require_project(pid)
    with db() as con:
        rows = con.execute("SELECT role, content FROM messages WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    return [dict(r) for r in rows]
