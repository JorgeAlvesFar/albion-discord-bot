"""Leitura dos eventos de combate da API do Albion: horário, guilda dos envolvidos e itens."""
import re
from datetime import datetime, timezone
from typing import Any

# A API do Albion envia até 7 casas decimais nos segundos; datetime aceita no máximo 6.
_EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")

def parse_timestamp(value) -> datetime | None:
    """Horário do combate em UTC, ou None se o valor for inválido."""
    if not value:
        return None
    try:
        text = _EXTRA_FRACTION_DIGITS.sub(r"\1", str(value)).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def guild_id_of(entity) -> str | None:
    return str(entity.get("GuildId")) if isinstance(entity, dict) and entity.get("GuildId") else None

def event_id_of(event) -> int | None:
    return int(event["EventId"]) if isinstance(event, dict) and event.get("EventId") is not None else None

def _items(values) -> list[dict[str, Any]]:
    return [item for item in values or [] if isinstance(item, dict) and item.get("Type")]

def victim_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Equipamento e inventário da vítima: o que entra na estimativa de prata."""
    victim = event.get("Victim") or {}
    return _items((victim.get("Equipment") or {}).values()) + _items(victim.get("Inventory"))

def event_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Todos os itens que aparecem no card: equipamento do killer, equipamento e inventário da vítima."""
    killer = event.get("Killer") or {}
    return _items((killer.get("Equipment") or {}).values()) + victim_items(event)

def inventory_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    return _items((event.get("Victim") or {}).get("Inventory"))
