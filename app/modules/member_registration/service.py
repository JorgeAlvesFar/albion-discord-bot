import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
import discord
from app.integrations.albion.client import AlbionAPIError
from app.modules.members_config.service import format_nickname, validate_role
from app.repositories.memory import LinkConflict
from app.repositories.models import MemberLink, ServerConfig

log = logging.getLogger(__name__)

MAX_OPTIONS = 25

@dataclass(frozen=True)
class PlayerOption:
    player_id: str
    name: str
    guild_id: str | None
    guild_name: str | None

def player_options(search_result: dict, term: str) -> tuple[PlayerOption | None, list[PlayerOption]]:
    """(personagem com nome exatamente igual, se houver um só; todos os resultados, sem duplicatas)."""
    options, seen = [], set()
    for player in search_result.get("players") or []:
        if not isinstance(player, dict) or not player.get("Id") or not player.get("Name"):
            continue
        player_id = str(player["Id"])
        if player_id in seen:
            continue
        seen.add(player_id)
        options.append(PlayerOption(player_id, str(player["Name"]), player.get("GuildId") or None, player.get("GuildName") or None))
    exact = [option for option in options if option.name.casefold() == term.strip().casefold()]
    return (exact[0] if len(exact) == 1 else None), options

class RegistrationError(Exception):
    """Motivo, já em texto para o usuário, pelo qual o registro não pode continuar."""

async def confirm_in_guild(albion, cfg: ServerConfig, option: PlayerOption) -> PlayerOption:
    """Confirma pela API (perfil atual do personagem) que ele está na guilda cadastrada."""
    try:
        player = await albion.get_player(option.player_id)
    except AlbionAPIError as exc:
        raise RegistrationError("A API do Albion não respondeu. Nada foi alterado; tente novamente em instantes.") from exc
    current = PlayerOption(str(player["Id"]), str(player.get("Name") or option.name), player.get("GuildId") or None, player.get("GuildName") or None)
    if current.guild_id != cfg.guild.guild_id:
        where = f"na guilda **{current.guild_name}**" if current.guild_id else "sem guilda"
        raise RegistrationError(
            f"O personagem **{current.name}** está atualmente {where}, e não na guilda cadastrada neste servidor "
            f"(**{cfg.guild.name}**). Se você entrou na guilda há pouco, a API pode levar um tempo para atualizar."
        )
    return current

def active_link(link: MemberLink | None, cfg: ServerConfig) -> MemberLink | None:
    return link if link and cfg.guild and link.albion_guild_id == cfg.guild.guild_id else None

async def save_registration(repos, cfg: ServerConfig, user_id: int, player: PlayerOption) -> MemberLink:
    link = MemberLink(cfg.discord_guild_id, user_id, player.player_id, player.name, cfg.guild.guild_id)
    result = await repos.save_link(link)
    if isinstance(result, LinkConflict):
        raise RegistrationError(f"O personagem **{player.name}** já está vinculado a outro membro deste servidor (<@{result.other_user_id}>).")
    log.info(
        "DiscordGuildId=%s: usuário %s registrado como %s (%s); vínculo anterior: %s.",
        cfg.discord_guild_id, user_id, player.name, player.player_id, result.player_name if result else None,
    )
    return link

@dataclass
class ApplyResult:
    done: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

async def apply_member_settings(member, cfg: ServerConfig, player_name: str) -> ApplyResult:
    """Aplica cargo e tag configurados. Cada falha é independente e nunca invalida o vínculo."""
    result = ApplyResult()
    guild = member.guild
    me = guild.me
    if cfg.members.role_id:
        role = guild.get_role(cfg.members.role_id)
        error = "o cargo configurado não existe mais" if role is None else validate_role(role, me)
        if error:
            result.problems.append(f"Cargo não aplicado: {error}")
        elif role in member.roles:
            result.done.append(f"Você já tinha o cargo {role.mention}.")
        else:
            try:
                await member.add_roles(role, reason="Registro do personagem do Albion")
                result.done.append(f"Cargo {role.mention} aplicado.")
            except discord.HTTPException as exc:
                result.problems.append(f"Cargo não aplicado: o Discord recusou ({exc.status}).")
    if cfg.members.tag:
        nickname = format_nickname(cfg.members.tag, player_name)
        if member.nick == nickname:
            result.done.append(f"Seu apelido já era `{nickname}`.")
        elif guild.owner_id == member.id:
            result.problems.append("Apelido não alterado: o Discord não permite que bots alterem o apelido do dono do servidor.")
        elif member.top_role.position >= me.top_role.position:
            result.problems.append("Apelido não alterado: seu cargo mais alto está acima ou na mesma posição do cargo do bot.")
        elif not me.guild_permissions.manage_nicknames:
            result.problems.append("Apelido não alterado: o bot não tem a permissão Gerenciar apelidos.")
        else:
            try:
                await member.edit(nick=nickname, reason="Registro do personagem do Albion")
                result.done.append(f"Apelido alterado para `{nickname}`.")
            except discord.HTTPException as exc:
                result.problems.append(f"Apelido não alterado: o Discord recusou ({exc.status}).")
    return result

@dataclass
class SyncStats:
    checked: int = 0
    left: int = 0
    returned: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    unknown: int = 0
    skipped_servers: int = 0

class MemberSync:
    """Verificação periódica: quem saiu da guilda perde o cargo configurado; quem voltou o recupera.

    O vínculo e o apelido são mantidos. Sem resposta válida da API, nada muda.
    """

    def __init__(self, bot, albion, repos, interval_seconds: int = 900):
        self.bot, self.albion, self.repos = bot, albion, repos
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="member-sync")

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(self.interval_seconds)
            try:
                stats = await self.run_once()
                log.info(
                    "Sincronização de membros: %s vínculo(s) verificado(s), %s saíram, %s voltaram, cargos +%s/-%s, "
                    "%s sem confirmação da API, %s servidor(es) sem resposta da API.",
                    stats.checked, stats.left, stats.returned, stats.roles_added, stats.roles_removed, stats.unknown, stats.skipped_servers,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto na sincronização de membros.")

    async def run_once(self) -> SyncStats:
        stats = SyncStats()
        for cfg in await self.repos.all_configs():
            if not cfg.guild:
                continue
            links = [link for link in await self.repos.links_for(cfg.discord_guild_id) if active_link(link, cfg)]
            if not links:
                continue
            try:
                roster = await self.albion.get_guild_members(cfg.guild.guild_id)
            except AlbionAPIError:
                stats.skipped_servers += 1
                log.warning("DiscordGuildId=%s: lista de membros de %s indisponível; nenhum cargo alterado.", cfg.discord_guild_id, cfg.guild.name)
                continue
            member_ids = {str(m.get("Id")) for m in roster if isinstance(m, dict) and m.get("Id")}
            if not member_ids:
                # Uma guilda com vínculos ativos não fica vazia de repente: trata como resposta suspeita.
                stats.skipped_servers += 1
                log.warning("DiscordGuildId=%s: a API devolveu a guilda %s sem membros; nenhum cargo alterado.", cfg.discord_guild_id, cfg.guild.name)
                continue
            for link in links:
                stats.checked += 1
                present = await self._is_present(link, cfg, member_ids)
                if present is None:
                    stats.unknown += 1
                    continue
                if present != link.in_guild:
                    if present:
                        stats.returned += 1
                    else:
                        stats.left += 1
                    log.info(
                        "DiscordGuildId=%s: %s (%s) %s a guilda %s.",
                        cfg.discord_guild_id, link.player_name, link.player_id, "voltou para" if present else "saiu de", cfg.guild.name,
                    )
                link.in_guild, link.checked_at = present, datetime.now(timezone.utc)
                await self._apply_role(cfg, link, present, stats)
        return stats

    async def _is_present(self, link: MemberLink, cfg: ServerConfig, member_ids: set[str]) -> bool | None:
        if link.player_id in member_ids:
            return True
        # Ausente da lista: confirma pelo perfil do personagem antes de concluir que saiu.
        try:
            player = await self.albion.get_player(link.player_id)
        except AlbionAPIError:
            return None
        return (player.get("GuildId") or None) == cfg.guild.guild_id

    async def _apply_role(self, cfg: ServerConfig, link: MemberLink, present: bool, stats: SyncStats) -> None:
        if not cfg.members.role_id:
            return
        guild = self.bot.get_guild(cfg.discord_guild_id)
        if guild is None:
            return
        member = guild.get_member(link.user_id)
        role = guild.get_role(cfg.members.role_id)
        if member is None or role is None or validate_role(role, guild.me):
            return
        try:
            if present and role not in member.roles:
                await member.add_roles(role, reason="Personagem do Albion voltou para a guilda")
                stats.roles_added += 1
            elif not present and role in member.roles:
                await member.remove_roles(role, reason="Personagem do Albion saiu da guilda")
                stats.roles_removed += 1
        except discord.HTTPException:
            log.exception("DiscordGuildId=%s: falha ao ajustar o cargo de %s.", cfg.discord_guild_id, link.user_id)
