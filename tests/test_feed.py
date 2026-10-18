import pytest
from app.modules.combat_tracker.feed import FEED_MAX_OFFSET, FEED_PAGE_SIZE
from tests.fakes import event, make_tracker

def ids(scan):
    return sorted(e["EventId"] for e in scan.events)

@pytest.mark.asyncio
async def test_first_scan_only_marks_events_as_seen():
    tracker, _ = make_tracker([event(i) for i in range(100, 0, -1)])
    scan = await tracker.feed.scan()
    assert scan.first_scan and scan.events == []
    assert tracker.feed.albion.offsets == [0]
    assert tracker.feed.seen_ids == set(range(100, 49, -1))

@pytest.mark.asyncio
async def test_scan_reads_until_a_page_with_only_seen_events():
    feed = [event(i) for i in range(100, 0, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    # 60 eventos novos chegam no topo: a primeira página é toda nova, a segunda mistura novos e vistos.
    tracker.feed.albion.feed = [event(i) for i in range(160, 100, -1)] + feed
    tracker.feed.albion.offsets.clear()
    scan = await tracker.feed.scan()
    assert ids(scan) == list(range(101, 161))
    assert tracker.feed.albion.offsets == [0, 51]
    assert scan.pages == 2 and scan.received == 102 and scan.duplicates == 42
    tracker.feed.albion.offsets.clear()
    assert (await tracker.feed.scan()).events == []
    assert tracker.feed.albion.offsets == [0]

@pytest.mark.asyncio
async def test_history_below_first_scan_is_never_new():
    feed = [event(i) for i in range(100, 0, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    # O feed "embaralha" e devolve eventos antigos no topo: continuam sendo histórico.
    tracker.feed.albion.feed = [event(i) for i in range(49, 0, -1)] + feed
    assert (await tracker.feed.scan()).events == []

@pytest.mark.asyncio
async def test_late_event_in_overlap_page_is_caught():
    feed = [event(i) for i in range(200, 100, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    # Um evento atrasado (id 175, dentro da janela já lida) aparece só agora no topo.
    tracker.feed.albion.feed = [event(201), event(175)] + [e for e in feed if e["EventId"] != 175]
    tracker.feed.seen_ids.discard(175)
    assert ids(await tracker.feed.scan()) == [175, 201]

@pytest.mark.asyncio
async def test_failure_mid_scan_keeps_events_read_so_far():
    feed = [event(i) for i in range(100, 0, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    tracker.feed.albion.feed = [event(i) for i in range(200, 100, -1)] + feed
    tracker.feed.albion.fail_at_offset = 51
    scan = await tracker.feed.scan()
    assert ids(scan) == list(range(150, 201)) and scan.api_errors == 1
    tracker.feed.albion.fail_at_offset = None
    assert ids(await tracker.feed.scan()) == list(range(101, 150))

@pytest.mark.asyncio
async def test_volume_beyond_pagination_limit_is_flagged():
    feed = [event(i) for i in range(100, 0, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    # Uma batalha grande: 1.200 eventos novos entre duas leituras, mais do que a API deixa alcançar.
    tracker.feed.albion.feed = [event(i) for i in range(1300, 100, -1)] + feed
    tracker.feed.albion.offsets.clear()
    scan = await tracker.feed.scan()
    reachable = FEED_MAX_OFFSET // FEED_PAGE_SIZE * FEED_PAGE_SIZE + FEED_PAGE_SIZE
    assert scan.hit_offset_limit and not scan.reached_overlap
    assert max(tracker.feed.albion.offsets) <= FEED_MAX_OFFSET
    assert len(scan.events) == reachable

@pytest.mark.asyncio
async def test_volume_close_to_limit_is_fully_read():
    feed = [event(i) for i in range(100, 0, -1)]
    tracker, _ = make_tracker(feed)
    await tracker.feed.scan()
    tracker.feed.albion.feed = [event(i) for i in range(1000, 100, -1)] + feed
    scan = await tracker.feed.scan()
    assert scan.reached_overlap and not scan.hit_offset_limit
    assert ids(scan) == list(range(101, 1001))
