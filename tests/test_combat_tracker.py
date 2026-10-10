from datetime import datetime, timezone, timedelta
from app.modules.combat_tracker.service import parse_timestamp, _guild_id

def test_parse_timestamp_utc():
    assert parse_timestamp("2026-10-10T12:00:00Z") == datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

def test_parse_timestamp_truncates_extra_fraction_digits():
    assert parse_timestamp("2026-10-10T04:33:03.620919100Z") == datetime(2026, 10, 10, 4, 33, 3, 620919, tzinfo=timezone.utc)

def test_parse_timestamp_rejects_invalid():
    assert parse_timestamp("not-a-time") is None

def test_guild_classification_uses_ids():
    assert _guild_id({"GuildId": "abc", "GuildName": "Same name"}) == "abc"
    assert _guild_id({"GuildName": "Guild"}) is None
