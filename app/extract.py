"""Turn uploaded files of many formats into plain text (plus loan-tape stats).

Supported: PDF (text and scanned), Word .docx, Excel .xlsx/.xlsm/.xls, CSV/TSV,
PowerPoint .pptx, images (PNG/JPG/WEBP/GIF/TIFF/BMP), TXT/MD/JSON/XML/HTML.
Scanned pages and images are transcribed with Gemini vision.
"""
import io
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

from . import llm, loan_tape
from .config import settings


class ExtractError(Exception):
    pass


@dataclass
class Extracted:
    text: str
    kind: str
    pages: int | None = None
    loan_stats: list = field(default_factory=list)


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
TEXT_EXT = {".txt", ".md", ".log", ".json", ".xml", ".html", ".htm", ".tsv"}
SUPPORTED = "PDF, DOCX, XLSX/XLS/XLSM, CSV/TSV, PPTX, images (PNG, JPG, WEBP, GIF, TIFF, BMP), TXT, MD, JSON, XML, HTML"


def extract(path: Path, filename: str) -> Extracted:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    if ext == ".docx":
        return _docx(path)
    if ext in {".xlsx", ".xlsm", ".xls"}:
        return _excel(path, ext)
    if ext in {".csv", ".tsv"}:
        return _csv(path)
    if ext == ".pptx":
        return _pptx(path)
    if ext in IMAGE_EXT:
        return _image(path)
    if ext in TEXT_EXT:
        return _text(path, ext)
    if ext in {".doc", ".ppt", ".rtf", ".pages", ".numbers", ".key"}:
        raise ExtractError(f"{ext} files aren't supported. Open it and save as .docx, .pptx or PDF, then upload again.")
    raise ExtractError(f"'{ext or 'no extension'}' isn't a supported file type. Supported: {SUPPORTED}.")


# ---------- PDF ----------
def _pdf(path: Path) -> Extracted:
    import pymupdf

    doc = pymupdf.open(path)
    if doc.needs_pass:
        raise ExtractError("This PDF is password protected. Remove the password and upload it again.")
    parts, ocr_used = [], 0
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if len(text) < 40:  # likely a scan or an image-only page
            if ocr_used < settings.ocr_max_pages:
                ocr_used += 1
                try:
                    pix = page.get_pixmap(dpi=150)
                    ocr = llm.ocr_image(pix.tobytes("png"), "image/png")
                    text = (text + "\n" + ocr).strip()
                except llm.LLMError as e:
                    text = (text + f"\n[scanned page - OCR failed: {e}]").strip()
            else:
                text = (text + "\n[scanned page - skipped: OCR page limit reached]").strip()
        parts.append(f"[Page {i + 1}]\n{text}")
    return Extracted("\n\n".join(parts), "pdf", pages=len(doc))


# ---------- Word ----------
def _docx(path: Path) -> Extracted:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(str(path))
    lines = []
    for child in d.element.body.iterchildren():
        if child.tag.endswith("}p"):
            t = Paragraph(child, d).text.strip()
            if t:
                lines.append(t)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, d).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip().replace("\n", " ")
                    if not cells or cells[-1] != t:  # merged cells repeat
                        cells.append(t)
                lines.append(" | ".join(cells))
            lines.append("")
    return Extracted("\n".join(lines), "docx")


# ---------- Spreadsheets ----------
def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else f"{v:.10g}"
    s = str(v).strip()
    return s[:-9] if s.endswith(" 00:00:00") else s


def _render(raw: pd.DataFrame, max_rows: int = 250, max_cols: int = 40) -> str:
    lines = []
    for _, row in raw.iloc[:max_rows, :max_cols].iterrows():
        cells = [_cell(v) for v in row]
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            lines.append(" | ".join(cells))
    if len(raw) > max_rows:
        lines.append(f"... {len(raw) - max_rows} more rows not shown ({len(raw)} rows in total)")
    return "\n".join(lines)


def _with_header(raw: pd.DataFrame):
    """Find the header row (first row with the most text cells) and return a DataFrame."""
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if len(raw) < 3:
        return None
    best, best_n = 0, -1
    for i in range(min(15, len(raw))):
        n = sum(isinstance(v, str) and v.strip() != "" for v in raw.iloc[i])
        if n > best_n:
            best, best_n = i, n
    if best_n < 3:
        return None
    names, seen = [], {}
    for j, v in enumerate(raw.iloc[best]):
        name = v.strip() if isinstance(v, str) and v.strip() else f"col{j}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    df = raw.iloc[best + 1:].copy()
    df.columns = names
    return df.reset_index(drop=True)


def _numeric_totals(df: pd.DataFrame) -> str:
    out = []
    for c in df.columns[:60]:
        s = loan_tape._num(df[c])
        if s.notna().mean() >= 0.8 and s.notna().sum() >= 3:
            out.append(f"{c}: sum={s.sum():.10g}, min={s.min():.10g}, max={s.max():.10g}, count={int(s.notna().sum())}")
        if len(out) >= 15:
            break
    return "Numeric column totals over ALL rows:\n" + "\n".join(out) if out else ""


def _sheets_to_extracted(sheets: dict, kind: str) -> Extracted:
    parts, stats = [], []
    for name, raw in sheets.items():
        if raw is None or raw.dropna(how="all").empty:
            continue
        block = [f"[Sheet: {name}]", _render(raw)]
        df = _with_header(raw)
        if df is not None:
            if len(df) > 250:
                block.append(_numeric_totals(df))
            s = loan_tape.analyze(df, sheet=str(name))
            if s:
                stats.append(s)
        parts.append("\n".join(b for b in block if b))
    return Extracted("\n\n".join(parts), kind, pages=len(sheets), loan_stats=stats)


def _excel(path: Path, ext: str) -> Extracted:
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, nrows=200_000)
    except ImportError as e:
        raise ExtractError(f"Missing Excel reader: {e}")
    except Exception as e:
        raise ExtractError(f"Couldn't read this Excel file ({type(e).__name__}: {str(e)[:150]}). Is it password protected or corrupted?")
    return _sheets_to_extracted(sheets, "excel")


def _csv(path: Path) -> Extracted:
    last = None
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            raw = pd.read_csv(path, header=None, sep=None, engine="python", dtype=object,
                              nrows=200_000, encoding=enc, on_bad_lines="skip")
            return _sheets_to_extracted({path.stem: raw}, "csv")
        except (UnicodeError, pd.errors.ParserError) as e:
            last = e
        except pd.errors.EmptyDataError:
            raise ExtractError("The CSV file is empty.")
    raise ExtractError(f"Couldn't parse this CSV file: {last}")


# ---------- PowerPoint ----------
def _pptx(path: Path) -> Extracted:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        lines = [f"[Slide {i}]"]
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            lines.append("Notes: " + slide.notes_slide.notes_text_frame.text.strip())
        parts.append("\n".join(lines))
    return Extracted("\n\n".join(parts), "pptx", pages=len(parts))


# ---------- Images ----------
def _image(path: Path) -> Extracted:
    from PIL import Image, ImageOps, ImageSequence

    try:
        img = Image.open(path)
    except Exception:
        raise ExtractError("This image couldn't be opened. Is the file corrupted?")
    parts = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        if i >= settings.ocr_max_pages:
            parts.append("[more frames skipped: OCR page limit reached]")
            break
        f = ImageOps.exif_transpose(frame.copy()).convert("RGB")
        f.thumbnail((2400, 2400))
        buf = io.BytesIO()
        f.save(buf, "PNG")
        parts.append(f"[Image {i + 1}]\n" + llm.ocr_image(buf.getvalue(), "image/png"))
    return Extracted("\n\n".join(parts), "image", pages=len(parts))


# ---------- Plain text family ----------
class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "br", "tr", "div", "li", "h1", "h2", "h3", "h4", "table"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _text(path: Path, ext: str) -> Extracted:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeError:
            continue
    if ext in (".html", ".htm"):
        p = _Strip()
        p.feed(s)
        s = re.sub(r"\n\s*\n+", "\n\n", "".join(p.out))
    elif ext == ".json":
        try:
            s = json.dumps(json.loads(s), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    return Extracted(s.strip(), ext.lstrip("."))


# ---------- Chunking for chat retrieval ----------
def chunk_text(text: str) -> list[str]:
    size, overlap = settings.chunk_chars, settings.chunk_overlap
    chunks, cur, cur_len = [], [], 0
    for line in text.split("\n"):
        while len(line) > size:  # hard-split very long lines
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            chunks.append(line[:size])
            line = line[size - overlap:]
        if cur_len + len(line) + 1 > size and cur:
            chunks.append("\n".join(cur))
            keep, keep_len = [], 0
            for prev in reversed(cur):
                if keep_len + len(prev) > overlap:
                    break
                keep.insert(0, prev)
                keep_len += len(prev) + 1
            cur, cur_len = keep, keep_len
        cur.append(line)
        cur_len += len(line) + 1
    if cur and "\n".join(cur).strip():
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]
