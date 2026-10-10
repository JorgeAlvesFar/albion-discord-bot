from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

@dataclass
class ServerConfig:
    discord_guild_id: int
    albion_guild_id: str | None = None
    albion_guild_name: str | None = None
    alliance_name: str | None = None
    combat_enabled: bool = False
    kills_enabled: bool = False
    deaths_enabled: bool = False
    kills_channel_id: int | None = None
    deaths_channel_id: int | None = None
    activated_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def combat_config_valid(self) -> bool:
        if not self.albion_guild_id or not self.combat_enabled or not self.activated_at:
            return False
        if self.kills_enabled and not self.kills_channel_id:
            return False
        if self.deaths_enabled and not self.deaths_channel_id:
            return False
        return self.kills_enabled or self.deaths_enabled

@dataclass
class CombatEventRecord:
    event_id: int
    discord_guild_id: int
    payload: dict[str, Any]
    status: str = "PENDING"
    message_ids: dict[str, int] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
