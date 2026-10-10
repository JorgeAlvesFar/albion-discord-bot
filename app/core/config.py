import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    data_store_mode: str = os.getenv("DATA_STORE_MODE", "memory")
    albion_api_base_url: str = os.getenv("ALBION_API_BASE_URL", "https://gameinfo.albiononline.com/api/gameinfo").rstrip("/")
    albion_http_timeout: float = float(os.getenv("ALBION_HTTP_TIMEOUT", "15"))
    albion_max_concurrency: int = max(1, int(os.getenv("ALBION_MAX_CONCURRENCY", "4")))
    albion_retry_attempts: int = max(1, int(os.getenv("ALBION_RETRY_ATTEMPTS", "3")))
    combat_poll_seconds: int = min(60, max(30, int(os.getenv("COMBAT_POLL_SECONDS", "45"))))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

settings = Settings()
if settings.data_store_mode != "memory":
    raise RuntimeError("Esta versão implementa somente DATA_STORE_MODE=memory.")
