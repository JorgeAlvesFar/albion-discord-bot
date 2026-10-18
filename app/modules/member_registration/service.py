import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
import discord
from app.integrations.albion.client import AlbionAPIError, AlbionNotFoundError
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
    except AlbionNotFoundError as exc:
        raise RegistrationError(f"O personagem **{option.name}** não foi encontrado na API do Albion. Confira o nome.") from exc
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
    return link if link and link.active and cfg.guild and link.albion_guild_id == cfg.guild.guild_id else None

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
    roles_removed: int = 0
    unknown: int = 0
    roster_unavailable: int = 0
    role_errors: int = 0

class MemberSync:
    """Verificação periódica: quem não está mais na guilda cadastrada perde o cargo e deixa de ser verificado.

    Vale qualquer motivo de saída (saiu, foi expulso, trocou de guilda, apagou o personagem, ou o servidor trocou a
    guilda cadastrada). O perfil do personagem é a fonte de verdade; a lista de membros da guilda é só um atalho.
    Quem sai tem o vínculo desativado e é ignorado daí em diante, mesmo que volte: só um novo /registrar devolve o
    cargo. O apelido não é alterado. Sem resposta da API (indisponível, timeout), nada muda.
    """

    def __init__(self, bot, albion, repos, interval_seconds: int = 900, audit=None):
        self.bot, self.albion, self.repos = bot, albion, repos
        self.interval_seconds = interval_seconds
        self.audit = audit
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
                    "Sincronização de membros: %s vínculo(s) ativo(s) verificado(s), %s fora da guilda (desativados), %s cargo(s) removido(s), "
                    "%s sem resposta da API (mantidos), %s lista(s) de membros indisponível(is), %s erro(s) de cargo.",
                    stats.checked, stats.left, stats.roles_removed, stats.unknown, stats.roster_unavailable, stats.role_errors,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto na sincronização de membros.")

    async def run_once(self) -> SyncStats:
        total = SyncStats()
        for cfg in await self.repos.all_configs():
            if not cfg.guild:
                continue
            stats = SyncStats()
            links = [link for link in await self.repos.links_for(cfg.discord_guild_id) if link.active]
            if links:
                await self._check_server(cfg, links, stats)
            if self.audit:
                self.audit.record_cycle(cfg.discord_guild_id, stats.checked, stats.unknown, stats.roster_unavailable, stats.role_errors)
            for name in vars(total):
                setattr(total, name, getattr(total, name) + getattr(stats, name))
        return total

    async def _check_server(self, cfg: ServerConfig, links: list[MemberLink], stats: SyncStats) -> None:
        member_ids = await self._roster(cfg, stats)
        for link in links:
            stats.checked += 1
            present, reason = await self._membership(link, cfg, member_ids)
            link.checked_at = datetime.now(timezone.utc)
            if present is None:
                stats.unknown += 1
                log.info("DiscordGuildId=%s: %s (%s) não verificado: %s; cargo mantido.", cfg.discord_guild_id, link.player_name, link.player_id, reason)
                continue
            if present:
                continue
            # Só desativa depois de tirar o cargo: se a remoção falhar, tenta de novo no próximo ciclo.
            removed, role_result = await self._remove_role(cfg, link, stats)
            if not removed:
                continue
            link.deactivate(reason)
            stats.left += 1
            log.info(
                "DiscordGuildId=%s: %s (%s) não está mais na guilda %s (%s); %s; vínculo desativado.",
                cfg.discord_guild_id, link.player_name, link.player_id, cfg.guild.name, reason, role_result,
            )
            if self.audit:
                await self.audit.record_removal(cfg, link, reason, role_result)

    async def _roster(self, cfg: ServerConfig, stats: SyncStats) -> set[str] | None:
        """Ids na lista de membros da guilda, ou None se ela não puder ser usada (aí cada perfil é consultado)."""
        try:
            roster = await self.albion.get_guild_members(cfg.guild.guild_id)
        except AlbionAPIError:
            stats.roster_unavailable += 1
            log.warning("DiscordGuildId=%s: lista de membros de %s indisponível; verificando pelo perfil de cada personagem.", cfg.discord_guild_id, cfg.guild.name)
            return None
        member_ids = {str(m.get("Id")) for m in roster if isinstance(m, dict) and m.get("Id")}
        if not member_ids:
            # Lista vazia é suspeita (a API às vezes devolve vazio): não serve para concluir nada.
            stats.roster_unavailable += 1
            log.warning("DiscordGuildId=%s: a API devolveu a guilda %s sem membros; verificando pelo perfil de cada personagem.", cfg.discord_guild_id, cfg.guild.name)
            return None
        return member_ids

    async def _membership(self, link: MemberLink, cfg: ServerConfig, member_ids: set[str] | None) -> tuple[bool | None, str]:
        """(está na guilda cadastrada?, motivo). None = não deu para confirmar: nada muda."""
        if link.albion_guild_id != cfg.guild.guild_id:
            return False, "vínculo feito com outra guilda; o servidor trocou a guilda cadastrada"
        if member_ids is not None and link.player_id in member_ids:
            return True, "na lista de membros"
        # Fora da lista (ou lista indisponível): o perfil atual do personagem decide.
        try:
            player = await self.albion.get_player(link.player_id)
        except AlbionNotFoundError:
            return False, "personagem não existe mais na API"
        except AlbionAPIError:
            return None, "API indisponível"
        guild_id = player.get("GuildId") or None
        if guild_id == cfg.guild.guild_id:
            return True, "perfil na guilda"
        return False, f"perfil na guilda {player.get('GuildName')}" if guild_id else "perfil sem guilda"

    async def _remove_role(self, cfg: ServerConfig, link: MemberLink, stats: SyncStats) -> tuple[bool, str]:
        """Tira o cargo configurado. (True, resultado) se não há mais nada a remover; (False, motivo) se falhou (tentar de novo)."""
        if not cfg.members.role_id:
            return True, "nenhum cargo configurado em /registrar-membros"
        guild = self.bot.get_guild(cfg.discord_guild_id)
        if guild is None:
            return False, "servidor indisponível para o bot"
        member = guild.get_member(link.user_id)
        if member is None:
            return True, "o membro não está mais no servidor Discord"
        role = guild.get_role(cfg.members.role_id)
        if role is None:
            return True, "o cargo configurado não existe mais"
        if role not in member.roles:
            return True, f"o membro já não tinha o cargo <@&{role.id}>"
        error = validate_role(role, guild.me)
        if error:
            stats.role_errors += 1
            log.warning("DiscordGuildId=%s: não foi possível remover o cargo de %s: %s", cfg.discord_guild_id, link.user_id, error)
            return False, error
        try:
            await member.remove_roles(role, reason="Personagem do Albion não está na guilda cadastrada")
        except discord.HTTPException:
            stats.role_errors += 1
            log.exception("DiscordGuildId=%s: falha ao remover o cargo de %s.", cfg.discord_guild_id, link.user_id)
            return False, "o Discord recusou a remoção"
        stats.roles_removed += 1
        return True, f"cargo <@&{role.id}> removido"
