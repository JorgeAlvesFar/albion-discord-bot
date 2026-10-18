from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

KINDS = ("kill", "death")

@dataclass(frozen=True)
class GuildLink:
    """/cadastrar: guilda do Albion associada ao servidor, identificada pelo Id (nunca pelo nome)."""
    guild_id: str
    name: str
    alliance_name: str | None = None

@dataclass(frozen=True)
class MembersConfig:
    """/registrar-membros: opções independentes; vazias = não atribuir cargo / não alterar o apelido."""
    role_id: int | None = None
    tag: str | None = None

    @property
    def empty(self) -> bool:
        return self.role_id is None and self.tag is None

@dataclass(frozen=True)
class KillboardSettings:
    """/killboard: o que o administrador escolhe. Canais de tipos desligados ficam guardados para religar."""
    kills_enabled: bool = False
    kills_channel_id: int | None = None
    deaths_enabled: bool = False
    deaths_channel_id: int | None = None

    def enabled(self, kind: str) -> bool:
        return self.kills_enabled if kind == "kill" else self.deaths_enabled

    def channel(self, kind: str) -> int | None:
        return self.kills_channel_id if kind == "kill" else self.deaths_channel_id

    def channel_for(self, kind: str) -> int | None:
        """Canal onde o tipo é publicado, ou None se o tipo estiver desligado ou sem canal."""
        return self.channel(kind) if self.enabled(kind) else None

    def toggled(self, kind: str) -> "KillboardSettings":
        return replace(self, kills_enabled=not self.kills_enabled) if kind == "kill" else replace(self, deaths_enabled=not self.deaths_enabled)

    def with_channel(self, kind: str, channel_id: int | None) -> "KillboardSettings":
        return replace(self, kills_channel_id=channel_id) if kind == "kill" else replace(self, deaths_channel_id=channel_id)

    @property
    def publishes_something(self) -> bool:
        return any(self.channel_for(kind) for kind in KINDS)

    @property
    def empty(self) -> bool:
        return self == KillboardSettings()

@dataclass(frozen=True)
class KillboardConfig:
    settings: KillboardSettings = KillboardSettings()
    # Instante a partir do qual os combates são publicados; None = killboard desativado.
    activated_at: datetime | None = None
    # Incrementada a cada gravação ou redefinição do killboard e a cada troca de guilda:
    # notificações criadas sob uma versão anterior não podem mais ser publicadas.
    version: int = 0

    @property
    def active(self) -> bool:
        return self.activated_at is not None and self.settings.publishes_something

    def saved(self, settings: KillboardSettings) -> "KillboardConfig":
        """Nova configuração gravada: com algo ligado, novo instante de referência; senão, desativada."""
        return KillboardConfig(settings, _utcnow() if settings.publishes_something else None, self.version + 1)

    def reset(self) -> "KillboardConfig":
        return KillboardConfig(version=self.version + 1)

@dataclass
class ServerConfig:
    """Configuração de um servidor Discord: cada módulo tem a sua parte e só altera a própria."""
    discord_guild_id: int
    guild: GuildLink | None = None
    members: MembersConfig = MembersConfig()
    killboard: KillboardConfig = KillboardConfig()
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def killboard_active(self) -> bool:
        """O tracker só trabalha para servidores com guilda cadastrada e killboard ativo."""
        return self.guild is not None and self.killboard.active

    def touch(self) -> None:
        self.updated_at = _utcnow()

@dataclass
class MemberLink:
    """Vínculo entre um usuário Discord e um personagem do Albion, num servidor.

    Só conta como ativo enquanto `albion_guild_id` for a guilda cadastrada no servidor: trocar a guilda não transfere vínculos.
    """
    discord_guild_id: int
    user_id: int
    player_id: str
    player_name: str
    albion_guild_id: str
    linked_at: datetime = field(default_factory=_utcnow)
    # Última situação confirmada pela API: o personagem estava na guilda? Só muda com resposta válida da API.
    in_guild: bool = True
    checked_at: datetime = field(default_factory=_utcnow)

# Estados de uma notificação. PUBLISHED, DISCARDED e FAILED são finais.
PENDING, PUBLISHED, DISCARDED, FAILED = "PENDING", "PUBLISHED", "DISCARDED", "FAILED"
FINAL_STATUSES = frozenset({PUBLISHED, DISCARDED, FAILED})

@dataclass
class Delivery:
    """Uma notificação (kill ou morte) de um evento para um servidor, com estado próprio."""
    kind: str
    channel_id: int
    status: str = PENDING
    attempts: int = 0
    next_attempt_at: datetime = field(default_factory=_utcnow)
    last_error: str | None = None
    message_id: int | None = None
    published_at: datetime | None = None

@dataclass
class CombatEventRecord:
    event_id: int
    discord_guild_id: int
    payload: dict[str, Any]
    combat_at: datetime
    observed_at: datetime
    config_version: int
    deliveries: dict[str, Delivery] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return all(delivery.status in FINAL_STATUSES for delivery in self.deliveries.values())
