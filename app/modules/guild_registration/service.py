import logging
from dataclasses import dataclass
import discord
from app.repositories.models import AuditConfig, GuildLink, ServerConfig

log = logging.getLogger(__name__)

MAX_OPTIONS = 25  # limite de opções de um menu de seleção do Discord

def guild_options(search_result: dict) -> list[GuildLink]:
    """Guildas da pesquisa da API, sem duplicatas e sem entradas incompletas, na ordem da API."""
    options, seen = [], set()
    for guild in search_result.get("guilds") or []:
        if not isinstance(guild, dict) or not guild.get("Id") or not guild.get("Name"):
            continue
        guild_id = str(guild["Id"])
        if guild_id in seen:
            continue
        seen.add(guild_id)
        options.append(GuildLink(guild_id, str(guild["Name"]), guild.get("AllianceName") or None))
    return options

@dataclass(frozen=True)
class LinkResult:
    changed: bool
    previous: GuildLink | None
    killboard_was_active: bool
    pending_discarded: int

async def link_guild(repos, tracker, discord_guild_id: int, guild: GuildLink) -> LinkResult:
    """Associa a guilda ao servidor. Trocar de guilda desativa e apaga o killboard; não ativa nada."""
    cfg = await repos.get_or_create_config(discord_guild_id)
    previous = cfg.guild
    if previous is not None and previous.guild_id == guild.guild_id:
        # Mesma guilda: só atualiza nome/aliança exibidos; killboard e demais módulos ficam como estão.
        cfg.guild = guild
        await repos.save_config(cfg)
        return LinkResult(False, previous, cfg.killboard_active, 0)
    was_active = cfg.killboard_active
    cfg.guild = guild
    if previous is not None:
        cfg.killboard = cfg.killboard.reset()
    await repos.save_config(cfg)
    discarded = await tracker.invalidate(discord_guild_id, "guilda alterada") if previous is not None else 0
    log.info(
        "DiscordGuildId=%s: guilda %s (%s) cadastrada; anterior=%s; killboard %s.",
        discord_guild_id, guild.name, guild.guild_id, previous, "desativado e redefinido" if previous else "inalterado (nunca configurado)",
    )
    return LinkResult(True, previous, was_active, discarded)

async def reset_guild(repos, tracker, discord_guild_id: int) -> LinkResult | None:
    """Remove a guilda do servidor e, junto, desativa o killboard e apaga o canal/horário da verificação de membros."""
    cfg = await repos.get_config(discord_guild_id)
    if not cfg or cfg.guild is None:
        return None
    previous, was_active = cfg.guild, cfg.killboard_active
    cfg.guild = None
    cfg.audit = AuditConfig()
    cfg.killboard = cfg.killboard.reset()
    await repos.save_config(cfg)
    discarded = await tracker.invalidate(discord_guild_id, "guilda redefinida")
    log.info("DiscordGuildId=%s: guilda %s (%s) redefinida; killboard desativado e redefinido.", discord_guild_id, previous.name, previous.guild_id)
    return LinkResult(True, previous, was_active, discarded)

def validate_audit(audit: AuditConfig, guild) -> list[str]:
    """Problemas que impedem salvar o canal da verificação. Sem canal, as remoções continuam sem publicação."""
    errors = []
    if not (0 <= audit.report_hour <= 23 and 0 <= audit.report_minute <= 59):
        errors.append("O horário do relatório diário é inválido.")
    if audit.channel_id is None:
        return errors
    channel = guild.get_channel(audit.channel_id) if guild else None
    if not isinstance(channel, discord.TextChannel):
        errors.append("O canal da verificação não existe mais ou não é um canal de texto.")
        return errors
    me = getattr(guild, "me", None)
    if me is None:
        return errors
    perms = channel.permissions_for(me)
    missing = [name for name, ok in (
        ("ver o canal", perms.view_channel), ("enviar mensagens", perms.send_messages), ("incorporar links", perms.embed_links),
    ) if not ok]
    if missing:
        errors.append(f"O bot não pode publicar no canal {channel.mention}: falta permissão para {', '.join(missing)}.")
    return errors

async def save_audit(repos, discord_guild_id: int, audit: AuditConfig) -> tuple[bool, ServerConfig]:
    """Grava o canal e o horário da verificação de membros. Independente da guilda e do killboard."""
    cfg = await repos.get_or_create_config(discord_guild_id)
    if cfg.audit == audit:
        return False, cfg
    cfg.audit = audit
    await repos.save_config(cfg)
    log.info(
        "DiscordGuildId=%s: verificação de membros: canal=%s horário=%s (Brasília).",
        discord_guild_id, audit.channel_id, audit.report_time_text,
    )
    return True, cfg
