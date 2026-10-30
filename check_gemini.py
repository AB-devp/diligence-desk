"""Run `python check_gemini.py` before starting the app. It tells you whether your key and model work."""
import sys

from app import llm
from app.config import settings

key = settings.gemini_api_key.strip()
if not key:
    sys.exit("GEMINI_API_KEY is empty. Copy .env.example to .env and paste your key.")

kind = "authorization key (AQ.), the current AI Studio format" if key.startswith("AQ.") else \
       "standard key (AIza), the older AI Studio format" if key.startswith("AIza") else "unrecognised format"
print(f"Key: {key[:4]}...{key[-3:]}  ->  {kind}")
print(f"Backend: {settings.gemini_backend}   Model: {settings.gemini_model}")

try:
    names = sorted((m.name or "").split("/")[-1] for m in llm._client().models.list()
                   if "generateContent" in (getattr(m, "supported_actions", None) or []))
    print(f"\nModels this key can use for text generation ({len(names)}):")
    for n in names:
        print("  ", n)
except Exception as e:
    print(f"\n(Model listing isn't available for this key: {str(e)[:160]}. This does not stop the app from working.)")

print("\nSending a test request...")
try:
    reply = llm.generate_text("Reply with exactly: OK")
    print(f"PASS  model '{llm.model_in_use()}' replied: {reply.strip()[:40]}")
except llm.LLMError as e:
    sys.exit(f"FAIL  {e}")
