import logging

import discord
from discord.ext import commands

from app.core.config import settings
from app.core.logging import configure_logging
from app.repositories.memory import MemoryRepositories
from app.integrations.albion.client import AlbionClient
from app.integrations.albion.prices import AlbionPriceClient
from app.integrations.albion.regions import prices_url_for
from app.modules.guild_registration.cog import GuildRegistrationCog
from app.modules.members_config.cog import MembersConfigCog
from app.modules.member_registration.cog import MemberRegistrationCog
from app.modules.member_registration.service import MemberSync
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
        self.card_renderer = KillCardRenderer(timeout=settings.albion_http_timeout)
        self.prices = AlbionPriceClient(prices_url_for(settings.albion_api_base_url), timeout=settings.albion_http_timeout)
        self.tracker = CombatTracker(
            self, self.albion, self.repos, settings.combat_poll_seconds,
            renderer=self.card_renderer, prices=self.prices,
        )
        await self.add_cog(GuildRegistrationCog(self, self.albion, self.repos, self.tracker))
        self.member_sync = MemberSync(self, self.albion, self.repos, settings.member_sync_seconds)
        await self.add_cog(MembersConfigCog(self, self.repos))
        await self.add_cog(MemberRegistrationCog(self, self.albion, self.repos))
        await self.add_cog(CombatCog(self, self.repos, self.tracker))
        # Sincronização global: substitui o conjunto de comandos (um comando removido some do Discord em alguns minutos).
        synced = await self.tree.sync()
        self.tracker.start()
        self.member_sync.start()
        log.info("Comandos de aplicação sincronizados: %s.", ", ".join(f"/{command.name}" for command in synced))

    async def close(self):
        for task_owner in (getattr(self, "tracker", None), getattr(self, "member_sync", None)):
            if task_owner:
                await task_owner.close()
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
