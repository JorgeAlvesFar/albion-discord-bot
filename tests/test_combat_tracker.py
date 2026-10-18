from datetime import datetime, timezone
from app.modules.combat_tracker.events import event_id_of, guild_id_of, parse_timestamp

def test_parse_timestamp_utc():
    assert parse_timestamp("2026-10-10T12:00:00Z") == datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

def test_parse_timestamp_truncates_extra_fraction_digits():
    assert parse_timestamp("2026-10-10T04:33:03.620919100Z") == datetime(2026, 10, 10, 4, 33, 3, 620919, tzinfo=timezone.utc)

def test_parse_timestamp_rejects_invalid():
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(None) is None

def test_guild_classification_uses_ids():
    assert guild_id_of({"GuildId": "abc", "GuildName": "Same name"}) == "abc"
    assert guild_id_of({"GuildName": "Guild"}) is None

def test_event_id():
    assert event_id_of({"EventId": "42"}) == 42
    assert event_id_of({"EventId": None}) is None and event_id_of("x") is None
