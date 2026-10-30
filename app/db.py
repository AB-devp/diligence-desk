import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, company_name TEXT NOT NULL, deal_type TEXT, deal_size TEXT,
  notes TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT, kind TEXT,
  status TEXT, error TEXT, pages INTEGER, chars INTEGER, text TEXT,
  loan_stats TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_docs_project ON documents(project_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
  text, project_id UNINDEXED, document_id UNINDEXED, filename UNINDEXED,
  tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS analyses(
  id TEXT PRIMARY KEY, project_id TEXT, created_at REAL, data TEXT);
CREATE TABLE IF NOT EXISTS reports(
  id TEXT PRIMARY KEY, project_id TEXT, created_at REAL, markdown TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, role TEXT,
  content TEXT, created_at REAL);
"""


def data_path() -> Path:
    p = Path(settings.data_dir)
    (p / "uploads").mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def db():
    con = sqlite3.connect(data_path() / "dd.sqlite3", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()
