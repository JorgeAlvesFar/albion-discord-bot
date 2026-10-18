import asyncio
from collections import deque
from datetime import datetime
from app.repositories.models import DISCARDED, PENDING, CombatEventRecord

# Eventos concluídos saem da memória; só a chave fica guardada, por uma janela limitada, para deduplicação.
COMPLETED_KEYS_LIMIT = 20000

class EventStore:
    """Fila de notificações de combate por servidor, com deduplicação e retenção limitada."""

    def __init__(self, completed_limit: int = COMPLETED_KEYS_LIMIT):
        self._events: dict[tuple[int, int], CombatEventRecord] = {}
        self._completed: set[tuple[int, int]] = set()
        self._completed_order: deque[tuple[int, int]] = deque()
        self._completed_limit = completed_limit
        self._events_lock = asyncio.Lock()

    async def add_if_new(self, record: CombatEventRecord) -> bool:
        key = (record.discord_guild_id, record.event_id)
        async with self._events_lock:
            if key in self._events or key in self._completed:
                return False
            self._events[key] = record
            return True

    async def get_event(self, discord_guild_id: int, event_id: int) -> CombatEventRecord | None:
        async with self._events_lock:
            return self._events.get((discord_guild_id, event_id))

    async def due_events(self, now: datetime) -> list[CombatEventRecord]:
        async with self._events_lock:
            due = [
                record for record in self._events.values()
                if any(d.status == PENDING and d.next_attempt_at <= now for d in record.deliveries.values())
            ]
        return sorted(due, key=lambda record: record.combat_at)

    async def next_due_at(self) -> datetime | None:
        async with self._events_lock:
            times = [d.next_attempt_at for record in self._events.values() for d in record.deliveries.values() if d.status == PENDING]
        return min(times, default=None)

    async def pending_count(self) -> int:
        async with self._events_lock:
            return sum(1 for record in self._events.values() for d in record.deliveries.values() if d.status == PENDING)

    async def finalize(self, record: CombatEventRecord) -> bool:
        """Remove da memória o evento cujas notificações terminaram, guardando só a chave para deduplicação."""
        key = (record.discord_guild_id, record.event_id)
        async with self._events_lock:
            if not record.done or self._events.get(key) is not record:
                return False
            del self._events[key]
            self._remember_completed(key)
            return True

    async def discard_pending(self, discord_guild_id: int, reason: str) -> int:
        """Descarta todas as notificações pendentes de um servidor (killboard desativado ou reconfigurado)."""
        discarded = 0
        async with self._events_lock:
            for key, record in list(self._events.items()):
                if key[0] != discord_guild_id:
                    continue
                for delivery in record.deliveries.values():
                    if delivery.status == PENDING:
                        delivery.status = DISCARDED
                        delivery.last_error = reason
                        discarded += 1
                if record.done:
                    del self._events[key]
                    self._remember_completed(key)
        return discarded

    def _remember_completed(self, key: tuple[int, int]) -> None:
        if key in self._completed:
            return
        self._completed.add(key)
        self._completed_order.append(key)
        if len(self._completed_order) > self._completed_limit:
            self._completed.discard(self._completed_order.popleft())
