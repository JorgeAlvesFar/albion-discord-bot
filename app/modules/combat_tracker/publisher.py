"""Publicador: consome a fila em memória e envia as notificações ao Discord, com novas tentativas."""
import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from app.modules.combat_tracker.events import victim_items
from app.modules.combat_tracker.messages import build_message
from app.repositories.models import DISCARDED, FAILED, PENDING, PUBLISHED, CombatEventRecord, Delivery

log = logging.getLogger(__name__)

# Novas tentativas de publicação após erros temporários do Discord.
RETRY_DELAYS = (15, 30, 60, 120, 300)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
IDLE_SECONDS = 30
# Publicações simultâneas: um lote grande da API não espera card por card. O discord.py respeita os limites de envio.
CONCURRENCY = 4

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _seconds(start: datetime | None, end: datetime | None) -> str:
    return f"{(end - start).total_seconds():.0f}s" if start and end else "?"

class PermanentPublishError(Exception):
    """Falha que não se resolve tentando de novo (canal apagado, sem permissão etc.)."""

class Publisher:
    def __init__(self, bot, repos, renderer=None, prices=None):
        self.bot, self.repos = bot, repos
        self.renderer, self.prices = renderer, prices
        self.wakeup = asyncio.Event()
        self.stats: Counter = Counter()

    async def run(self):
        await self.bot.wait_until_ready()
        log.info("Publicador de combate iniciado.")
        while not self.bot.is_closed():
            # Limpo antes de processar: um aviso do coletor durante o processamento não se perde.
            self.wakeup.clear()
            try:
                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def process(record: CombatEventRecord) -> None:
                    async with semaphore:
                        try:
                            await self.process(record)
                        except Exception:
                            log.exception("Erro não previsto ao publicar o evento %s de DiscordGuildId=%s.", record.event_id, record.discord_guild_id)

                await asyncio.gather(*(process(record) for record in await self.repos.due_events(_utcnow())))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro não previsto no publicador.")
            next_due = await self.repos.next_due_at()
            timeout = IDLE_SECONDS if next_due is None else max(1.0, (next_due - _utcnow()).total_seconds())
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=min(timeout, IDLE_SECONDS))
            except asyncio.TimeoutError:
                pass

    async def _discard_reason(self, record: CombatEventRecord, delivery: Delivery) -> str | None:
        """Revalida, com a configuração atual, se a notificação ainda pode ser publicada."""
        cfg = await self.repos.get_config(record.discord_guild_id)
        if not cfg or not cfg.killboard_active:
            return "killboard desativado"
        killboard = cfg.killboard
        if killboard.version != record.config_version:
            return f"configuração substituída (versão {record.config_version} -> {killboard.version})"
        if record.combat_at < killboard.activated_at:
            return "combate anterior à ativação"
        if killboard.settings.channel_for(delivery.kind) != delivery.channel_id:
            return "canal ou tipo de notificação alterado"
        return None

    async def process(self, record: CombatEventRecord) -> None:
        silver = None
        for delivery in record.deliveries.values():
            if delivery.status != PENDING or delivery.next_attempt_at > _utcnow():
                continue
            reason = await self._discard_reason(record, delivery)
            if reason:
                self._finish(record, delivery, DISCARDED, reason)
                continue
            delivery.attempts += 1
            try:
                channel = await self._resolve_channel(delivery.channel_id)
                if silver is None and self.renderer and self.prices:
                    silver = await self.prices.estimate_value(victim_items(record.payload))
                embed, file = await build_message(self.renderer, record.payload, delivery.kind, silver)
                # O card pode levar segundos; revalida para não enviar depois de uma desativação feita nesse meio-tempo.
                reason = await self._discard_reason(record, delivery)
                if reason:
                    self._finish(record, delivery, DISCARDED, reason)
                    continue
                message = await channel.send(embed=embed, file=file)
            except PermanentPublishError as exc:
                self._finish(record, delivery, FAILED, str(exc))
                self.stats["failed"] += 1
                continue
            except discord.HTTPException as exc:
                if isinstance(exc, (discord.Forbidden, discord.NotFound)) or (400 <= exc.status < 500 and exc.status != 429):
                    self._finish(record, delivery, FAILED, f"erro permanente do Discord: HTTP {exc.status} {exc.text}".strip())
                else:
                    self._schedule_retry(record, delivery, f"HTTP {exc.status}")
                self.stats["failed"] += 1
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self._schedule_retry(record, delivery, type(exc).__name__)
                self.stats["failed"] += 1
                continue
            delivery.message_id = message.id
            delivery.published_at = _utcnow()
            delivery.status = PUBLISHED
            self.stats["published"] += 1
            log.info(
                "Evento %s (%s) publicado para DiscordGuildId=%s no canal %s, mensagem %s (tentativa %s): combate=%s visto=%s publicado=%s; "
                "atraso da API=%s, atraso do bot=%s, total=%s.",
                record.event_id, delivery.kind, record.discord_guild_id, delivery.channel_id, message.id, delivery.attempts,
                record.combat_at.isoformat(), record.observed_at.isoformat(), delivery.published_at.isoformat(),
                _seconds(record.combat_at, record.observed_at), _seconds(record.observed_at, delivery.published_at),
                _seconds(record.combat_at, delivery.published_at),
            )
        if await self.repos.finalize(record):
            log.debug("Evento %s de DiscordGuildId=%s concluído e liberado da memória.", record.event_id, record.discord_guild_id)

    def _finish(self, record: CombatEventRecord, delivery: Delivery, status: str, reason: str) -> None:
        delivery.status = status
        delivery.last_error = reason
        log.log(
            logging.WARNING if status == FAILED else logging.INFO, "Evento %s (%s) de DiscordGuildId=%s %s após %s tentativa(s): %s.",
            record.event_id, delivery.kind, record.discord_guild_id,
            "falhou definitivamente" if status == FAILED else "descartado", delivery.attempts, reason,
        )

    def _schedule_retry(self, record: CombatEventRecord, delivery: Delivery, reason: str) -> None:
        if delivery.attempts >= MAX_ATTEMPTS:
            self._finish(record, delivery, FAILED, f"{reason}; limite de {MAX_ATTEMPTS} tentativas atingido")
            return
        delay = RETRY_DELAYS[min(delivery.attempts, len(RETRY_DELAYS)) - 1]
        delivery.last_error = reason
        delivery.next_attempt_at = _utcnow() + timedelta(seconds=delay)
        log.warning(
            "Falha temporária ao publicar evento %s (%s) para DiscordGuildId=%s (tentativa %s/%s): %s; nova tentativa em %ss.",
            record.event_id, delivery.kind, record.discord_guild_id, delivery.attempts, MAX_ATTEMPTS, reason, delay,
        )

    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise PermanentPublishError(f"canal {channel_id} inacessível ({type(exc).__name__})") from exc
        if not isinstance(channel, discord.TextChannel):
            raise PermanentPublishError(f"canal {channel_id} não é um canal de texto ({type(channel).__name__})")
        me = getattr(channel.guild, "me", None)
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links and (perms.attach_files or not self.renderer)):
                raise PermanentPublishError(f"sem permissão em #{channel.name}: ver canal, enviar mensagens, incorporar links e anexar arquivos")
        return channel
