"""Mensagens de kill/morte: embed com o card anexado ou, se o card falhar, embed em texto."""
import asyncio
import io
import logging
import discord
from app.modules.combat_tracker.events import parse_timestamp

log = logging.getLogger(__name__)

CARD_FILENAME = "combate.png"
TITLES = {"kill": "⚔️ Kill da guilda", "death": "💀 Morte da guilda"}
# O card depende do serviço de ícones do Albion, que já levou mais de 40 s por card; acima disso, publica em texto.
CARD_TIMEOUT_SECONDS = 20

def _color(kind: str) -> discord.Color:
    return discord.Color.green() if kind == "kill" else discord.Color.red()

def _footer(embed: discord.Embed, event: dict) -> discord.Embed:
    timestamp = parse_timestamp(event.get("TimeStamp"))
    if timestamp:
        embed.timestamp = timestamp
    embed.set_footer(text=f"Event ID: {event.get('EventId', 'desconhecido')}")
    return embed

async def build_message(renderer, event: dict, kind: str, silver: float | None) -> tuple[discord.Embed, discord.File | None]:
    if renderer:
        try:
            png = await asyncio.wait_for(renderer.render(event, kind, silver), timeout=CARD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("Card do evento %s não ficou pronto em %ss (serviço de ícones lento); enviando só texto.", event.get("EventId"), CARD_TIMEOUT_SECONDS)
        except Exception:
            log.exception("Falha ao gerar o card do evento %s; enviando só texto.", event.get("EventId"))
        else:
            embed = discord.Embed(title=TITLES[kind], color=_color(kind))
            embed.set_image(url=f"attachment://{CARD_FILENAME}")
            return _footer(embed, event), discord.File(io.BytesIO(png), filename=CARD_FILENAME)
    return text_embed(event, kind), None

def _entity(entity) -> str:
    if not isinstance(entity, dict):
        return "Não informado"
    name = entity.get("Name") or "Nome não informado"
    guild = entity.get("GuildName") or "Sem guilda informada"
    return f"**{name}**\n{guild}"[:1024]

def text_embed(event: dict, kind: str) -> discord.Embed:
    killer = event.get("Killer") or {}
    victim = event.get("Victim") or {}
    embed = discord.Embed(title=TITLES[kind], color=_color(kind))
    embed.add_field(name="Killer", value=_entity(killer), inline=True)
    embed.add_field(name="Vítima", value=_entity(victim), inline=True)
    if event.get("TotalVictimKillFame") is not None:
        embed.add_field(name="Fama", value=f"{event['TotalVictimKillFame']:,}", inline=True)
    equipment = victim.get("Equipment")
    if isinstance(equipment, dict):
        names = [f"{slot}: {item['Type']}" for slot, item in equipment.items() if isinstance(item, dict) and item.get("Type")]
        if names:
            embed.add_field(name="Equipamento da vítima", value="\n".join(names)[:1024], inline=False)
    return _footer(embed, event)
