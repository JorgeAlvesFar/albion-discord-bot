import pytest
from app.repositories.memory import MemoryRepositories
from app.repositories.models import ServerConfig, CombatEventRecord

@pytest.mark.asyncio
async def test_configs_are_isolated_by_discord_guild():
    repo = MemoryRepositories()
    await repo.save_config(ServerConfig(1, albion_guild_id="A"))
    await repo.save_config(ServerConfig(2, albion_guild_id="B"))
    assert (await repo.get_config(1)).albion_guild_id == "A"
    assert (await repo.get_config(2)).albion_guild_id == "B"

@pytest.mark.asyncio
async def test_event_deduplication_is_scoped_by_server():
    repo = MemoryRepositories()
    event = {"EventId": 42}
    assert await repo.add_if_new(CombatEventRecord(42, 1, event))
    assert not await repo.add_if_new(CombatEventRecord(42, 1, event))
    assert await repo.add_if_new(CombatEventRecord(42, 2, event))
