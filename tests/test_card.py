import io
import httpx
import pytest
from PIL import Image
from app.integrations.albion.prices import AlbionPriceClient
from app.integrations.albion.regions import prices_url_for
from app.modules.combat_tracker.card import _inventory_layout, draw_card, event_items, format_short, item_key, victim_items

def _png(color) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (128, 128), color).save(output, "PNG")
    return output.getvalue()

EVENT = {
    "EventId": 1,
    "TotalVictimKillFame": 111555,
    "numberOfParticipants": 2,
    "Killer": {"Name": "Killer", "GuildName": "Guild A", "AllianceTag": "ABC", "AverageItemPower": 1324.4,
               "Equipment": {"MainHand": {"Type": "T6_2H_AXE@1", "Quality": 3, "Count": 1}, "Food": None}},
    "Victim": {"Name": "Victim", "GuildName": "Guild B", "AverageItemPower": 1269.0,
               "Equipment": {"Head": {"Type": "T5_HEAD_PLATE_SET3@2", "Quality": 2, "Count": 1}},
               "Inventory": [{"Type": "T7_HIDE", "Quality": 0, "Count": 25}, None]},
}

def test_format_short():
    assert format_short(1_332_470) == "1.33M"
    assert format_short(111_555) == "111.56K"
    assert format_short(1_000) == "1K"
    assert format_short(240) == "240"

def test_items_skip_empty_slots():
    assert [i["Type"] for i in victim_items(EVENT)] == ["T5_HEAD_PLATE_SET3@2", "T7_HIDE"]
    assert len(event_items(EVENT)) == 3

def test_draw_card_renders_png_with_missing_icons():
    icons = {item_key(EVENT["Killer"]["Equipment"]["MainHand"]): _png((200, 0, 0, 255))}
    fame = Image.new("RGBA", (80, 64), (255, 0, 0, 255))
    data = draw_card(EVENT, "kill", 1_332_470, icons, _png((0, 0, 255, 255)), fame)
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.width > image.height * 1.3

def test_draw_card_without_silver_or_inventory():
    event = {**EVENT, "Victim": {**EVENT["Victim"], "Inventory": []}}
    data = draw_card(event, "death", None, {}, None, None)
    assert Image.open(io.BytesIO(data)).format == "PNG"

def test_inventory_shrinks_to_fit_two_rows_when_possible():
    assert _inventory_layout(8, 880) == (64, 12)
    size, cols = _inventory_layout(30, 880)
    assert size < 64 and -(-30 // cols) <= 2
    inventory = [{"Type": f"T4_ITEM_{i}", "Quality": 0, "Count": i} for i in range(48)]
    event = {**EVENT, "Victim": {**EVENT["Victim"], "Inventory": inventory}}
    size, cols = _inventory_layout(len(inventory), 880)
    assert size == 36 and -(-48 // cols) == 3
    assert Image.open(io.BytesIO(draw_card(event, "death", 0, {}, None, None))).format == "PNG"

def test_prices_url_for_known_and_unknown_hosts():
    assert prices_url_for("https://gameinfo-ams.albiononline.com/api/gameinfo") == "https://europe.albion-online-data.com"
    assert prices_url_for("https://example.com") == "https://west.albion-online-data.com"

@pytest.mark.asyncio
async def test_price_estimate_uses_quality_then_any_quality():
    rows = [
        {"item_id": "T6_2H_AXE@1", "quality": 3, "sell_price_min": 100},
        {"item_id": "T6_2H_AXE@1", "quality": 3, "sell_price_min": 300},
        {"item_id": "T7_HIDE", "quality": 1, "sell_price_min": 10},
        {"item_id": "T7_HIDE", "quality": 1, "sell_price_min": 0},
    ]
    client = AlbionPriceClient("https://prices.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)))
    items = [{"Type": "T6_2H_AXE@1", "Quality": 3, "Count": 1}, {"Type": "T7_HIDE", "Quality": 0, "Count": 25}, {"Type": "SEM_PRECO", "Count": 1}]
    assert await client.estimate_value(items) == 200 + 250
    await client.close()

@pytest.mark.asyncio
async def test_price_estimate_returns_none_on_failure():
    client = AlbionPriceClient("https://prices.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert await client.estimate_value([{"Type": "T7_HIDE", "Count": 1}]) is None
    await client.close()
