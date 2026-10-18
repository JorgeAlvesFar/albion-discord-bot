import logging
import re
from app.repositories.models import MembersConfig, ServerConfig

log = logging.getLogger(__name__)

NICKNAME_MAX = 32  # limite do Discord para apelidos
ALBION_NAME_MAX = 16  # nomes de personagem do Albion têm até 16 caracteres
# "[TAG] " + nome com 16 caracteres precisa caber em 32.
TAG_MAX = NICKNAME_MAX - ALBION_NAME_MAX - len("[] ")

def normalize_tag(text: str | None) -> tuple[str | None, str | None]:
    """Devolve (tag, erro). Texto vazio = sem tag. Colchetes digitados pelo admin são removidos."""
    tag = re.sub(r"\s+", " ", (text or "").strip())
    if tag.startswith("[") and tag.endswith("]"):
        tag = tag[1:-1].strip()
    if not tag:
        return None, None
    if "[" in tag or "]" in tag:
        return None, "A tag não pode conter colchetes; eles são adicionados automaticamente: `[TAG] Nome`."
    if len(tag) > TAG_MAX:
        return None, f"A tag pode ter no máximo {TAG_MAX} caracteres, para o apelido caber no limite de {NICKNAME_MAX} do Discord."
    return tag, None

def format_nickname(tag: str | None, character_name: str) -> str:
    """`[TAG] NomeDoPersonagem`, ou só o nome quando não há tag."""
    nickname = f"[{tag}] {character_name}" if tag else character_name
    return nickname[:NICKNAME_MAX]

def validate_role(role, bot_member) -> str | None:
    """Motivo pelo qual o bot não pode atribuir o cargo, ou None se puder."""
    if role.is_default():
        return "O cargo @everyone não pode ser usado."
    if role.managed:
        return f"O cargo **{role.name}** é gerenciado por uma integração ou bot e não pode ser atribuído manualmente."
    if not bot_member.guild_permissions.manage_roles:
        return "O bot precisa da permissão **Gerenciar cargos** para atribuir o cargo."
    if role.position >= bot_member.top_role.position:
        return (
            f"O cargo **{role.name}** está acima ou na mesma posição do cargo mais alto do bot. "
            "Mova o cargo do bot para cima dele em Configurações do servidor → Cargos."
        )
    return None

def validate_tag_permission(tag: str | None, bot_member) -> str | None:
    if tag and not bot_member.guild_permissions.manage_nicknames:
        return "O bot precisa da permissão **Gerenciar apelidos** para aplicar a tag no apelido."
    return None

async def save_members(repos, discord_guild_id: int, members: MembersConfig) -> tuple[bool, ServerConfig]:
    """Salva cargo e tag; não toca na guilda nem no killboard. Devolve (mudou, configuração)."""
    cfg = await repos.get_or_create_config(discord_guild_id)
    if cfg.members == members:
        return False, cfg
    cfg.members = members
    await repos.save_config(cfg)
    log.info("DiscordGuildId=%s: membros configurados: cargo=%s tag=%r.", discord_guild_id, members.role_id, members.tag)
    return True, cfg

async def reset_members(repos, discord_guild_id: int) -> tuple[bool, ServerConfig | None]:
    cfg = await repos.get_config(discord_guild_id)
    if not cfg or cfg.members.empty:
        return False, cfg
    cfg.members = MembersConfig()
    await repos.save_config(cfg)
    log.info("DiscordGuildId=%s: configuração de membros redefinida.", discord_guild_id)
    return True, cfg
