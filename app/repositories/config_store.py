import asyncio
from app.repositories.models import ServerConfig

class ConfigStore:
    """Configuração de cada servidor Discord, indexada pelo DiscordGuildId."""

    def __init__(self):
        self._configs: dict[int, ServerConfig] = {}
        self._configs_lock = asyncio.Lock()

    async def get_config(self, discord_guild_id: int) -> ServerConfig | None:
        async with self._configs_lock:
            return self._configs.get(discord_guild_id)

    async def get_or_create_config(self, discord_guild_id: int) -> ServerConfig:
        async with self._configs_lock:
            return self._configs.setdefault(discord_guild_id, ServerConfig(discord_guild_id))

    async def save_config(self, config: ServerConfig) -> None:
        async with self._configs_lock:
            config.touch()
            self._configs[config.discord_guild_id] = config

    async def all_configs(self) -> list[ServerConfig]:
        async with self._configs_lock:
            return list(self._configs.values())
