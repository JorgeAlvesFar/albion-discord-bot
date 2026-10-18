import asyncio
from dataclasses import dataclass
from app.repositories.models import MemberLink

@dataclass(frozen=True)
class LinkConflict:
    """O personagem já está vinculado a outro usuário do mesmo servidor."""
    other_user_id: int

class LinkStore:
    """Vínculos usuário Discord ↔ personagem do Albion, por servidor."""

    def __init__(self):
        self._links: dict[tuple[int, int], MemberLink] = {}
        self._links_lock = asyncio.Lock()

    async def get_link(self, discord_guild_id: int, user_id: int) -> MemberLink | None:
        async with self._links_lock:
            return self._links.get((discord_guild_id, user_id))

    async def links_for(self, discord_guild_id: int) -> list[MemberLink]:
        async with self._links_lock:
            return [link for (gid, _), link in self._links.items() if gid == discord_guild_id]

    async def save_link(self, link: MemberLink) -> LinkConflict | MemberLink | None:
        """Grava o vínculo, substituindo o anterior do mesmo usuário.

        Devolve LinkConflict se o personagem já estiver vinculado (na mesma guilda) a outro usuário do servidor;
        senão, o vínculo substituído (ou None). A verificação e a gravação ocorrem sob a mesma trava.
        """
        async with self._links_lock:
            for (gid, user_id), other in self._links.items():
                if (
                    gid == link.discord_guild_id and user_id != link.user_id
                    and other.player_id == link.player_id and other.albion_guild_id == link.albion_guild_id
                ):
                    return LinkConflict(user_id)
            previous = self._links.get((link.discord_guild_id, link.user_id))
            self._links[(link.discord_guild_id, link.user_id)] = link
            return previous
