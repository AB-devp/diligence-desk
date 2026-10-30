"""Thin wrapper around the Gemini API: text, JSON and image OCR calls.

- AI Studio keys (new "AQ." authorization keys and old "AIza" keys) use the Gemini API. The SDK sends the
  key in the x-goog-api-key header. Vertex AI is only used when GEMINI_BACKEND=vertex.
- If the configured model has been retired (404), we look for a current flash model
  the key can use and switch to it for this process.
- Transient errors (429/5xx) are retried with backoff.
"""
import json
import logging
import re
import time

from google import genai
from google.genai import types

from .config import settings

log = logging.getLogger("dd.llm")


class LLMError(RuntimeError):
    pass


_client_obj = None
_model_override: str | None = None


def configured() -> bool:
    return bool(settings.gemini_api_key.strip())


def model_in_use() -> str:
    return _model_override or settings.gemini_model


def _client():
    global _client_obj
    if _client_obj is None:
        key = settings.gemini_api_key.strip()
        if not key:
            raise LLMError("GEMINI_API_KEY is not set. Add it to .env and restart the app.")
        _client_obj = genai.Client(vertexai=settings.gemini_backend.strip().lower() == "vertex", api_key=key)
    return _client_obj


def _is_not_found(msg: str) -> bool:
    return bool(re.search(r"\b404\b|not[_ ]found", msg.lower()))


def _is_transient(msg: str) -> bool:
    return bool(re.search(r"\b(429|500|502|503|504)\b|unavailable|overloaded|deadline|resource_exhausted", msg.lower()))


def _friendly(msg: str) -> str:
    m = msg.lower()
    if "service_disabled" in m or "has not been used in project" in m or "api is disabled" in m or "or it is disabled" in m:
        url = re.search(r"https://[^\s'\"]+", msg)
        where = f"Open {url.group(0).rstrip('.,)')} and click Enable" if url else "Enable the API in the Google Cloud console"
        return (f"The key is valid, but the API it calls isn't enabled in its Google Cloud project. {where}, wait a few "
                "minutes, then try again. If you did not create this key in Google AI Studio, create a new one at "
                "https://aistudio.google.com/apikey, which sets this up for you. "
                f"(Backend in use: {settings.gemini_backend}. Details: {msg[:160]})")
    if "access_token_type_unsupported" in m:
        return ("Google didn't accept how the key was presented (ACCESS_TOKEN_TYPE_UNSUPPORTED). This usually means an "
                "out-of-date client library. Run `pip install -U google-genai` and try again. If it still fails, "
                "create a fresh key in Google AI Studio. Details: " + msg[:200])
    if "api key not valid" in m or "api_key_invalid" in m or re.search(r"\b(401|403)\b", m) or "permission" in m:
        return ("Google rejected the API key. Check that GEMINI_API_KEY in .env is the full key from Google AI Studio "
                "(new keys start with 'AQ.'), with no quotes or spaces, and that the app was restarted after you "
                "edited .env. Keys made in Google Cloud for Vertex AI need GEMINI_BACKEND=vertex. "
                f"(Backend in use: {settings.gemini_backend}. Details: {msg[:200]})")
    if _is_not_found(m):
        return (f"Gemini model '{model_in_use()}' is not available for this key. "
                "Run `python check_gemini.py`, then set GEMINI_MODEL in .env to a model it lists as working. "
                "Details: " + msg[:200])
    if "429" in m or "quota" in m or "resource_exhausted" in m:
        return "Gemini rate limit or quota reached. Wait a minute and retry, or check your quota. Details: " + msg[:200]
    return "Gemini request failed: " + msg[:300]


def _pick_fallback_model(exclude: str) -> str | None:
    bad = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "8b", "thinking", "robotics")
    try:
        names = []
        for m in _client().models.list():
            name = (m.name or "").split("/")[-1]
            actions = getattr(m, "supported_actions", None) or []
            if "flash" in name and "generateContent" in actions and not any(b in name for b in bad):
                names.append(name)
    except Exception as e:  # listing is best-effort
        log.warning("Could not list Gemini models: %s", e)
        return None
    names = [n for n in names if n != exclude]
    # prefer stable over preview, then highest name (newest version)
    names.sort(key=lambda n: ("preview" not in n, n), reverse=True)
    return names[0] if names else None


def _generate(contents, config) -> str:
    global _model_override
    tried_fallback = False
    last = ""
    for attempt in range(4):
        model = model_in_use()
        try:
            resp = _client().models.generate_content(model=model, contents=contents, config=config)
            text = resp.text
            if not text:
                raise LLMError("Gemini returned an empty response (it may have been blocked by safety filters).")
            return text
        except LLMError:
            raise
        except Exception as e:
            last = str(e)
            if _is_not_found(last) and not tried_fallback:
                tried_fallback = True
                alt = _pick_fallback_model(exclude=model)
                if alt:
                    log.warning("Model %s not available, switching to %s", model, alt)
                    _model_override = alt
                    continue
            if _is_transient(last) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            break
    raise LLMError(_friendly(last))


def generate_text(prompt: str, system: str | None = None, temperature: float = 0.3) -> str:
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
    return _generate(prompt, cfg)


def generate_json(prompt: str, schema, system: str | None = None) -> dict:
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.2,
    )
    last_err = None
    for _ in range(2):
        text = _generate(prompt, cfg)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last_err = e
    raise LLMError(f"Gemini returned malformed JSON twice: {last_err}")


def ocr_image(data: bytes, mime: str = "image/png") -> str:
    prompt = (
        "Transcribe all text in this document image exactly as written. Keep numbers, currency symbols "
        "and signs exact. Render tables as rows with cells separated by ' | '. "
        "Output only the transcription, no commentary. If the image has no readable text, output nothing."
    )
    cfg = types.GenerateContentConfig(temperature=0.0)
    return _generate([types.Part.from_bytes(data=data, mime_type=mime), prompt], cfg).strip()
