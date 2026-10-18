"""Tracker de combate: coordena o coletor (feed global → fila em memória) e o publicador (fila → Discord)."""
import asyncio
import logging
import time
from collections import Counter
from app.modules.combat_tracker.events import event_id_of, guild_id_of, parse_timestamp
from app.modules.combat_tracker.feed import FeedReader, ScanResult
from app.modules.combat_tracker.publisher import Publisher
from app.repositories.models import CombatEventRecord, Delivery

log = logging.getLogger(__name__)

# Intervalo adaptativo da coleta: mira em no máximo TARGET_EVENTS_PER_SCAN eventos novos por leitura,
# bem abaixo do alcance da paginação, para sobrar margem em picos.
MIN_POLL_SECONDS = 10
TARGET_EVENTS_PER_SCAN = 300
MAX_INTERVAL_GROWTH = 15

def _seconds(start, end) -> str:
    return f"{(end - start).total_seconds():.0f}s" if start and end else "?"

class CombatTracker:
    def __init__(self, bot, albion, repos, poll_seconds=45, renderer=None, prices=None):
        self.bot, self.repos = bot, repos
        self.feed = FeedReader(albion)
        self.publisher = Publisher(bot, repos, renderer, prices)
        self.max_poll_seconds = max(MIN_POLL_SECONDS, poll_seconds)
        self.interval = float(self.max_poll_seconds)
        self._interval_reason = "inicial"
        self._last_scan_started: float | None = None
        self._tasks: list[asyncio.Task] = []

    def start(self):
        if not any(not task.done() for task in self._tasks):
            self._tasks = [
                asyncio.create_task(self._collector_loop(), name="combat-collector"),
                asyncio.create_task(self.publisher.run(), name="combat-publisher"),
            ]

    async def close(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def invalidate(self, discord_guild_id: int, reason: str) -> int:
        """Descarta as notificações pendentes de um servidor cuja configuração do killboard mudou."""
        discarded = await self.repos.discard_pending(discord_guild_id, reason)
        log.info("DiscordGuildId=%s: %s notificação(ões) pendente(s) descartada(s) (%s).", discord_guild_id, discarded, reason)
        return discarded

    async def _collector_loop(self):
        await self.bot.wait_until_ready()
        log.info("Coletor de combate iniciado (intervalo entre %ss e %ss).", MIN_POLL_SECONDS, self.max_poll_seconds)
        while not self.bot.is_closed():
            started = time.monotonic()
            try:
                scan = await self.feed.scan()
                discards = await self.enqueue(scan)
                self._adapt_interval(scan, started)
                await self._log_cycle(scan, discards)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto no ciclo do coletor.")
            await asyncio.sleep(max(1.0, self.interval - (time.monotonic() - started)))

    async def enqueue(self, scan: ScanResult) -> Counter:
        """Cria as notificações pendentes dos eventos novos para cada servidor com killboard ativo."""
        discards: Counter = Counter()
        if not scan.events:
            return discards
        configs = [cfg for cfg in await self.repos.all_configs() if cfg.killboard_active]
        queued = 0
        for cfg in configs:
            guild_id, killboard = cfg.guild.guild_id, cfg.killboard
            # Um resumo por servidor e motivo, com um exemplo: com a API atrasada, um ciclo pode descartar centenas de eventos.
            server_discards: Counter = Counter()
            examples: dict[str, str] = {}

            def discard(reason: str, detail: str) -> None:
                server_discards[reason] += 1
                examples.setdefault(reason, detail)
                log.debug("Evento descartado para DiscordGuildId=%s (%s): %s.", cfg.discord_guild_id, reason, detail)

            for event in scan.events:
                killer_guild, victim_guild = guild_id_of(event.get("Killer")), guild_id_of(event.get("Victim"))
                if guild_id not in (killer_guild, victim_guild):
                    continue
                event_id = event_id_of(event)
                combat_at = parse_timestamp(event.get("TimeStamp"))
                if combat_at is None:
                    discard("timestamp_invalido", f"evento {event_id} com TimeStamp {event.get('TimeStamp')!r}")
                    continue
                if combat_at < killboard.activated_at:
                    discard("anterior_a_ativacao", f"evento {event_id}, combate em {combat_at.isoformat()}, ativação em {killboard.activated_at.isoformat()}")
                    continue
                deliveries = {}
                for kind, involved in (("kill", killer_guild == guild_id), ("death", victim_guild == guild_id)):
                    channel_id = killboard.settings.channel_for(kind)
                    if involved and channel_id:
                        deliveries[kind] = Delivery(kind, channel_id)
                if not deliveries:
                    discard("tipo_desativado", f"evento {event_id} ({'kill' if killer_guild == guild_id else 'morte'})")
                    continue
                record = CombatEventRecord(event_id, cfg.discord_guild_id, event, combat_at, scan.observed_at, killboard.version, deliveries)
                if not await self.repos.add_if_new(record):
                    discard("duplicado", f"evento {event_id}")
                    continue
                queued += 1
                log.info(
                    "Evento %s enfileirado para DiscordGuildId=%s (%s): combate=%s visto=%s (atraso da API %s).",
                    event_id, cfg.discord_guild_id, "+".join(deliveries), combat_at.isoformat(), scan.observed_at.isoformat(),
                    _seconds(combat_at, scan.observed_at),
                )
            for reason, count in server_discards.items():
                log.info("DiscordGuildId=%s: %s evento(s) descartado(s) por %s (ex.: %s).", cfg.discord_guild_id, count, reason, examples[reason])
            discards.update(server_discards)
        if queued:
            self.publisher.wakeup.set()
        return discards

    def _adapt_interval(self, scan: ScanResult, started: float) -> None:
        previous_start, self._last_scan_started = self._last_scan_started, started
        if scan.first_scan or previous_start is None:
            self.interval = float(self.max_poll_seconds)
            self._interval_reason = "leitura inicial"
            return
        elapsed = max(1.0, started - previous_start)
        rate = len(scan.events) / elapsed
        if scan.hit_offset_limit:
            target, reason = MIN_POLL_SECONDS, "limite de paginação atingido"
        elif rate > 0:
            target = TARGET_EVENTS_PER_SCAN / rate
            reason = f"{rate * 60:.0f} evento(s) novo(s)/min no feed"
        else:
            target, reason = self.max_poll_seconds, "sem eventos novos"
        target = min(target, self.interval + MAX_INTERVAL_GROWTH)
        self.interval = float(min(self.max_poll_seconds, max(MIN_POLL_SECONDS, target)))
        self._interval_reason = reason

    async def _log_cycle(self, scan: ScanResult, discards: Counter) -> None:
        stats = self.publisher.stats
        published, failed = stats["published"], stats["failed"]
        stats.clear()
        lags = [(scan.observed_at - combat_at).total_seconds() for combat_at in (parse_timestamp(e.get("TimeStamp")) for e in scan.events) if combat_at]
        lag_text = f"{min(lags):.0f}s a {max(lags):.0f}s" if lags else "n/a"
        log.info(
            "Ciclo do coletor: %.1fs, %s página(s), %s recebido(s), %s novo(s), %s duplicado(s), %s inválido(s), %s erro(s) da API; "
            "atraso da API dos novos: %s; descartes=%s; fila=%s; publicações desde o último ciclo: %s ok, %s falha(s); "
            "próximo ciclo em %.0fs (%s).",
            scan.duration, scan.pages, scan.received, len(scan.events), scan.duplicates, scan.invalid, scan.api_errors, lag_text,
            dict(discards) or "nenhum", await self.repos.pending_count(), published, failed, self.interval, self._interval_reason,
        )
