# Diligence Desk

Financial due diligence on a deal room. Create a deal, drop in whatever the seller sent
(financial statements, loan tapes, contracts, customer lists, scans, photos of pages), and get:

- **Findings**: risks across financials, loan portfolio, customer concentration, deal terms and legal,
  each with severity, evidence and the file it came from. Plus missing information, questions for
  management, and a recommendation.
- **Loan portfolio numbers** calculated by code from every row of a loan tape (balance, weighted rate,
  days-past-due buckets, 90+ ratio, delinquent/default share, borrower concentration). The AI never
  does this arithmetic.
- **Investment committee memo** in Markdown (downloadable).
- **Ask the documents**: questions answered only from the uploaded files, citing the file name.

## Supported files

PDF (text and scanned), Word `.docx`, Excel `.xlsx` `.xlsm` `.xls`, CSV/TSV, PowerPoint `.pptx`,
images (PNG, JPG, WEBP, GIF, TIFF, BMP), TXT, MD, JSON, XML, HTML.
Scanned PDF pages and images are read with Gemini vision (OCR). Old `.doc` / `.ppt` files must be
re-saved as `.docx` / `.pptx` or PDF.

## Run it

You need Python 3.12 and a Gemini API key from https://aistudio.google.com/apikey. New AI Studio keys start with `AQ.`
(authorization keys); older `AIza` keys also work. Both use the Gemini API, which is the default.

```bash
python -m venv venv
# Windows:  venv\Scripts\activate        macOS/Linux:  source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env   then paste your key into .env
python check_gemini.py        # confirms the key and model work
uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Try the files in `sample_data/` (a fictional lender with a weak loan book).

### Docker

```bash
cp .env.example .env          # then paste your key
docker compose up --build
```

Data lives in the `dd_data` volume. `docker compose down -v` wipes it.

## How it works

One FastAPI process, one SQLite file in `data/`, one HTML page. No Postgres, vector database or
embedding model.

1. **Read**: each file is converted to text (`app/extract.py`). Spreadsheets keep their sheet structure; tables in
   Word files are kept; scans go through OCR. Spreadsheets that look like loan tapes also get statistics
   computed in code (`app/loan_tape.py`).
2. **Analyse**: all readable text (up to about 500k characters) plus the computed statistics go to Gemini in
   one request, with a JSON schema for the answer. The risk score is computed from the findings' severities
   in code (`app/analysis.py`).
3. **Ask**: questions search the deal's documents with SQLite full-text search and send the best excerpts to
   Gemini. Searches are always restricted to the selected deal.

## Be aware

- **Documents are sent to Google's Gemini API.** Check Google's data terms, or use a Vertex AI setup that
  meets your confidentiality requirements, before uploading real client or customer data.
- **No login.** Anyone who can open the page can see every deal. Run it on your own machine; don't expose it
  to the internet without adding authentication.
- AI output can be wrong. Findings cite files so you can check them, and the memo says to verify figures.
  This is a first-pass tool for a diligence team, not an audit or investment advice.
- The risk score is a simple weighted count of findings, not calibrated against real outcomes.
- Very large data rooms are truncated to fit the model's context (the analysis tells the model when text
  was cut). Questions still search everything.
- Files are analysed as text. Charts, signatures and stamps in PDFs are not interpreted unless the page is
  scanned.

## Troubleshooting

| You see | Cause and fix |
|---|---|
| "Google rejected the API key" | Paste the full key from AI Studio into `.env` (no quotes or spaces) and restart. Keys made in Google Cloud for Vertex AI need `GEMINI_BACKEND=vertex`. Run `python check_gemini.py`. |
| "isn't enabled in its Google Cloud project" | Open the link in the message and click Enable, wait a few minutes, or create a fresh key in AI Studio. |
| `ACCESS_TOKEN_TYPE_UNSUPPORTED` | Out-of-date client library: `pip install -U google-genai`. |
| "model ... is not available" | The model was retired. The app tries to switch to a current Flash model by itself; otherwise set `GEMINI_MODEL` in `.env` to one listed by `check_gemini.py`. |
| Document stuck on `failed` | The message under the file name says why (password-protected, unsupported type, empty file). |
| "rate limit or quota" | Wait a minute, or check your Google quota. |
| Analysis is slow | A large data room can take a minute or two. The button shows progress until it finishes. |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

The tests cover every file format, the loan-tape maths, per-deal search isolation, error messages, the retired-model
logic, and that an `AQ.` key is sent to the Gemini API in the `x-goog-api-key` header. They stub the Gemini calls, so they run offline.
