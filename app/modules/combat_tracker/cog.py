import discord
from discord import app_commands
from discord.ext import commands

class CombatCog(commands.Cog):
    def __init__(self, bot, repos):
        self.bot, self.repos = bot, repos

    @app_commands.command(name="status_tracker", description="Mostra o estado do tracker de combate deste servidor")
    @app_commands.guild_only()
    async def status_tracker(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Este comando só funciona em um servidor.", ephemeral=True)
            return
        cfg = await self.repos.get_config(interaction.guild.id)
        if not cfg:
            await interaction.response.send_message("Este servidor ainda não está configurado. Use `/configurar`.", ephemeral=True)
            return
        state = "ATIVO" if cfg.combat_config_valid else "DESATIVADO"
        lines = [
            f"**Tracker:** {state}",
            f"**Guilda Albion:** {cfg.albion_guild_name or 'Não configurada'}",
            f"**Kills:** {'habilitadas' if cfg.kills_enabled else 'desabilitadas'}",
            f"**Mortes:** {'habilitadas' if cfg.deaths_enabled else 'desabilitadas'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
