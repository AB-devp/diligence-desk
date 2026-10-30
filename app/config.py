from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Gemini API key from Google AI Studio. New keys start with "AQ." (authorization keys); older ones start with "AIza".
    # Both work with the Gemini API, which is the default backend.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    # "gemini" = Gemini API (AI Studio keys, the default). "vertex" = Vertex AI express mode (keys made in Google Cloud).
    gemini_backend: str = "gemini"

    data_dir: str = "./data"
    max_upload_mb: int = 50

    # Limits that keep a single request inside the model's context window.
    max_doc_chars: int = 400_000       # stored text per document
    max_context_chars: int = 500_000   # total text sent for analysis / memo
    ocr_max_pages: int = 25            # scanned PDF pages OCR'd per document
    chunk_chars: int = 3000
    chunk_overlap: int = 300


settings = Settings()
