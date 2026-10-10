import logging

import discord
from discord.ext import commands

from app.core.config import settings
from app.core.logging import configure_logging
from app.repositories.memory import MemoryRepositories
from app.integrations.albion.client import AlbionClient
from app.integrations.albion.prices import AlbionPriceClient
from app.integrations.albion.regions import region_for
from app.modules.configuration.cog import ConfigurationCog
from app.modules.combat_tracker.card import KillCardRenderer
from app.modules.combat_tracker.service import CombatTracker
from app.modules.combat_tracker.cog import CombatCog

configure_logging(settings.log_level)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

class AlbionBot(commands.Bot):
    async def setup_hook(self):
        self.repos = MemoryRepositories()
        self.albion = AlbionClient(
            base_url=settings.albion_api_base_url,
            timeout=settings.albion_http_timeout,
            max_concurrency=settings.albion_max_concurrency,
            retry_attempts=settings.albion_retry_attempts,
        )
        region, prices_url = region_for(settings.albion_api_base_url)
        self.card_renderer = KillCardRenderer(region, timeout=settings.albion_http_timeout)
        self.prices = AlbionPriceClient(prices_url, timeout=settings.albion_http_timeout)
        self.tracker = CombatTracker(
            self, self.albion, self.repos, settings.combat_poll_seconds, settings.member_sync_seconds,
            renderer=self.card_renderer, prices=self.prices,
        )
        await self.add_cog(ConfigurationCog(self, self.albion, self.repos, self.tracker))
        await self.add_cog(CombatCog(self, self.repos))
        await self.tree.sync()
        self.tracker.start()
        log.info("Comandos de aplicação sincronizados.")

    async def close(self):
        tracker = getattr(self, "tracker", None)
        if tracker:
            await tracker.close()
        for client in (getattr(self, "albion", None), getattr(self, "card_renderer", None), getattr(self, "prices", None)):
            if client:
                await client.close()
        await super().close()

async def main():
    if not settings.discord_token:
        raise RuntimeError("DISCORD_TOKEN não configurado. Preencha as variáveis de ambiente.")
    bot = AlbionBot(command_prefix="!", intents=intents)
    async with bot:
        await bot.start(settings.discord_token)
