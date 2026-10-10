from datetime import datetime, timezone
from types import SimpleNamespace
import discord
import pytest
from app.integrations.albion.client import AlbionAPIError
from app.modules.combat_tracker.service import CombatTracker
from app.repositories.memory import MemoryRepositories
from app.repositories.models import ServerConfig

GUILD = "G1"

def _event(event_id, killer_guild="X", victim_guild="Y", ts="2026-10-10T08:00:00Z"):
    return {"EventId": event_id, "TimeStamp": ts, "Killer": {"Name": "k", "GuildId": killer_guild}, "Victim": {"Name": "v", "GuildId": victim_guild}}

class FakeAlbion:
    def __init__(self, feed):
        self.feed = feed
        self.offsets = []
        self.fail_at_offset = None

    async def get_recent_events(self, limit, offset):
        if offset == self.fail_at_offset:
            raise AlbionAPIError("indisponível")
        self.offsets.append(offset)
        return self.feed[offset:offset + limit]

class FakeChannel(discord.TextChannel):
    def __init__(self):
        self.name = "canal"
        self.sent = []

    async def send(self, embed=None, file=None):
        self.sent.append(embed)
        return SimpleNamespace(id=len(self.sent))

def _tracker(feed):
    channels = {10: FakeChannel(), 20: FakeChannel()}
    tracker = CombatTracker(SimpleNamespace(get_channel=channels.get), FakeAlbion(feed), MemoryRepositories())
    return tracker, channels

@pytest.mark.asyncio
async def test_first_scan_only_marks_events_as_seen():
    tracker, _ = _tracker([_event(i) for i in range(100, 0, -1)])
    assert await tracker._scan_feed() == []
    assert tracker.albion.offsets == [0]
    assert tracker._seen_ids == set(range(100, 49, -1))

@pytest.mark.asyncio
async def test_scan_reads_until_a_page_with_only_seen_events():
    feed = [_event(i) for i in range(100, 0, -1)]
    tracker, _ = _tracker(feed)
    await tracker._scan_feed()
    # 60 eventos novos chegam no topo: a primeira página é toda nova, a segunda mistura novos e vistos.
    tracker.albion.feed = [_event(i) for i in range(160, 100, -1)] + feed
    tracker.albion.offsets.clear()
    new = await tracker._scan_feed()
    assert sorted(e["EventId"] for e in new) == list(range(101, 161))
    assert tracker.albion.offsets == [0, 51]
    tracker.albion.offsets.clear()
    assert await tracker._scan_feed() == []
    assert tracker.albion.offsets == [0]

@pytest.mark.asyncio
async def test_history_below_first_scan_is_never_new():
    feed = [_event(i) for i in range(100, 0, -1)]
    tracker, _ = _tracker(feed)
    await tracker._scan_feed()
    # O feed "embaralha" e devolve eventos antigos no topo: continuam sendo histórico.
    tracker.albion.feed = [_event(i) for i in range(49, 0, -1)] + feed
    assert await tracker._scan_feed() == []

@pytest.mark.asyncio
async def test_late_event_in_overlap_page_is_caught():
    feed = [_event(i) for i in range(200, 100, -1)]
    tracker, _ = _tracker(feed)
    await tracker._scan_feed()
    # Um evento atrasado (id 175, dentro da janela já lida) aparece só agora no topo.
    tracker.albion.feed = [_event(201), _event(175)] + [e for e in feed if e["EventId"] != 175]
    tracker._seen_ids.discard(175)
    new = await tracker._scan_feed()
    assert sorted(e["EventId"] for e in new) == [175, 201]

@pytest.mark.asyncio
async def test_failure_mid_scan_keeps_events_read_so_far():
    feed = [_event(i) for i in range(100, 0, -1)]
    tracker, _ = _tracker(feed)
    await tracker._scan_feed()
    tracker.albion.feed = [_event(i) for i in range(200, 100, -1)] + feed
    tracker.albion.fail_at_offset = 51
    new = await tracker._scan_feed()
    assert sorted(e["EventId"] for e in new) == list(range(150, 201))
    tracker.albion.fail_at_offset = None
    new = await tracker._scan_feed()
    assert sorted(e["EventId"] for e in new) == list(range(101, 150))

@pytest.mark.asyncio
async def test_poll_publishes_only_new_feed_events_of_the_guild():
    tracker, channels = _tracker([])
    cfg = ServerConfig(1, GUILD, "Guild", None, True, True, True, 10, 20, datetime(2026, 10, 10, tzinfo=timezone.utc))
    events = [_event(1, killer_guild=GUILD), _event(2, victim_guild=GUILD), _event(3)]
    await tracker._poll_guild(cfg, events)
    assert len(channels[10].sent) == 1 and len(channels[20].sent) == 1
    await tracker._poll_guild(cfg, events)
    assert len(channels[10].sent) == 1 and len(channels[20].sent) == 1
