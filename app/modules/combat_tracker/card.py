import asyncio
import io
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
import httpx
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

FAME_ICON_PATH = Path(__file__).resolve().parents[2] / "assets" / "fame.png"
ICON_URL = "https://render.albiononline.com/v1/item/{}.png"
SILVER_ICON_TYPE = "T1_SILVERBAG_NONTRADABLE"
ICON_FETCH_SIZE = 128

EQUIPMENT_GRID = (
    ("Bag", "Head", "Cape"),
    ("MainHand", "Armor", "OffHand"),
    ("Potion", "Shoes", "Food"),
    (None, "Mount", None),
)
SLOT, INV_SLOT, GAP, PAD, MIDDLE, INV_PER_ROW = 88, 64, 8, 32, 96, 9
HEADER_HEIGHT, STATS_HEIGHT = 172, 56

BG, SLOT_BG, SLOT_LINE, LINE = (21, 22, 28), (36, 38, 46), (52, 55, 66), (48, 51, 61)
WHITE, MUTED, DIM, GOLD = (242, 243, 245), (160, 165, 178), (120, 125, 138), (250, 204, 21)
GREEN, RED = (74, 222, 128), (239, 68, 68)

ItemKey = tuple[str, int]

def format_short(value: float) -> str:
    for divisor, suffix in ((1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.2f}".rstrip("0").rstrip(".") + suffix
    return str(int(value))

def item_key(item: dict[str, Any]) -> ItemKey:
    return item["Type"], int(item.get("Quality") or 0)

def _items(values) -> list[dict[str, Any]]:
    return [item for item in values or [] if isinstance(item, dict) and item.get("Type")]

def victim_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    victim = event.get("Victim") or {}
    return _items((victim.get("Equipment") or {}).values()) + _items(victim.get("Inventory"))

def event_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    killer = event.get("Killer") or {}
    return _items((killer.get("Equipment") or {}).values()) + victim_items(event)

# A fonte embutida no Pillow (Aileron) não tem acentos, por isso os rótulos do card são em inglês.
@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)

def _open(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except (OSError, ValueError):
        return None

def _fit(image: Image.Image, height: int) -> Image.Image:
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    return image.resize((max(1, round(image.width * height / image.height)), height), Image.LANCZOS)

def _text(draw, xy, value, size, fill, bold=False, anchor="la"):
    # A fonte embutida não tem negrito; um contorno fino na mesma cor simula o peso.
    draw.text(xy, value, font=_font(size), fill=fill, anchor=anchor, stroke_width=1 if bold and size >= 20 else 0, stroke_fill=fill)

def _spaced(draw, xy, value, size, fill, align_right=False, spacing=2):
    font = _font(size)
    widths = [draw.textlength(char, font=font) for char in value]
    x, y = xy
    if align_right:
        x -= sum(widths) + spacing * (len(value) - 1)
    for char, width in zip(value, widths):
        draw.text((x, y), char, font=font, fill=fill)
        draw.text((x + 0.5, y), char, font=font, fill=fill)
        x += width + spacing

def _slot(canvas, draw, icon, x, y, size, count, count_size):
    draw.rounded_rectangle((x, y, x + size, y + size), radius=8, fill=SLOT_BG, outline=SLOT_LINE)
    if icon is None:
        return
    canvas.alpha_composite(icon.resize((size, size), Image.LANCZOS), (x, y))
    if count and count > 1:
        draw.text((x + size - 8, y + size - 6), str(count), font=_font(count_size), fill="white", anchor="rs", stroke_width=3, stroke_fill="black")

def _guild_line(entity: dict[str, Any]) -> str:
    guild = entity.get("GuildName") or "No guild"
    tag = entity.get("AllianceTag")
    return (f"{guild} [{tag}]" if tag else guild).upper()

def _ip(entity: dict[str, Any]) -> str:
    value = entity.get("AverageItemPower")
    return f"IP {round(value)}" if isinstance(value, (int, float)) else "IP ?"

def draw_card(
    event: dict[str, Any],
    kind: str,
    silver: float | None,
    timestamp: datetime | None,
    region: str,
    icons: dict[ItemKey, bytes],
    silver_icon: bytes | None,
    fame_icon: Image.Image | None,
) -> bytes:
    killer = event.get("Killer") or {}
    victim = event.get("Victim") or {}
    sides = [killer.get("Equipment") or {}, victim.get("Equipment") or {}]
    inventory = _items(victim.get("Inventory"))
    images = {key: _open(data) for key, data in icons.items()}

    grid_w, grid_h = 3 * SLOT + 2 * GAP, 4 * SLOT + 3 * GAP
    width = PAD * 2 + grid_w * 2 + MIDDLE
    inv_rows = (len(inventory) + INV_PER_ROW - 1) // INV_PER_ROW
    inv_h = inv_rows * INV_SLOT + max(inv_rows - 1, 0) * GAP
    height = 6 + HEADER_HEIGHT + STATS_HEIGHT + grid_h + PAD + (28 + 30 + inv_h + PAD if inventory else 0)

    canvas = Image.new("RGBA", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, 5), fill=GREEN if kind == "kill" else RED)

    when = f" · {timestamp:%Y-%m-%d %H:%M} UTC" if timestamp else ""
    _text(draw, (width - PAD, 30), f"{region}{when}", 13, MUTED, anchor="ra")

    left, right = PAD, width - PAD
    _spaced(draw, (left, 64), "KILLER", 11, GREEN)
    _spaced(draw, (right, 64), "VICTIM", 11, RED, align_right=True)
    _text(draw, (left, 82), str(killer.get("Name") or "?"), 28, WHITE, bold=True)
    _text(draw, (right, 82), str(victim.get("Name") or "?"), 28, WHITE, bold=True, anchor="ra")
    _text(draw, (left, 120), _guild_line(killer), 13, MUTED)
    _text(draw, (right, 120), _guild_line(victim), 13, MUTED, anchor="ra")
    _text(draw, (left, 142), _ip(killer), 20, GREEN, bold=True)
    _text(draw, (right, 142), _ip(victim), 20, RED, bold=True, anchor="ra")

    y = 6 + HEADER_HEIGHT
    participants = event.get("numberOfParticipants") or len(event.get("Participants") or [])
    groups = [(_fit(fame_icon, 26) if fame_icon else None, f"{event.get('TotalVictimKillFame') or 0:,}", GOLD, " fame")]
    if silver is not None:
        silver_image = _open(silver_icon)
        groups.append((_fit(silver_image, 30) if silver_image else None, f"~{format_short(silver)}", WHITE, " silver"))
    groups.append((None, str(participants), WHITE, " participants"))
    separator, icon_gap = 34, 8

    def group_width(icon, value, _color, label):
        return (icon.width + icon_gap if icon else 0) + draw.textlength(value, font=_font(18)) + draw.textlength(label, font=_font(15))

    x = (width - (sum(group_width(*group) for group in groups) + separator * (len(groups) - 1))) / 2
    middle_y = y + STATS_HEIGHT / 2 - 8
    for index, (icon, value, color, label) in enumerate(groups):
        if icon:
            canvas.alpha_composite(icon, (int(x), int(middle_y - icon.height / 2)))
            x += icon.width + icon_gap
        draw.text((x, middle_y), value, font=_font(18), fill=color, anchor="lm")
        x += draw.textlength(value, font=_font(18))
        draw.text((x, middle_y), label, font=_font(15), fill=MUTED, anchor="lm")
        x += draw.textlength(label, font=_font(15))
        if index < len(groups) - 1:
            draw.text((x + separator / 2, middle_y), "·", font=_font(18), fill=DIM, anchor="mm")
            x += separator
    y += STATS_HEIGHT

    for side, equipment in enumerate(sides):
        x0 = PAD + side * (grid_w + MIDDLE)
        for row_index, row in enumerate(EQUIPMENT_GRID):
            for col_index, slot_name in enumerate(row):
                if slot_name is None:
                    continue
                item = equipment.get(slot_name)
                icon = images.get(item_key(item)) if isinstance(item, dict) and item.get("Type") else None
                count = item.get("Count") if isinstance(item, dict) else None
                _slot(canvas, draw, icon, x0 + col_index * (SLOT + GAP), y + row_index * (SLOT + GAP), SLOT, count, 22)
    _text(draw, (width / 2, y + grid_h / 2), "VS", 22, DIM, bold=True, anchor="mm")
    y += grid_h + PAD

    if inventory:
        draw.line((PAD, y, width - PAD, y), fill=LINE, width=1)
        y += 28
        _text(draw, (width / 2, y), "VICTIM'S INVENTORY", 13, MUTED, anchor="mm")
        y += 30
        inv_w = min(len(inventory), INV_PER_ROW) * (INV_SLOT + GAP) - GAP
        x0 = (width - inv_w) // 2
        for index, item in enumerate(inventory):
            row_index, col_index = divmod(index, INV_PER_ROW)
            _slot(canvas, draw, images.get(item_key(item)), x0 + col_index * (INV_SLOT + GAP), y + row_index * (INV_SLOT + GAP), INV_SLOT, item.get("Count"), 18)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output.getvalue()

class KillCardRenderer:
    """Monta o card da kill/morte com os ícones oficiais, consultados sob demanda no render do Albion."""

    def __init__(self, region: str, timeout: float = 10, max_concurrency: int = 8, cache_size: int = 1000):
        self.region = region
        self.cache_size = cache_size
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "AlbionDiscordBot/0.1"})
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._icons: dict[tuple[str, int, int], bytes] = {}
        try:
            self._fame_icon = Image.open(FAME_ICON_PATH).convert("RGBA")
        except OSError:
            log.warning("Ícone de fama não encontrado em %s; o card sairá sem ele.", FAME_ICON_PATH)
            self._fame_icon = None

    async def close(self):
        await self._client.aclose()

    async def _icon(self, item_type: str, quality: int, size: int = ICON_FETCH_SIZE) -> bytes | None:
        key = (item_type, quality, size)
        if key in self._icons:
            return self._icons[key]
        params = {"size": size}
        if quality:
            params["quality"] = quality
        try:
            async with self._semaphore:
                response = await self._client.get(ICON_URL.format(item_type), params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Ícone indisponível para %s (qualidade %s): %s", item_type, quality, type(exc).__name__)
            return None
        if len(self._icons) >= self.cache_size:
            self._icons.clear()
        self._icons[key] = response.content
        return response.content

    async def render(self, event: dict[str, Any], kind: str, silver: float | None, timestamp: datetime | None) -> bytes:
        keys = sorted({item_key(item) for item in event_items(event)})
        results = await asyncio.gather(*(self._icon(item_type, quality) for item_type, quality in keys))
        icons = {key: data for key, data in zip(keys, results) if data}
        silver_icon = await self._icon(SILVER_ICON_TYPE, 0, 64) if silver is not None else None
        return await asyncio.to_thread(draw_card, event, kind, silver, timestamp, self.region, icons, silver_icon, self._fame_icon)
