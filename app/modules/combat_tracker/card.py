import asyncio
import io
import logging
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
# O Discord limita a altura das imagens de embed: um card mais largo que alto é exibido maior.
WIDTH, PAD, SLOT, GAP, MIDDLE = 960, 40, 76, 8, 232
GRID_TOP = 170
INV_MAX_SLOT, INV_MIN_SLOT, INV_MAX_ROWS = 64, 36, 2

BG, PANEL, SLOT_BG, SLOT_LINE, LINE = (17, 18, 23), (24, 26, 32), (36, 38, 46), (52, 55, 66), (48, 51, 61)
WHITE, MUTED, DIM, GOLD = (242, 243, 245), (160, 165, 178), (110, 115, 128), (250, 204, 21)
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

# A fonte embutida no Pillow (Aileron) não tem acentos: os textos do card evitam letras acentuadas.
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

def _spaced(draw, xy, value, size, fill, align="left", spacing=3):
    font = _font(size)
    widths = [draw.textlength(char, font=font) for char in value]
    total = sum(widths) + spacing * (len(value) - 1)
    x, y = xy
    if align == "right":
        x -= total
    elif align == "center":
        x -= total / 2
    for char, width in zip(value, widths):
        draw.text((x, y), char, font=font, fill=fill)
        draw.text((x + 0.5, y), char, font=font, fill=fill)
        x += width + spacing

def _diamond(draw, cx, cy, radius, fill):
    draw.polygon([(cx, cy - radius), (cx + radius, cy), (cx, cy + radius), (cx - radius, cy)], fill=fill)

def _people_icon(height: int) -> Image.Image:
    scale = 4
    size = height * scale
    image = Image.new("RGBA", (size * 3 // 2, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for cx, radius, color in ((size * 0.38, size * 0.16, DIM), (size * 1.12, size * 0.16, DIM), (size * 0.75, size * 0.2, MUTED)):
        draw.ellipse((cx - radius, size * 0.12, cx + radius, size * 0.12 + radius * 2), fill=color)
        draw.pieslice((cx - radius * 1.9, size * 0.12 + radius * 2.1, cx + radius * 1.9, size * 0.12 + radius * 6), 180, 360, fill=color)
    return image.resize((image.width // scale, image.height // scale), Image.LANCZOS)

def _slot(canvas, draw, icon, x, y, size, count, count_size):
    draw.rounded_rectangle((x, y, x + size, y + size), radius=8, fill=SLOT_BG, outline=SLOT_LINE)
    if icon is None:
        return
    canvas.alpha_composite(icon.resize((size, size), Image.LANCZOS), (x, y))
    if count and count > 1:
        draw.text((x + size - 7, y + size - 5), str(count), font=_font(count_size), fill="white", anchor="rs", stroke_width=3, stroke_fill="black")

def _guild_line(entity: dict[str, Any]) -> str:
    guild = entity.get("GuildName") or "No guild"
    tag = entity.get("AllianceTag")
    return (f"{guild} [{tag}]" if tag else guild).upper()

def _ip(entity: dict[str, Any]) -> str:
    value = entity.get("AverageItemPower")
    return f"IP {round(value)}" if isinstance(value, (int, float)) else "IP ?"

def _inventory_layout(count: int, box_w: int) -> tuple[int, int]:
    """Maior slot (e colunas) que faz o inventário caber em até INV_MAX_ROWS linhas."""
    for size in range(INV_MAX_SLOT, INV_MIN_SLOT - 1, -4):
        cols = max(1, (box_w + GAP) // (size + GAP))
        if -(-count // cols) <= INV_MAX_ROWS:
            return size, cols
    return INV_MIN_SLOT, max(1, (box_w + GAP) // (INV_MIN_SLOT + GAP))

def draw_card(
    event: dict[str, Any],
    kind: str,
    silver: float | None,
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
    grid_bottom = GRID_TOP + grid_h
    inv_size, inv_cols = _inventory_layout(len(inventory), WIDTH - 2 * PAD) if inventory else (0, 1)
    inv_rows = -(-len(inventory) // inv_cols)
    inv_top = grid_bottom + 72
    height = inv_top + inv_rows * (inv_size + GAP) - GAP + 32 if inventory else grid_bottom + 36

    canvas = Image.new("RGBA", (WIDTH, height), BG)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((12, 12, WIDTH - 12, height - 12), radius=14, fill=PANEL, outline=LINE)
    center = WIDTH / 2

    left, right = PAD, WIDTH - PAD
    _spaced(draw, (left, 34), "KILLER", 14, GREEN)
    _spaced(draw, (right, 34), "VICTIM", 14, RED, align="right")
    _text(draw, (left, 56), str(killer.get("Name") or "?"), 36, WHITE, bold=True)
    _text(draw, (right, 56), str(victim.get("Name") or "?"), 36, WHITE, bold=True, anchor="ra")
    _spaced(draw, (left, 104), _guild_line(killer), 15, MUTED, spacing=2)
    _spaced(draw, (right, 104), _guild_line(victim), 15, MUTED, align="right", spacing=2)
    _text(draw, (left, 128), _ip(killer), 26, GREEN, bold=True)
    _text(draw, (right, 128), _ip(victim), 26, RED, bold=True, anchor="ra")
    _text(draw, (center, 82), "VS", 34, DIM, bold=True, anchor="mm")
    draw.line((center - 150, 82, center - 42, 82), fill=LINE, width=2)
    draw.line((center + 42, 82, center + 150, 82), fill=LINE, width=2)

    margin = (WIDTH - (grid_w * 2 + MIDDLE)) // 2
    for side, equipment in enumerate(sides):
        x0 = margin + side * (grid_w + MIDDLE)
        for row_index, row in enumerate(EQUIPMENT_GRID):
            for col_index, slot_name in enumerate(row):
                if slot_name is None:
                    continue
                item = equipment.get(slot_name)
                icon = images.get(item_key(item)) if isinstance(item, dict) and item.get("Type") else None
                count = item.get("Count") if isinstance(item, dict) else None
                _slot(canvas, draw, icon, x0 + col_index * (SLOT + GAP), GRID_TOP + row_index * (SLOT + GAP), SLOT, count, 20)

    for line_x in (margin + grid_w + 18, WIDTH - margin - grid_w - 18):
        draw.line((line_x, GRID_TOP + 8, line_x, grid_bottom - 8), fill=LINE, width=1)
        _diamond(draw, line_x, GRID_TOP + grid_h / 2, 5, LINE)

    participants = event.get("numberOfParticipants") or len(event.get("Participants") or [])
    stats = [(_fit(fame_icon, 34) if fame_icon else None, f"{event.get('TotalVictimKillFame') or 0:,}", GOLD, "fama")]
    if silver is not None:
        silver_image = _open(silver_icon)
        stats.append((_fit(silver_image, 34) if silver_image else None, f"~{format_short(silver)}", WHITE, "valor"))
    stats.append((_people_icon(26), str(participants), WHITE, "participante" if participants == 1 else "participantes"))
    block_h, separator_h = 88, 26
    y = GRID_TOP + (grid_h - (len(stats) * block_h + (len(stats) - 1) * separator_h)) / 2
    for index, (icon, value, color, label) in enumerate(stats):
        if icon:
            canvas.alpha_composite(icon, (int(center - icon.width / 2), int(y + 17 - icon.height / 2)))
        _text(draw, (center, y + 52), value, 24, color, bold=True, anchor="mm")
        _text(draw, (center, y + 76), label, 15, MUTED, anchor="mm")
        y += block_h
        if index < len(stats) - 1:
            _diamond(draw, center, y + separator_h / 2, 4, DIM)
            y += separator_h

    if inventory:
        draw.line((PAD, grid_bottom + 24, WIDTH - PAD, grid_bottom + 24), fill=LINE, width=1)
        _spaced(draw, (center, grid_bottom + 36), "ITENS CARREGADOS", 14, MUTED, align="center")
        used_cols = min(inv_cols, len(inventory))
        x0 = (WIDTH - (used_cols * (inv_size + GAP) - GAP)) // 2
        for index, item in enumerate(inventory):
            row_index, col_index = divmod(index, inv_cols)
            _slot(canvas, draw, images.get(item_key(item)), x0 + col_index * (inv_size + GAP), inv_top + row_index * (inv_size + GAP), inv_size, item.get("Count"), max(13, inv_size // 4 + 2))

    output = io.BytesIO()
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output.getvalue()

class KillCardRenderer:
    """Monta o card da kill/morte com os ícones oficiais, consultados sob demanda no render do Albion."""

    def __init__(self, timeout: float = 10, max_concurrency: int = 8, cache_size: int = 1000):
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

    async def render(self, event: dict[str, Any], kind: str, silver: float | None) -> bytes:
        keys = sorted({item_key(item) for item in event_items(event)})
        results = await asyncio.gather(*(self._icon(item_type, quality) for item_type, quality in keys))
        icons = {key: data for key, data in zip(keys, results) if data}
        silver_icon = await self._icon(SILVER_ICON_TYPE, 0, 64) if silver is not None else None
        return await asyncio.to_thread(draw_card, event, kind, silver, icons, silver_icon, self._fame_icon)
