import logging
import time

def configure_logging(level: str = "INFO") -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s UTC %(levelname)s %(name)s: %(message)s",
    )
    # Cada requisição do httpx gera uma linha INFO; os logs do tracker já resumem as consultas.
    logging.getLogger("httpx").setLevel(logging.WARNING)
