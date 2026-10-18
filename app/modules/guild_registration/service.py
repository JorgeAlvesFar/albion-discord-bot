import logging
from dataclasses import dataclass
from app.repositories.models import GuildLink

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
    """Remove a guilda do servidor e, junto, desativa e apaga o killboard (que depende dela)."""
    cfg = await repos.get_config(discord_guild_id)
    if not cfg or cfg.guild is None:
        return None
    previous, was_active = cfg.guild, cfg.killboard_active
    cfg.guild = None
    cfg.killboard = cfg.killboard.reset()
    await repos.save_config(cfg)
    discarded = await tracker.invalidate(discord_guild_id, "guilda redefinida")
    log.info("DiscordGuildId=%s: guilda %s (%s) redefinida; killboard desativado e redefinido.", discord_guild_id, previous.name, previous.guild_id)
    return LinkResult(True, previous, was_active, discarded)
