import asyncio
from datetime import timedelta
import discord
import pytest
from app.modules.combat_tracker import service
from app.modules.combat_tracker.publisher import MAX_ATTEMPTS
from app.modules.combat_tracker.service import MIN_POLL_SECONDS, ScanResult
from app.repositories.models import DISCARDED, FAILED, PENDING, PUBLISHED
from tests.fakes import ACTIVATION, GUILD, FakeChannel, bump_version, config, deactivate, event, http_error, make_tracker, reactivate
from tests.fakes import now as _utcnow

async def enqueue(tracker, events):
    return await tracker.enqueue(ScanResult(events=events, observed_at=ACTIVATION + timedelta(minutes=10)))

async def publish_due(tracker):
    for record in await tracker.repos.due_events(_utcnow()):
        await tracker.publisher.process(record)

async def force_retry_now(tracker):
    for record in tracker.repos._events.values():
        for delivery in record.deliveries.values():
            delivery.next_attempt_at = _utcnow() - timedelta(seconds=1)

# ---------- Defeito 1: filtro temporal ----------

@pytest.mark.asyncio
async def test_event_before_activation_that_arrives_late_is_not_queued():
    tracker, channels = make_tracker()
    await tracker.repos.save_config(config())
    # Ativação 12:00, combate 11:55, evento visto pelo coletor às 12:10.
    discards = await enqueue(tracker, [event(1, killer_guild=GUILD, ts="2026-10-18T11:55:00Z")])
    await publish_due(tracker)
    assert discards["anterior_a_ativacao"] == 1
    assert channels[10].sent == []

@pytest.mark.asyncio
@pytest.mark.parametrize("ts, queued", [
    ("2026-10-18T11:59:59.999999Z", False),
    ("2026-10-18T12:00:00Z", True),
    ("2026-10-18T12:00:00.0000001Z", True),
    ("2026-10-18T09:00:01-03:00", True),
    ("2026-10-18T08:59:59-03:00", False),
])
async def test_activation_boundary_and_timezones(ts, queued):
    tracker, _ = make_tracker()
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD, ts=ts)])
    assert (await tracker.repos.get_event(1, 1) is not None) == queued

@pytest.mark.asyncio
async def test_activation_is_revalidated_before_publishing():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)
    await enqueue(tracker, [event(1, killer_guild=GUILD, ts="2026-10-18T12:05:00Z")])
    # Reativação enquanto o evento esperava na fila: nova referência temporal posterior ao combate.
    reactivate(cfg, ACTIVATION + timedelta(minutes=30))
    await publish_due(tracker)
    assert channels[10].sent == []
    assert await tracker.repos.get_event(1, 1) is None

# ---------- Defeito 2: novas tentativas ----------

@pytest.mark.asyncio
async def test_temporary_failure_is_retried_without_new_feed_read():
    channels = {10: FakeChannel(10, failures=[http_error(503)]), 20: FakeChannel(20)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    await publish_due(tracker)
    record = await tracker.repos.get_event(1, 1)
    delivery = record.deliveries["kill"]
    assert delivery.status == PENDING and delivery.attempts == 1 and delivery.next_attempt_at > _utcnow()
    # Ainda não chegou a hora da nova tentativa: nada é enviado.
    await publish_due(tracker)
    assert channels[10].sent == []
    await force_retry_now(tracker)
    await publish_due(tracker)
    assert len(channels[10].sent) == 1
    assert await tracker.repos.get_event(1, 1) is None

@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried():
    forbidden = discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "Missing Permissions")
    channels = {10: FakeChannel(10, failures=[forbidden]), 20: FakeChannel(20)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    record = await tracker.repos.get_event(1, 1)
    await publish_due(tracker)
    assert record.deliveries["kill"].status == FAILED and record.deliveries["kill"].attempts == 1
    assert await tracker.repos.get_event(1, 1) is None

@pytest.mark.asyncio
async def test_deleted_channel_fails_permanently():
    tracker, _ = make_tracker(channels={})
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    record = await tracker.repos.get_event(1, 1)
    await publish_due(tracker)
    assert record.deliveries["kill"].status == FAILED and "inacessível" in record.deliveries["kill"].last_error

@pytest.mark.asyncio
async def test_retries_stop_at_limit():
    channels = {10: FakeChannel(10, failures=[http_error(503)] * 20), 20: FakeChannel(20)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    record = await tracker.repos.get_event(1, 1)
    for _ in range(MAX_ATTEMPTS + 3):
        await force_retry_now(tracker)
        await publish_due(tracker)
    assert record.deliveries["kill"].status == FAILED and record.deliveries["kill"].attempts == MAX_ATTEMPTS

@pytest.mark.asyncio
async def test_kill_and_death_have_independent_states():
    # Membros da guilda se enfrentando: o mesmo evento é kill e morte.
    channels = {10: FakeChannel(10), 20: FakeChannel(20, failures=[http_error(500)])}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(1, killer_guild=GUILD, victim_guild=GUILD)])
    record = await tracker.repos.get_event(1, 1)
    await publish_due(tracker)
    assert record.deliveries["kill"].status == PUBLISHED and record.deliveries["death"].status == PENDING
    assert await tracker.repos.get_event(1, 1) is record
    await force_retry_now(tracker)
    await publish_due(tracker)
    assert len(channels[10].sent) == 1 and len(channels[20].sent) == 1
    assert await tracker.repos.get_event(1, 1) is None

# ---------- Defeito 3: coletor independente do publicador ----------

@pytest.mark.asyncio
async def test_collector_keeps_running_while_publisher_is_blocked(monkeypatch):
    monkeypatch.setattr(service, "MIN_POLL_SECONDS", 0)
    release = asyncio.Event()
    channels = {10: FakeChannel(10, block=release), 20: FakeChannel(20)}
    tracker, _ = make_tracker([event(i) for i in range(10, 0, -1)], channels=channels, poll_seconds=1)
    tracker.max_poll_seconds = 0
    await tracker.repos.save_config(config())
    tracker.start()
    await asyncio.sleep(1.2)  # leitura inicial
    tracker.feed.albion.feed = [event(11, killer_guild=GUILD)] + tracker.feed.albion.feed
    await asyncio.sleep(1.2)  # o evento 11 entra na fila e o publicador fica preso no envio
    scans_before = len(tracker.feed.albion.offsets)
    tracker.feed.albion.feed = [event(12, killer_guild=GUILD)] + tracker.feed.albion.feed
    await asyncio.sleep(2.5)
    assert len(tracker.feed.albion.offsets) > scans_before
    assert await tracker.repos.get_event(1, 12) is not None
    release.set()
    await asyncio.sleep(0.5)
    tracker.bot.closed = True
    await tracker.close()
    assert len(channels[10].sent) >= 1

@pytest.mark.asyncio
async def test_publisher_sends_a_burst_in_parallel():
    class SlowChannel(FakeChannel):
        async def send(self, embed=None, file=None):
            await asyncio.sleep(0.3)
            return await super().send(embed=embed, file=file)

    channels = {10: SlowChannel(10), 20: FakeChannel(20)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config())
    await enqueue(tracker, [event(i, killer_guild=GUILD) for i in range(1, 9)])
    tracker.start()
    await asyncio.sleep(1.2)  # em série, 8 envios de 0,3 s levariam 2,4 s
    tracker.bot.closed = True
    await tracker.close()
    assert len(channels[10].sent) == 8

# ---------- Defeito 5: intervalo adaptativo ----------

def scan(new=0, hit_limit=False):
    return ScanResult(events=[event(i) for i in range(new)], hit_offset_limit=hit_limit)

def test_interval_shrinks_with_volume_and_grows_slowly_when_quiet():
    tracker, _ = make_tracker(poll_seconds=60)
    tracker._adapt_interval(ScanResult(first_scan=True), started=0)
    assert tracker.interval == 60
    tracker._adapt_interval(scan(new=600), started=60)  # 10 eventos/s -> 300 eventos a cada 30 s
    assert tracker.interval == 30
    tracker._adapt_interval(scan(new=900), started=90)  # 30 eventos/s
    assert tracker.interval == MIN_POLL_SECONDS
    tracker._adapt_interval(scan(new=0), started=100)
    assert tracker.interval == MIN_POLL_SECONDS + 15
    tracker._adapt_interval(scan(new=0), started=125)
    assert tracker.interval == MIN_POLL_SECONDS + 30
    tracker._adapt_interval(scan(new=20, hit_limit=True), started=165)
    assert tracker.interval == MIN_POLL_SECONDS
    tracker._adapt_interval(scan(new=5), started=175)  # 0,5 evento/s -> 600 s, limitado pelo crescimento máximo
    assert tracker.interval == MIN_POLL_SECONDS + 15
    for start in range(200, 400, 40):
        tracker._adapt_interval(scan(new=0), started=start)
    assert tracker.interval == 60  # nunca passa do máximo configurado

# ---------- Defeito 6: desativação e reconfiguração ----------

@pytest.mark.asyncio
async def test_deactivation_discards_pending_notifications():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)
    await enqueue(tracker, [event(1, killer_guild=GUILD), event(2, victim_guild=GUILD)])
    deactivate(cfg)
    assert await tracker.invalidate(1, "tracker desativado") == 2
    await publish_due(tracker)
    assert channels[10].sent == [] and channels[20].sent == []
    assert await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_old_version_is_discarded_even_without_explicit_invalidation():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    record = await tracker.repos.get_event(1, 1)
    bump_version(cfg)  # guilda trocada / killboard redefinido
    await publish_due(tracker)
    assert channels[10].sent == [] and record.deliveries["kill"].status == DISCARDED

@pytest.mark.asyncio
async def test_deactivation_during_card_render_prevents_send():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)

    class SlowRenderer:
        async def render(self, event, kind, silver):
            deactivate(cfg)  # admin desativa enquanto o card é gerado
            raise RuntimeError("sem card")

    tracker.publisher.renderer = SlowRenderer()
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    await publish_due(tracker)
    assert channels[10].sent == []

@pytest.mark.asyncio
async def test_reactivation_does_not_replay_old_events():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)
    old = event(1, killer_guild=GUILD)
    await enqueue(tracker, [old])
    deactivate(cfg)
    await tracker.invalidate(1, "tracker desativado")
    reactivate(cfg)
    discards = await enqueue(tracker, [old])
    await publish_due(tracker)
    assert discards["duplicado"] == 1 and channels[10].sent == []

@pytest.mark.asyncio
async def test_published_message_is_kept_after_deactivation():
    tracker, channels = make_tracker()
    cfg = config()
    await tracker.repos.save_config(cfg)
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    await publish_due(tracker)
    deactivate(cfg)
    assert await tracker.invalidate(1, "tracker desativado") == 0
    assert len(channels[10].sent) == 1

# ---------- Isolamento, classificação e deduplicação ----------

@pytest.mark.asyncio
async def test_same_event_goes_to_each_server_by_its_own_rules():
    channels = {10: FakeChannel(10), 20: FakeChannel(20), 30: FakeChannel(30), 40: FakeChannel(40)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(config(discord_guild_id=1, guild="A", kills=10, deaths=20))
    await tracker.repos.save_config(config(discord_guild_id=2, guild="B", kills=30, deaths=40))
    await enqueue(tracker, [event(7, killer_guild="A", victim_guild="B")])
    await publish_due(tracker)
    assert len(channels[10].sent) == 1 and channels[20].sent == []  # servidor 1: kill da guilda A
    assert channels[30].sent == [] and len(channels[40].sent) == 1  # servidor 2: morte da guilda B

@pytest.mark.asyncio
async def test_rejections_report_the_right_reason():
    tracker, _ = make_tracker()
    await tracker.repos.save_config(config(deaths=None))
    discards = await enqueue(tracker, [
        event(1, victim_guild=GUILD),  # morte com notificações de morte desativadas
        event(2, killer_guild=GUILD, ts="2026-10-18T11:00:00Z"),  # anterior à ativação
        event(3, killer_guild=GUILD, ts="não é data"),  # TimeStamp inválido
        event(4),  # não envolve a guilda: nem entra na avaliação
    ])
    assert dict(discards) == {"tipo_desativado": 1, "anterior_a_ativacao": 1, "timestamp_invalido": 1}

@pytest.mark.asyncio
async def test_inactive_config_gets_nothing():
    tracker, _ = make_tracker()
    cfg = config()
    deactivate(cfg)
    await tracker.repos.save_config(cfg)
    await enqueue(tracker, [event(1, killer_guild=GUILD)])
    assert await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_duplicate_event_is_queued_once():
    tracker, channels = make_tracker()
    await tracker.repos.save_config(config())
    first = await enqueue(tracker, [event(1, killer_guild=GUILD)])
    second = await enqueue(tracker, [event(1, killer_guild=GUILD)])
    await publish_due(tracker)
    third = await enqueue(tracker, [event(1, killer_guild=GUILD)])
    assert not first and second["duplicado"] == 1 and third["duplicado"] == 1
    assert len(channels[10].sent) == 1
