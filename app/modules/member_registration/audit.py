"""Publicações da verificação de membros: aviso de cada remoção e relatório diário do que foi feito."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import discord
from app.repositories.models import MemberLink, ServerConfig

log = logging.getLogger(__name__)

# Horário do relatório no fuso de Brasília (sem horário de verão desde 2019).
BRASILIA = timezone(timedelta(hours=-3), "BRT")
TICK_SECONDS = 60
RETRY_AFTER_FAILURE = timedelta(minutes=15)
MAX_LISTED_REMOVALS = 20

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass(frozen=True)
class Removal:
    user_id: int
    player_name: str
    player_id: str
    reason: str
    role_result: str
    at: datetime

@dataclass
class Activity:
    """O que a verificação fez num servidor desde o último relatório."""
    since: datetime
    cycles: int = 0
    checked: int = 0
    unknown: int = 0
    roster_unavailable: int = 0
    role_errors: int = 0
    removals: list[Removal] = field(default_factory=list)

def next_report_at(cfg: ServerConfig, now: datetime) -> datetime:
    """Próximo horário de relatório (em UTC) estritamente depois de `now`."""
    local = now.astimezone(BRASILIA)
    scheduled = local.replace(hour=cfg.audit.report_hour, minute=cfg.audit.report_minute, second=0, microsecond=0)
    if scheduled <= local:
        scheduled += timedelta(days=1)
    return scheduled.astimezone(timezone.utc)

def removal_embed(cfg: ServerConfig, removal: Removal) -> discord.Embed:
    embed = discord.Embed(title="Membro removido do registro", color=discord.Color.orange(), timestamp=removal.at)
    embed.add_field(name="Membro", value=f"<@{removal.user_id}>", inline=True)
    embed.add_field(name="Personagem", value=removal.player_name, inline=True)
    embed.add_field(name="Guilda cadastrada", value=cfg.guild.name if cfg.guild else "—", inline=True)
    embed.add_field(name="Motivo", value=removal.reason, inline=False)
    embed.add_field(name="Cargo", value=removal.role_result, inline=False)
    embed.set_footer(text="O registro foi desativado. Para voltar, o membro precisa usar /registrar de novo.")
    return embed

def report_embed(cfg: ServerConfig, activity: Activity, now: datetime) -> discord.Embed:
    removed = len(activity.removals)
    embed = discord.Embed(
        title="Relatório diário da verificação de membros",
        color=discord.Color.orange() if removed else discord.Color.green(),
        description=f"Período: <t:{int(activity.since.timestamp())}:f> até <t:{int(now.timestamp())}:f>",
    )
    embed.add_field(name="Guilda", value=cfg.guild.name if cfg.guild else "—", inline=True)
    embed.add_field(name="Verificações executadas", value=str(activity.cycles), inline=True)
    embed.add_field(name="Registros verificados", value=str(activity.checked), inline=True)
    if activity.removals:
        lines = [f"<@{r.user_id}> · {r.player_name} · {r.reason}" for r in activity.removals[:MAX_LISTED_REMOVALS]]
        if removed > MAX_LISTED_REMOVALS:
            lines.append(f"… e mais {removed - MAX_LISTED_REMOVALS}")
        embed.add_field(name=f"Removidos ({removed})", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Removidos", value="Ninguém no período.", inline=False)
    problems = []
    if activity.unknown:
        problems.append(f"{activity.unknown} verificação(ões) sem resposta da API (cargo mantido, nova tentativa no próximo ciclo)")
    if activity.role_errors:
        problems.append(f"{activity.role_errors} falha(s) ao remover cargo (veja permissões e hierarquia do bot)")
    if activity.roster_unavailable:
        problems.append(f"{activity.roster_unavailable} vez(es) a lista de membros da guilda estava indisponível (verificado pelo perfil)")
    embed.add_field(name="Pendências", value="\n".join(problems) or "Nenhuma.", inline=False)
    embed.set_footer(text="Próximo relatório em 24 horas. O histórico fica em memória e é perdido se o bot reiniciar.")
    return embed

class MembershipAudit:
    def __init__(self, bot, repos):
        self.bot, self.repos = bot, repos
        self.activity: dict[int, Activity] = {}
        self._schedule: dict[int, tuple[tuple[int, int, int | None], datetime]] = {}
        self._task: asyncio.Task | None = None

    def _activity(self, discord_guild_id: int, now: datetime | None = None) -> Activity:
        return self.activity.setdefault(discord_guild_id, Activity(now or _utcnow()))

    def record_cycle(self, discord_guild_id: int, checked: int, unknown: int, roster_unavailable: int, role_errors: int) -> None:
        activity = self._activity(discord_guild_id)
        activity.cycles += 1
        activity.checked += checked
        activity.unknown += unknown
        activity.roster_unavailable += roster_unavailable
        activity.role_errors += role_errors

    async def record_removal(self, cfg: ServerConfig, link: MemberLink, reason: str, role_result: str) -> None:
        removal = Removal(link.user_id, link.player_name, link.player_id, reason, role_result, _utcnow())
        self._activity(cfg.discord_guild_id).removals.append(removal)
        if cfg.audit.channel_id:
            await self._send(cfg, removal_embed(cfg, removal), "aviso de remoção")

    async def _send(self, cfg: ServerConfig, embed: discord.Embed, what: str) -> bool:
        channel = self.bot.get_channel(cfg.audit.channel_id)
        if channel is None:
            log.warning("DiscordGuildId=%s: canal de verificação %s não encontrado; %s não publicado.", cfg.discord_guild_id, cfg.audit.channel_id, what)
            return False
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            log.exception("DiscordGuildId=%s: falha ao publicar %s no canal %s.", cfg.discord_guild_id, what, cfg.audit.channel_id)
            return False

    async def tick(self, now: datetime | None = None) -> int:
        """Publica os relatórios que venceram. Devolve quantos foram publicados."""
        now = now or _utcnow()
        published = 0
        for cfg in await self.repos.all_configs():
            gid = cfg.discord_guild_id
            if not cfg.guild or not cfg.audit.channel_id:
                self._schedule.pop(gid, None)
                continue
            key = (cfg.audit.report_hour, cfg.audit.report_minute, cfg.audit.channel_id)
            scheduled = self._schedule.get(gid)
            if scheduled is None or scheduled[0] != key:
                # Configuração nova ou alterada: agenda o próximo horário; o período começa agora se ainda não havia histórico.
                self._schedule[gid] = (key, next_report_at(cfg, now))
                self._activity(gid, now)
                continue
            if now < scheduled[1]:
                continue
            activity = self._activity(gid, now)
            if await self._send(cfg, report_embed(cfg, activity, now), "relatório diário"):
                published += 1
                self.activity[gid] = Activity(now)
                self._schedule[gid] = (key, next_report_at(cfg, now))
                log.info("DiscordGuildId=%s: relatório diário publicado (%s remoção(ões)).", gid, len(activity.removals))
            else:
                self._schedule[gid] = (key, now + RETRY_AFTER_FAILURE)
        return published

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="membership-audit")

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
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto no relatório diário da verificação de membros.")
            await asyncio.sleep(TICK_SECONDS)
