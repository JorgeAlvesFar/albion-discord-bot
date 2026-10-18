from datetime import datetime, timezone
import pytest
from app.repositories.memory import MemoryRepositories
from app.repositories.models import PUBLISHED, CombatEventRecord, Delivery, GuildLink, ServerConfig

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

def record(event_id, discord_guild_id=1):
    return CombatEventRecord(event_id, discord_guild_id, {"EventId": event_id}, NOW, NOW, 1, {"kill": Delivery("kill", 10)})

@pytest.mark.asyncio
async def test_configs_are_isolated_by_discord_guild():
    repo = MemoryRepositories()
    await repo.save_config(ServerConfig(1, GuildLink("A", "Guilda A")))
    await repo.save_config(ServerConfig(2, GuildLink("B", "Guilda B")))
    assert (await repo.get_config(1)).guild.guild_id == "A"
    assert (await repo.get_config(2)).guild.guild_id == "B"
    assert (await repo.get_or_create_config(3)).guild is None
    assert (await repo.get_or_create_config(1)).guild.guild_id == "A"

@pytest.mark.asyncio
async def test_event_deduplication_is_scoped_by_server():
    repo = MemoryRepositories()
    assert await repo.add_if_new(record(42, 1))
    assert not await repo.add_if_new(record(42, 1))
    assert await repo.add_if_new(record(42, 2))

@pytest.mark.asyncio
async def test_finished_event_leaves_memory_but_stays_deduplicated():
    repo = MemoryRepositories()
    rec = record(42)
    await repo.add_if_new(rec)
    assert not await repo.finalize(rec)  # ainda pendente
    rec.deliveries["kill"].status = PUBLISHED
    assert await repo.finalize(rec)
    assert await repo.get_event(1, 42) is None
    assert not await repo.add_if_new(record(42))

@pytest.mark.asyncio
async def test_completed_keys_have_bounded_retention():
    repo = MemoryRepositories(completed_limit=3)
    for event_id in range(5):
        rec = record(event_id)
        await repo.add_if_new(rec)
        rec.deliveries["kill"].status = PUBLISHED
        await repo.finalize(rec)
    assert len(repo._completed) == 3
    assert await repo.add_if_new(record(0))  # esquecido pela retenção; o piso do feed impede que ele volte do feed

@pytest.mark.asyncio
async def test_discard_pending_only_affects_its_server():
    repo = MemoryRepositories()
    await repo.add_if_new(record(1, 1))
    await repo.add_if_new(record(1, 2))
    assert await repo.discard_pending(1, "teste") == 1
    assert await repo.pending_count() == 1
    assert await repo.get_event(2, 1) is not None
