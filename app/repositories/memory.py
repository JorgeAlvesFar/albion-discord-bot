from app.repositories.config_store import ConfigStore
from app.repositories.event_store import COMPLETED_KEYS_LIMIT, EventStore
from app.repositories.link_store import LinkConflict, LinkStore

__all__ = ["MemoryRepositories", "LinkConflict"]

class MemoryRepositories(ConfigStore, LinkStore, EventStore):
    """Armazenamento em memória da aplicação: configurações, vínculos e fila de eventos.

    Cada parte tem sua própria trava; tudo se perde quando o processo reinicia.
    """

    def __init__(self, completed_limit: int = COMPLETED_KEYS_LIMIT):
        ConfigStore.__init__(self)
        LinkStore.__init__(self)
        EventStore.__init__(self, completed_limit)
