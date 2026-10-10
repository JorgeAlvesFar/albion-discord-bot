from datetime import datetime, timezone, timedelta
from app.modules.combat_tracker.service import parse_timestamp, _guild_id

def test_parse_timestamp_utc():
    assert parse_timestamp("2026-10-10T12:00:00Z") == datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

def test_parse_timestamp_rejects_invalid():
    assert parse_timestamp("not-a-time") is None

def test_guild_classification_uses_ids():
    assert _guild_id({"GuildId": "abc", "GuildName": "Same name"}) == "abc"
    assert _guild_id({"GuildName": "Guild"}) is None
