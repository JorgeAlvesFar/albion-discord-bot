import asyncio
import logging
import httpx
from typing import Any

log = logging.getLogger(__name__)

class AlbionAPIError(RuntimeError):
    pass

class AlbionClient:
    """Cliente de produção: sempre consulta a API real do Albion."""
    def __init__(self, base_url: str, timeout: float = 15, max_concurrency: int = 4, retry_attempts: int = 3):
        self.base_url = base_url.rstrip("/")
        self.retry_attempts = retry_attempts
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "AlbionDiscordBot/0.1"})

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error = None
        async with self._semaphore:
            for attempt in range(self.retry_attempts):
                try:
                    response = await self._client.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise AlbionAPIError("A API retornou JSON inválido.") from exc
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                        break
                    if attempt + 1 < self.retry_attempts:
                        retry_after = 0
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                            try: retry_after = float(exc.response.headers.get("Retry-After", "0"))
                            except ValueError: retry_after = 0
                        await asyncio.sleep(max(retry_after, min(2 ** attempt, 8)))
        log.warning("Falha na API Albion em %s: %s", path, type(last_error).__name__ if last_error else "resposta inválida")
        raise AlbionAPIError(f"Falha ao consultar API do Albion ({path}).") from last_error

    async def search(self, name: str) -> dict[str, Any]:
        params = {"q": name}
        uri = self._client.build_request("GET", f"{self.base_url}/search", params=params).url
        log.info("Pesquisa Albion: termo=%r uri=%s", name, uri)
        result = await self._get("search", params)
        if not isinstance(result, dict):
            raise AlbionAPIError("Formato inesperado na pesquisa.")
        return result

    async def get_recent_events(self, limit: int = 51, offset: int = 0) -> list[dict[str, Any]]:
        result = await self._get("events", {"limit": limit, "offset": offset})
        if not isinstance(result, list):
            raise AlbionAPIError("Formato inesperado no feed de eventos.")
        return result
