import logging
import statistics
import time
from typing import Any
import httpx

log = logging.getLogger(__name__)

class AlbionPriceClient:
    """Estima valores em prata com os preços de mercado do Albion Online Data Project."""

    def __init__(self, base_url: str, timeout: float = 10, cache_seconds: int = 600, chunk_size: int = 40):
        self.base_url = base_url.rstrip("/")
        self.cache_seconds = cache_seconds
        self.chunk_size = chunk_size
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "AlbionDiscordBot/0.1"})
        self._cache: dict[str, tuple[float, dict[int | None, float]]] = {}

    async def close(self):
        await self._client.aclose()

    async def _refresh(self, item_types: list[str]) -> None:
        now = time.monotonic()
        stale = [t for t in item_types if t not in self._cache or now - self._cache[t][0] > self.cache_seconds]
        if len(self._cache) > 5000:
            self._cache.clear()
        for start in range(0, len(stale), self.chunk_size):
            chunk = stale[start:start + self.chunk_size]
            response = await self._client.get(f"{self.base_url}/api/v2/stats/prices/{','.join(chunk)}.json")
            response.raise_for_status()
            grouped: dict[str, dict[int | None, list[int]]] = {}
            for row in response.json():
                price = row.get("sell_price_min") or 0
                if price <= 0:
                    continue
                by_quality = grouped.setdefault(row.get("item_id"), {})
                by_quality.setdefault(row.get("quality"), []).append(price)
                by_quality.setdefault(None, []).append(price)
            for item_type in chunk:
                prices = grouped.get(item_type, {})
                self._cache[item_type] = (now, {quality: statistics.median(values) for quality, values in prices.items()})

    async def estimate_value(self, items: list[dict[str, Any]]) -> float | None:
        """Soma o preço mínimo de venda (mediana entre cidades) de cada item; None se a consulta falhar."""
        item_types = sorted({item["Type"] for item in items})
        if not item_types:
            return 0.0
        try:
            await self._refresh(item_types)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Falha ao consultar preços de mercado: %s", type(exc).__name__)
            return None
        total = 0.0
        missing = set()
        for item in items:
            prices = self._cache.get(item["Type"], (0, {}))[1]
            unit = prices.get(item.get("Quality") or 1) or prices.get(None)
            if unit is None:
                missing.add(item["Type"])
                continue
            total += unit * int(item.get("Count") or 1)
        if missing:
            log.info("Sem preço de mercado para %s item(ns): %s", len(missing), ", ".join(sorted(missing)[:10]))
        return total
