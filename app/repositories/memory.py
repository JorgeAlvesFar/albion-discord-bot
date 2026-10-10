import asyncio
from app.repositories.models import ServerConfig, CombatEventRecord

class MemoryRepositories:
    def __init__(self):
        self._configs: dict[int, ServerConfig] = {}
        self._events: dict[tuple[int, int], CombatEventRecord] = {}
        self._lock = asyncio.Lock()

    async def get_config(self, discord_guild_id: int) -> ServerConfig | None:
        async with self._lock:
            return self._configs.get(discord_guild_id)

    async def save_config(self, config: ServerConfig) -> None:
        async with self._lock:
            self._configs[config.discord_guild_id] = config

    async def all_configs(self) -> list[ServerConfig]:
        async with self._lock:
            return list(self._configs.values())

    async def add_if_new(self, record: CombatEventRecord) -> bool:
        key = (record.discord_guild_id, record.event_id)
        async with self._lock:
            if key in self._events:
                return False
            self._events[key] = record
            return True

    async def update_event(self, discord_guild_id: int, event_id: int, status: str, message_id: int | None = None, kind: str | None = None) -> None:
        async with self._lock:
            record = self._events.get((discord_guild_id, event_id))
            if not record:
                return
            record.status = status
            if message_id is not None and kind is not None:
                record.message_ids[kind] = message_id

    async def get_event(self, discord_guild_id: int, event_id: int) -> CombatEventRecord | None:
        async with self._lock:
            return self._events.get((discord_guild_id, event_id))
