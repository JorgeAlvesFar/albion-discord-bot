import asyncio
import io
import logging
import re
import time
from datetime import datetime, timezone
import discord
from app.integrations.albion.client import AlbionAPIError
from app.modules.combat_tracker.card import victim_items
from app.repositories.models import CombatEventRecord

log = logging.getLogger(__name__)

# A API do Albion envia até 7 casas decimais nos segundos; datetime aceita no máximo 6.
_EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")

# O endpoint events?guildId= só traz kills da guilda; mortes exigem uma consulta por membro.
MAX_DEATH_LOOKUPS_PER_CYCLE = 25
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
    def __init__(self, bot, albion, repos, poll_seconds=45, member_sync_seconds=900, renderer=None, prices=None):
        self.bot, self.albion, self.repos = bot, albion, repos
        self.renderer, self.prices = renderer, prices
        self.poll_seconds = max(30, min(60, poll_seconds))
        self.member_sync_seconds = member_sync_seconds
        self._task = None
        self._locks = {}
        self._initial_published: set[tuple[int, datetime]] = set()
        self._members: dict[int, tuple[str, float, list[dict]]] = {}
        self._member_cursor: dict[int, int] = {}

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

    async def _fetch_kill_events(self, cfg) -> list[dict]:
        events = []
        seen_page_signatures = set()
        offset = 0
        page_size = 51
        # Bound the scan to avoid runaway requests. API behavior must be validated in production.
        for _ in range(5):
            page = await self.albion.get_events_page(cfg.albion_guild_id, limit=page_size, offset=offset)
            log.info("Página de kills offset=%s: %s evento(s).", offset, len(page))
            if not page:
                break
            signature = tuple(str(x.get("EventId")) for x in page if isinstance(x, dict))
            if not signature or signature in seen_page_signatures:
                log.warning("Paginação repetida/vazia para DiscordGuildId=%s; interrompendo varredura.", cfg.discord_guild_id)
                break
            seen_page_signatures.add(signature)
            events.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return events

    async def _guild_members(self, cfg) -> list[dict]:
        cached = self._members.get(cfg.discord_guild_id)
        cached_members = cached[2] if cached and cached[0] == cfg.albion_guild_id else None
        if cached_members is not None and time.monotonic() - cached[1] < self.member_sync_seconds:
            return cached_members
        try:
            members = await self.albion.get_guild_members(cfg.albion_guild_id)
        except AlbionAPIError:
            log.warning("Falha ao atualizar membros de %s; usando a lista anterior (%s membro(s)).", cfg.albion_guild_name, len(cached_members or []))
            return cached_members or []
        members = [m for m in members if isinstance(m, dict) and m.get("Id")]
        self._members[cfg.discord_guild_id] = (cfg.albion_guild_id, time.monotonic(), members)
        log.info("Lista de membros de %s atualizada: %s membro(s).", cfg.albion_guild_name, len(members))
        return members

    async def _fetch_death_events(self, cfg) -> list[dict]:
        members = await self._guild_members(cfg)
        if not members:
            log.info("Nenhum membro conhecido em %s; mortes não consultadas neste ciclo.", cfg.albion_guild_name)
            return []
        batch_size = min(len(members), MAX_DEATH_LOOKUPS_PER_CYCLE)
        start = self._member_cursor.get(cfg.discord_guild_id, 0) % len(members)
        batch = [members[(start + i) % len(members)] for i in range(batch_size)]
        self._member_cursor[cfg.discord_guild_id] = start + batch_size
        results = await asyncio.gather(*(self.albion.get_player_deaths(m["Id"]) for m in batch), return_exceptions=True)
        events = []
        failures = 0
        for member, result in zip(batch, results):
            if isinstance(result, BaseException):
                failures += 1
                log.warning("Falha ao consultar mortes de %s (%s): %s", member.get("Name"), member["Id"], type(result).__name__)
                continue
            events.extend(result)
        log.info(
            "Mortes consultadas para %s de %s membro(s) (%s falha(s)): %s evento(s).",
            len(batch), len(members), failures, len(events),
        )
        return events

    async def _poll_guild(self, cfg):
        # Endpoint é consultado somente depois de uma configuração válida e ativada.
        activation = cfg.activated_at
        if not activation or not cfg.albion_guild_id:
            return
        log.info(
            "Consultando eventos: DiscordGuildId=%s AlbionGuild=%s (%s) ativado_em=%s",
            cfg.discord_guild_id, cfg.albion_guild_name, cfg.albion_guild_id, activation.isoformat(),
        )
        kills_on = bool(cfg.kills_enabled and cfg.kills_channel_id)
        deaths_on = bool(cfg.deaths_enabled and cfg.deaths_channel_id)
        kill_events = await self._fetch_kill_events(cfg) if kills_on else []
        death_events = await self._fetch_death_events(cfg) if deaths_on else []

        candidates = {}
        newest_event_time = None
        invalid_timestamps = []
        latest_kill = None
        latest_death = None
        for item in kill_events + death_events:
            if not isinstance(item, dict) or item.get("EventId") is None:
                continue
            event_time = parse_timestamp(item.get("TimeStamp"))
            if event_time is None:
                invalid_timestamps.append(item.get("TimeStamp"))
                continue
            if newest_event_time is None or event_time > newest_event_time:
                newest_event_time = event_time
            if _guild_id(item.get("Killer")) == cfg.albion_guild_id and (latest_kill is None or event_time > latest_kill[0]):
                latest_kill = (event_time, item)
            if _guild_id(item.get("Victim")) == cfg.albion_guild_id and (latest_death is None or event_time > latest_death[0]):
                latest_death = (event_time, item)
            if event_time >= activation:
                candidates[int(item["EventId"])] = item
        if invalid_timestamps:
            log.warning("%s evento(s) com TimeStamp inválido descartado(s). Exemplo: %r", len(invalid_timestamps), invalid_timestamps[0])
        if newest_event_time:
            lag_minutes = (datetime.now(timezone.utc) - newest_event_time).total_seconds() / 60
            newest_text = f"{newest_event_time.isoformat()} (há {lag_minutes:.0f} min)"
        else:
            newest_text = "nenhum"
        log.info(
            "Eventos recebidos: kills=%s mortes=%s; após a ativação: %s; evento mais recente: %s.",
            len(kill_events), len(death_events), len(candidates), newest_text,
        )

        initial_key = (cfg.discord_guild_id, activation)
        initial_ids = []
        if initial_key not in self._initial_published:
            # Na ativação, publica a última kill e a última morte já existentes para o canal não começar vazio.
            for kind, latest, enabled in (("kill", latest_kill, kills_on), ("death", latest_death, deaths_on)):
                if not enabled:
                    continue
                if latest is None:
                    log.info("Nenhum evento de %s anterior à ativação encontrado para publicar.", kind)
                    continue
                event_id = int(latest[1]["EventId"])
                initial_ids.append(event_id)
                candidates.setdefault(event_id, latest[1])
                log.info("Último evento de %s já existente: %s (%s); será publicado se ainda não foi.", kind, event_id, latest[0].isoformat())

        for event_id, event in sorted(candidates.items(), key=lambda pair: parse_timestamp(pair[1].get("TimeStamp")) or datetime.min.replace(tzinfo=timezone.utc)):
            await self._publish_event(cfg, event_id, event)

        if initial_key not in self._initial_published:
            stored_initial = [await self.repos.get_event(cfg.discord_guild_id, event_id) for event_id in initial_ids]
            if all(record and record.status == "PUBLISHED" for record in stored_initial):
                self._initial_published.add(initial_key)
            else:
                log.warning("Últimos eventos da ativação ainda não publicados para DiscordGuildId=%s; nova tentativa no próximo ciclo.", cfg.discord_guild_id)

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
                png = await self.renderer.render(event, kind, silver, timestamp)
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
