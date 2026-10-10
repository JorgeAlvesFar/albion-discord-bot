import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()  # Precisa rodar antes de importar app.*, que lê as variáveis na importação.

from app.main import main  # noqa: E402


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot encerrado pelo sinal de interrupção.")
