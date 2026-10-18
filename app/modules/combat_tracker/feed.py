"""Leitura do feed global de eventos com sobreposição entre leituras e deduplicação por EventId."""
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from app.integrations.albion.client import AlbionAPIError
from app.modules.combat_tracker.events import event_id_of

log = logging.getLogger(__name__)

# Os endpoints filtrados (events?guildId=, players/{id}/deaths) chegam a ficar horas atrasados;
# o feed global de eventos fica a poucos minutos do jogo, por isso é a fonte dos eventos.
FEED_PAGE_SIZE = 51
FEED_MAX_OFFSET = 1000  # a API recusa offset > 1000 (HTTP 400): ~1.020 eventos alcançáveis por leitura
SEEN_EVENTS_LIMIT = 20000

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class ScanResult:
    events: list[dict] = field(default_factory=list)
    observed_at: datetime = field(default_factory=_utcnow)
    pages: int = 0
    received: int = 0
    duplicates: int = 0
    invalid: int = 0
    api_errors: int = 0
    reached_overlap: bool = False
    hit_offset_limit: bool = False
    first_scan: bool = False
    duration: float = 0.0

class FeedReader:
    """Lê o feed do topo até alcançar a leitura anterior e devolve só os eventos novos."""

    def __init__(self, albion):
        self.albion = albion
        self.seen_ids: set[int] = set()
        self._seen_order: deque[int] = deque()
        self._ready = False
        # EventIds abaixo do piso são histórico anterior à primeira leitura (ou já esquecido) e nunca são novos.
        self._floor = 0
        # Eventos lidos numa leitura interrompida: não servem de ponto de parada, porque abaixo deles pode haver eventos ainda não lidos.
        self._unconfirmed_ids: set[int] = set()

    def _remember(self, event_id: int) -> None:
        if event_id in self.seen_ids:
            return
        self.seen_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > SEEN_EVENTS_LIMIT:
            forgotten = self._seen_order.popleft()
            self.seen_ids.discard(forgotten)
            self._floor = max(self._floor, forgotten)

    async def scan(self) -> ScanResult:
        result = ScanResult(first_scan=not self._ready)
        clock = time.monotonic()
        new_events: dict[int, dict] = {}
        offset = 0
        failed = False
        while offset <= FEED_MAX_OFFSET:
            try:
                page = await self.albion.get_recent_events(limit=FEED_PAGE_SIZE, offset=offset)
            except AlbionAPIError:
                log.warning("Falha ao ler o feed global (offset=%s); seguindo com %s evento(s) novo(s) lidos até aqui.", offset, len(new_events))
                result.api_errors += 1
                failed = True
                break
            result.pages += 1
            result.received += len(page)
            overlaps = False
            for item in page:
                event_id = event_id_of(item)
                if event_id is None:
                    result.invalid += 1
                    continue
                if event_id < self._floor or (event_id in self.seen_ids and event_id not in self._unconfirmed_ids):
                    overlaps = True
                    result.duplicates += 1
                    continue
                if event_id in self.seen_ids or event_id in new_events:
                    result.duplicates += 1
                    continue
                new_events[event_id] = item
            if result.first_scan or overlaps or len(page) < FEED_PAGE_SIZE:
                result.reached_overlap = True
                break
            offset += FEED_PAGE_SIZE
        if not result.reached_overlap and not failed:
            result.hit_offset_limit = True
            log.warning(
                "Feed global: limite de paginação atingido (%s eventos) sem alcançar a leitura anterior; eventos mais antigos podem ter sido perdidos.",
                FEED_MAX_OFFSET + FEED_PAGE_SIZE,
            )
        for event_id in new_events:
            self._remember(event_id)
        if failed:
            self._unconfirmed_ids.update(new_events)
        else:
            self._unconfirmed_ids.clear()
        result.duration = time.monotonic() - clock
        result.observed_at = _utcnow()
        if result.first_scan:
            if result.pages:
                self._ready = True
                self._floor = min(new_events, default=0)
                log.info("Feed global: leitura inicial marcou %s evento(s) como vistos; a partir de agora só entram eventos novos.", len(new_events))
            return result
        result.events = list(new_events.values())
        return result
