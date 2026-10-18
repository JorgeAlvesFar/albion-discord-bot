"""Regras do /killboard: validação de canais e permissões, salvar e redefinir."""
import logging
from dataclasses import dataclass
import discord
from app.repositories.models import KINDS, KillboardSettings, ServerConfig

log = logging.getLogger(__name__)

LABELS = {"kill": "kills", "death": "mortes"}

def validate_settings(settings: KillboardSettings, guild) -> list[str]:
    """Problemas que impedem salvar. Só os tipos ligados são validados; canais de tipos desligados ficam guardados."""
    errors = []
    for kind in KINDS:
        label = LABELS[kind]
        if not settings.enabled(kind):
            continue
        channel_id = settings.channel(kind)
        if channel_id is None:
            errors.append(f"Escolha o canal de {label} ou desligue as notificações de {label}.")
            continue
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            errors.append(f"O canal de {label} não existe mais ou não é um canal de texto.")
            continue
        perms = channel.permissions_for(guild.me)
        missing = [name for name, ok in (
            ("ver o canal", perms.view_channel), ("enviar mensagens", perms.send_messages),
            ("incorporar links", perms.embed_links), ("anexar arquivos", perms.attach_files),
        ) if not ok]
        if missing:
            errors.append(f"O bot não pode publicar {label} em {channel.mention}: falta permissão para {', '.join(missing)}.")
    return errors

@dataclass(frozen=True)
class SaveResult:
    operation: str | None  # None = nada mudou
    config: ServerConfig | None
    pending_discarded: int = 0

async def save_killboard(repos, tracker, discord_guild_id: int, settings: KillboardSettings) -> SaveResult:
    """Grava a configuração. Toda alteração define nova referência temporal (ou desativa) e descarta as pendências."""
    cfg = await repos.get_config(discord_guild_id)
    if cfg is None or cfg.guild is None:
        raise ValueError("guilda não cadastrada")
    current = cfg.killboard
    if current.settings == settings and current.active == settings.publishes_something:
        return SaveResult(None, cfg)
    was_active = cfg.killboard_active
    cfg.killboard = current.saved(settings)
    await repos.save_config(cfg)
    if cfg.killboard.active:
        operation = "alterado" if was_active else "ativado"
    else:
        operation = "desativado" if was_active else "alterado"
    discarded = await tracker.invalidate(discord_guild_id, f"killboard {operation}")
    log.info(
        "DiscordGuildId=%s: killboard %s: %s versão=%s ativado_em=%s.", discord_guild_id, operation, settings,
        cfg.killboard.version, cfg.killboard.activated_at.isoformat() if cfg.killboard.activated_at else None,
    )
    return SaveResult(operation, cfg, discarded)

async def reset_killboard(repos, tracker, discord_guild_id: int) -> SaveResult:
    cfg = await repos.get_config(discord_guild_id)
    if cfg is None or (cfg.killboard.settings.empty and cfg.killboard.activated_at is None):
        return SaveResult(None, cfg)
    cfg.killboard = cfg.killboard.reset()
    await repos.save_config(cfg)
    discarded = await tracker.invalidate(discord_guild_id, "killboard redefinido")
    log.info("DiscordGuildId=%s: killboard redefinido (versão %s).", discord_guild_id, cfg.killboard.version)
    return SaveResult("redefinido", cfg, discarded)
