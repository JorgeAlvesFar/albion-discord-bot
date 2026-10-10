import asyncio
import logging
import re
from datetime import datetime, timezone
import discord
from app.repositories.models import CombatEventRecord

log = logging.getLogger(__name__)

# A API do Albion envia até 7 casas decimais nos segundos; datetime aceita no máximo 6.
_EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")

def parse_timestamp(value):
    if not value:
        return None
    try:
        text = _EXTRA_FRACTION_DIGITS.sub(r"\1", str(value)).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def _guild_id(entity):
    return str(entity.get("GuildId")) if isinstance(entity, dict) and entity.get("GuildId") else None

class CombatTracker:
    def __init__(self, bot, albion, repos, poll_seconds=45):
        self.bot, self.albion, self.repos = bot, albion, repos
        self.poll_seconds = max(30, min(60, poll_seconds))
        self._task = None
        self._locks = {}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="combat-tracker")

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        await self.bot.wait_until_ready()
        log.info("Tracker de combate iniciado (intervalo de %ss).", self.poll_seconds)
        while not self.bot.is_closed():
            try:
                configs = await self.repos.all_configs()
                active = [cfg for cfg in configs if cfg.combat_config_valid]
                log.info("Ciclo do tracker: %s servidor(es) configurado(s), %s com tracker ativo.", len(configs), len(active))
                for cfg in active:
                    lock = self._locks.setdefault(cfg.discord_guild_id, asyncio.Lock())
                    if lock.locked():
                        log.info("DiscordGuildId=%s ainda está em consulta do ciclo anterior; pulando.", cfg.discord_guild_id)
                        continue
                    async with lock:
                        await self._poll_guild(cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto no ciclo do tracker.")
            await asyncio.sleep(self.poll_seconds)

    async def _poll_guild(self, cfg):
        # Endpoint é consultado somente depois de uma configuração válida e ativada.
        activation = cfg.activated_at
        if not activation or not cfg.albion_guild_id:
            return
        log.info(
            "Consultando eventos: DiscordGuildId=%s AlbionGuild=%s (%s) ativado_em=%s",
            cfg.discord_guild_id, cfg.albion_guild_name, cfg.albion_guild_id, activation.isoformat(),
        )
        seen_page_signatures = set()
        candidates = {}
        offset = 0
        page_size = 51
        total_events = 0
        newest_event_time = None
        invalid_timestamps = []
        # Bound the scan to avoid runaway requests. API behavior must be validated in production.
        for _ in range(5):
            page = await self.albion.get_events_page(cfg.albion_guild_id, limit=page_size, offset=offset)
            log.info("Página de eventos offset=%s: %s evento(s).", offset, len(page))
            if not page:
                break
            total_events += len(page)
            signature = tuple(str(x.get("EventId")) for x in page if isinstance(x, dict))
            if not signature or signature in seen_page_signatures:
                log.warning("Paginação repetida/vazia para DiscordGuildId=%s; interrompendo varredura.", cfg.discord_guild_id)
                break
            seen_page_signatures.add(signature)
            for item in page:
                if not isinstance(item, dict) or item.get("EventId") is None:
                    continue
                event_time = parse_timestamp(item.get("TimeStamp"))
                if event_time is None:
                    invalid_timestamps.append(item.get("TimeStamp"))
                    continue
                if newest_event_time is None or event_time > newest_event_time:
                    newest_event_time = event_time
                if event_time >= activation:
                    candidates[int(item["EventId"])] = item
            if len(page) < page_size:
                break
            offset += len(page)
        if invalid_timestamps:
            log.warning("%s evento(s) com TimeStamp inválido descartado(s). Exemplo: %r", len(invalid_timestamps), invalid_timestamps[0])
        log.info(
            "Eventos recebidos: %s; após a ativação: %s; evento mais recente: %s.",
            total_events, len(candidates), newest_event_time.isoformat() if newest_event_time else "nenhum",
        )
        for event_id, event in sorted(candidates.items(), key=lambda pair: parse_timestamp(pair[1].get("TimeStamp")) or datetime.min.replace(tzinfo=timezone.utc)):
            killer = event.get("Killer") or {}
            victim = event.get("Victim") or {}
            is_kill = _guild_id(killer) == cfg.albion_guild_id
            is_death = _guild_id(victim) == cfg.albion_guild_id
            kinds = []
            if is_kill and cfg.kills_enabled and cfg.kills_channel_id:
                kinds.append(("kill", cfg.kills_channel_id))
            if is_death and cfg.deaths_enabled and cfg.deaths_channel_id:
                kinds.append(("death", cfg.deaths_channel_id))
            if not kinds:
                log.info(
                    "Evento %s ignorado: killer_guild=%s victim_guild=%s esperado=%s kill=%s death=%s.",
                    event_id, _guild_id(killer), _guild_id(victim), cfg.albion_guild_id, is_kill, is_death,
                )
                continue
            record = CombatEventRecord(event_id, cfg.discord_guild_id, event)
            await self.repos.add_if_new(record)
            stored = await self.repos.get_event(cfg.discord_guild_id, event_id)
            if not stored or stored.status == "PUBLISHED":
                log.debug("Evento %s já publicado; ignorando.", event_id)
                continue
            all_sent = True
            for kind, channel_id in kinds:
                if kind in stored.message_ids:
                    continue
                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    log.warning("Canal inválido/indisponível (%s, tipo=%s) para evento %s.", channel_id, type(channel).__name__, event_id)
                    all_sent = False
                    continue
                try:
                    message = await channel.send(embed=self._make_embed(event, kind))
                    log.info("Evento %s (%s) publicado em #%s (%s), mensagem %s.", event_id, kind, channel.name, channel_id, message.id)
                    await self.repos.update_event(cfg.discord_guild_id, event_id, "PENDING", message.id, kind)
                    stored = await self.repos.get_event(cfg.discord_guild_id, event_id)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Falha ao publicar evento %s no canal %s.", event_id, channel_id)
                    all_sent = False
            if all_sent:
                await self.repos.update_event(cfg.discord_guild_id, event_id, "PUBLISHED")

    def _make_embed(self, event: dict, kind: str) -> discord.Embed:
        killer = event.get("Killer") or {}
        victim = event.get("Victim") or {}
        title = "⚔️ Kill da guilda" if kind == "kill" else "💀 Morte da guilda"
        embed = discord.Embed(title=title, color=discord.Color.green() if kind == "kill" else discord.Color.red())
        embed.add_field(name="Killer", value=self._entity(killer), inline=True)
        embed.add_field(name="Vítima", value=self._entity(victim), inline=True)
        if event.get("KillFame") is not None:
            embed.add_field(name="Kill Fame", value=f"{event['KillFame']:,}", inline=True)
        if event.get("TotalVictimKillFame") is not None:
            embed.add_field(name="Fama total da vítima", value=f"{event['TotalVictimKillFame']:,}", inline=True)
        if event.get("AverageItemPower") is not None:
            embed.add_field(name="IP médio", value=str(event["AverageItemPower"]), inline=True)
        if event.get("KillArea"):
            embed.add_field(name="Área", value=str(event["KillArea"])[:1024], inline=True)
        if event.get("Location"):
            embed.add_field(name="Localização", value=str(event["Location"])[:1024], inline=True)
        equipment = victim.get("Equipment") or killer.get("Equipment")
        if isinstance(equipment, dict):
            names = []
            for slot, item in equipment.items():
                if isinstance(item, dict) and item.get("Type"):
                    names.append(f"{slot}: {item['Type']}")
            if names:
                embed.add_field(name="Equipamentos", value="\n".join(names)[:1024], inline=False)
        timestamp = parse_timestamp(event.get("TimeStamp"))
        if timestamp:
            embed.timestamp = timestamp
        embed.set_footer(text=f"Event ID: {event.get('EventId', 'desconhecido')}")
        return embed

    @staticmethod
    def _entity(entity):
        if not isinstance(entity, dict):
            return "Não informado"
        name = entity.get("Name") or "Nome não informado"
        guild = entity.get("GuildName") or "Sem guilda informada"
        return f"**{name}**\n{guild}"[:1024]
