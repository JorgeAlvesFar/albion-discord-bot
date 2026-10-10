import asyncio
import io
import logging
import re
from collections import deque
from datetime import datetime, timezone
import discord
from app.integrations.albion.client import AlbionAPIError
from app.modules.combat_tracker.card import victim_items
from app.repositories.models import CombatEventRecord

log = logging.getLogger(__name__)

# A API do Albion envia até 7 casas decimais nos segundos; datetime aceita no máximo 6.
_EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")

# Os endpoints filtrados (events?guildId=, players/{id}/deaths) chegam a ficar horas atrasados;
# o feed global de eventos fica a poucos minutos do jogo, por isso é a fonte dos eventos.
FEED_PAGE_SIZE = 51
FEED_MAX_OFFSET = 1000
SEEN_EVENTS_LIMIT = 20000
CARD_FILENAME = "combate.png"
TITLES = {"kill": "⚔️ Kill da guilda", "death": "💀 Morte da guilda"}

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
    def __init__(self, bot, albion, repos, poll_seconds=45, renderer=None, prices=None):
        self.bot, self.albion, self.repos = bot, albion, repos
        self.renderer, self.prices = renderer, prices
        self.poll_seconds = max(30, min(60, poll_seconds))
        self._task = None
        self._seen_ids: set[int] = set()
        self._seen_order: deque[int] = deque()
        self._feed_ready = False
        # EventIds abaixo do piso são histórico anterior à primeira leitura (ou já esquecido) e nunca são publicados.
        self._feed_floor = 0
        # Eventos lidos numa leitura interrompida: não servem de ponto de parada, porque abaixo deles pode haver eventos ainda não lidos.
        self._unconfirmed_ids: set[int] = set()

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
                # O feed é lido a cada ciclo mesmo sem servidores ativos, para que uma ativação não publique o histórico.
                new_events = await self._scan_feed()
                configs = await self.repos.all_configs()
                active = [cfg for cfg in configs if cfg.combat_config_valid]
                log.info("Ciclo do tracker: %s servidor(es) configurado(s), %s com tracker ativo.", len(configs), len(active))
                for cfg in active:
                    try:
                        await self._poll_guild(cfg, new_events)
                    except Exception:
                        log.exception("Erro ao processar eventos de DiscordGuildId=%s.", cfg.discord_guild_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto no ciclo do tracker.")
            await asyncio.sleep(self.poll_seconds)

    def _remember(self, event_id: int) -> None:
        if event_id in self._seen_ids:
            return
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > SEEN_EVENTS_LIMIT:
            forgotten = self._seen_order.popleft()
            self._seen_ids.discard(forgotten)
            self._feed_floor = max(self._feed_floor, forgotten)

    async def _scan_feed(self) -> list[dict]:
        """Lê o feed global do topo até a página que se sobrepõe à leitura anterior e devolve apenas os eventos novos."""
        first_scan = not self._feed_ready
        new_events: dict[int, dict] = {}
        offset = 0
        pages = 0
        reached_seen = False
        failed = False
        while offset <= FEED_MAX_OFFSET:
            try:
                page = await self.albion.get_recent_events(limit=FEED_PAGE_SIZE, offset=offset)
            except AlbionAPIError:
                log.warning("Falha ao ler o feed global (offset=%s); seguindo com %s evento(s) novo(s) lidos até aqui.", offset, len(new_events))
                failed = True
                break
            pages += 1
            overlaps = False
            for item in page:
                if not isinstance(item, dict) or item.get("EventId") is None:
                    continue
                event_id = int(item["EventId"])
                if event_id < self._feed_floor or (event_id in self._seen_ids and event_id not in self._unconfirmed_ids):
                    overlaps = True
                    continue
                if event_id not in self._seen_ids:
                    new_events.setdefault(event_id, item)
            if first_scan or overlaps or len(page) < FEED_PAGE_SIZE:
                reached_seen = True
                break
            offset += FEED_PAGE_SIZE
        if not reached_seen and not failed:
            log.warning("Feed global: %s eventos lidos sem encontrar eventos já vistos; os mais antigos podem ter sido perdidos.", offset)
        for event_id in new_events:
            self._remember(event_id)
        if failed:
            self._unconfirmed_ids.update(new_events)
        else:
            self._unconfirmed_ids.clear()
        if first_scan:
            if pages:
                self._feed_ready = True
                self._feed_floor = min(new_events, default=0)
                log.info("Feed global: leitura inicial marcou %s evento(s) como vistos; a partir de agora só entram eventos novos.", len(new_events))
            return []
        times = [t for t in (parse_timestamp(e.get("TimeStamp")) for e in new_events.values()) if t]
        if times:
            newest = max(times)
            lag_minutes = (datetime.now(timezone.utc) - newest).total_seconds() / 60
            newest_text = f"{newest.isoformat()} (há {lag_minutes:.0f} min)"
        else:
            newest_text = "nenhum"
        log.info("Feed global: %s página(s), %s evento(s) novo(s); mais recente: %s.", pages, len(new_events), newest_text)
        return list(new_events.values())

    def _involves_guild(self, event: dict, guild_id: str) -> bool:
        return _guild_id(event.get("Killer")) == guild_id or _guild_id(event.get("Victim")) == guild_id

    async def _poll_guild(self, cfg, feed_events: list[dict]):
        if not cfg.activated_at or not cfg.albion_guild_id:
            return
        candidates = {
            int(event["EventId"]): event
            for event in feed_events
            if isinstance(event, dict) and event.get("EventId") is not None and self._involves_guild(event, cfg.albion_guild_id)
        }
        log.info(
            "DiscordGuildId=%s AlbionGuild=%s (%s): %s evento(s) novo(s) da guilda no feed.",
            cfg.discord_guild_id, cfg.albion_guild_name, cfg.albion_guild_id, len(candidates),
        )

        for event_id, event in sorted(candidates.items(), key=lambda pair: parse_timestamp(pair[1].get("TimeStamp")) or datetime.min.replace(tzinfo=timezone.utc)):
            await self._publish_event(cfg, event_id, event)

    async def _publish_event(self, cfg, event_id: int, event: dict):
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
            return
        record = CombatEventRecord(event_id, cfg.discord_guild_id, event)
        await self.repos.add_if_new(record)
        stored = await self.repos.get_event(cfg.discord_guild_id, event_id)
        if not stored or stored.status == "PUBLISHED":
            log.debug("Evento %s já publicado; ignorando.", event_id)
            return
        all_sent = True
        silver = await self.prices.estimate_value(victim_items(event)) if self.renderer and self.prices else None
        for kind, channel_id in kinds:
            if kind in stored.message_ids:
                continue
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                log.warning("Canal inválido/indisponível (%s, tipo=%s) para evento %s.", channel_id, type(channel).__name__, event_id)
                all_sent = False
                continue
            embed, file = await self._build_message(event, kind, silver)
            try:
                message = await channel.send(embed=embed, file=file)
                log.info("Evento %s (%s) publicado em #%s (%s), mensagem %s.", event_id, kind, channel.name, channel_id, message.id)
                await self.repos.update_event(cfg.discord_guild_id, event_id, "PENDING", message.id, kind)
                stored = await self.repos.get_event(cfg.discord_guild_id, event_id)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Falha ao publicar evento %s no canal %s.", event_id, channel_id)
                all_sent = False
        if all_sent:
            await self.repos.update_event(cfg.discord_guild_id, event_id, "PUBLISHED")

    async def _build_message(self, event: dict, kind: str, silver: float | None) -> tuple[discord.Embed, discord.File | None]:
        timestamp = parse_timestamp(event.get("TimeStamp"))
        if self.renderer:
            try:
                png = await self.renderer.render(event, kind, silver)
            except Exception:
                log.exception("Falha ao gerar o card do evento %s; enviando só texto.", event.get("EventId"))
            else:
                embed = discord.Embed(title=TITLES[kind], color=self._color(kind))
                embed.set_image(url=f"attachment://{CARD_FILENAME}")
                if timestamp:
                    embed.timestamp = timestamp
                embed.set_footer(text=f"Event ID: {event.get('EventId', 'desconhecido')}")
                return embed, discord.File(io.BytesIO(png), filename=CARD_FILENAME)
        return self._make_embed(event, kind), None

    @staticmethod
    def _color(kind: str) -> discord.Color:
        return discord.Color.green() if kind == "kill" else discord.Color.red()

    def _make_embed(self, event: dict, kind: str) -> discord.Embed:
        killer = event.get("Killer") or {}
        victim = event.get("Victim") or {}
        embed = discord.Embed(title=TITLES[kind], color=self._color(kind))
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
