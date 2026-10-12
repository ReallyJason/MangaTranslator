"""Configuration loaded from server/.env (and the process environment)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVER_DIR / ".env"

log = logging.getLogger("fukidashi.config")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a core dependency
    load_dotenv = None


DEFAULT_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",
    "compatible": "",
}

PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "compatible": "compatible",
    "openai_compatible": "compatible",
    "openai-compatible": "compatible",
    "ollama": "compatible",
    "lmstudio": "compatible",
    "lm_studio": "compatible",
    "openrouter": "compatible",
    "none": "none",
    "off": "none",
    "": "none",
}


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 5003

    llm_provider: str = "anthropic"  # anthropic | openai | compatible | none
    llm_model: str = ""
    llm_timeout: int = 120
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = ""
    openai_base_url: str = ""

    deepl_api_key: str = ""
    google_api_key: str = ""

    use_gpu: str = "auto"  # auto | true | false
    inpainter: str = "lama"  # lama | opencv
    lama_url: str = (
        "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt"
    )
    use_manga_ocr: bool = True

    cache_max_mb: int = 1024

    data_dir: Path = field(default_factory=lambda: SERVER_DIR / "data")
    models_dir: Path = field(default_factory=lambda: SERVER_DIR / "models")
    fonts_dir: Path = field(default_factory=lambda: SERVER_DIR / "fonts")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def series_dir(self) -> Path:
        return self.data_dir / "series"

    @property
    def effective_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")

    @property
    def effective_openai_base_url(self) -> str:
        if self.openai_base_url:
            return self.openai_base_url.rstrip("/")
        if self.llm_provider == "openai":
            return "https://api.openai.com/v1"
        return ""

    def llm_status(self) -> tuple[bool, str]:
        """Return (configured, reason-if-not)."""
        p = self.llm_provider
        if p == "none":
            return False, "LLM_PROVIDER is set to none"
        if p == "anthropic":
            if not self.anthropic_api_key:
                return False, "ANTHROPIC_API_KEY is not set"
        elif p == "openai":
            if not self.openai_api_key:
                return False, "OPENAI_API_KEY is not set"
        elif p == "compatible":
            if not self.openai_base_url:
                return False, "OPENAI_BASE_URL is not set"
            if not self.effective_model:
                return False, "LLM_MODEL is not set"
        else:
            return False, f"unknown LLM_PROVIDER '{p}'"
        return True, ""


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    if load_dotenv is not None and env_file is not None and env_file.exists():
        load_dotenv(env_file, override=False)
    env = os.environ

    raw_provider = env.get("LLM_PROVIDER", "anthropic").strip().lower()
    provider = PROVIDER_ALIASES.get(raw_provider, raw_provider)
    if provider not in ("anthropic", "openai", "compatible", "none"):
        log.warning("Unknown LLM_PROVIDER=%r; context mode will fall back to fast mode", raw_provider)

    inpainter = env.get("INPAINTER", "lama").strip().lower() or "lama"
    if inpainter not in ("lama", "opencv"):
        log.warning("Unknown INPAINTER=%r; using lama", inpainter)
        inpainter = "lama"

    s = Settings(
        host=env.get("HOST", "127.0.0.1"),
        port=_int(env.get("PORT"), 5003),
        llm_provider=provider,
        llm_model=env.get("LLM_MODEL", "").strip(),
        llm_timeout=_int(env.get("LLM_TIMEOUT"), 120),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_base_url=(env.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com").rstrip("/"),
        openai_api_key=env.get("OPENAI_API_KEY", "").strip(),
        openai_base_url=env.get("OPENAI_BASE_URL", "").strip(),
        deepl_api_key=env.get("DEEPL_API_KEY", "").strip(),
        google_api_key=env.get("GOOGLE_API_KEY", "").strip(),
        use_gpu=(env.get("USE_GPU", "auto").strip().lower() or "auto"),
        inpainter=inpainter,
        use_manga_ocr=_bool(env.get("USE_MANGA_OCR"), True),
        cache_max_mb=_int(env.get("CACHE_MAX_MB"), 1024),
    )
    if env.get("LAMA_URL"):
        s.lama_url = env["LAMA_URL"].strip()
    if env.get("FUKIDASHI_DATA_DIR"):
        s.data_dir = Path(env["FUKIDASHI_DATA_DIR"])
    return s
