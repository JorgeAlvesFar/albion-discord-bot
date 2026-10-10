import logging
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from app.repositories.models import ServerConfig
from app.integrations.albion.client import AlbionAPIError

log = logging.getLogger(__name__)

def _admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.guild_permissions.administrator)

class GuildSearchModal(discord.ui.Modal, title="Pesquisar guilda do Albion"):
    name = discord.ui.TextInput(label="Nome da guilda", placeholder="Digite o nome para pesquisar", max_length=100)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("Somente administradores deste servidor podem configurar o bot.", ephemeral=True)
            return
        try:
            result = await self.cog.albion.search(str(self.name).strip())
        except AlbionAPIError:
            await interaction.response.send_message("A API do Albion falhou. A configuração não foi alterada; tente novamente mais tarde.", ephemeral=True)
            return
        guilds = result.get("guilds", [])
        if not guilds:
            await interaction.response.send_message("Nenhuma guilda encontrada. Confira o nome e tente novamente.", ephemeral=True)
            return
        view = GuildResultsView(self.cog, interaction.guild.id, guilds[:25])
        await interaction.response.send_message("Selecione a guilda correta. Confira o nome e o ID antes de confirmar.", view=view, ephemeral=True)

class GuildResultsView(discord.ui.View):
    def __init__(self, cog, discord_guild_id: int, results: list[dict]):
        super().__init__(timeout=180)
        self.cog = cog
        self.discord_guild_id = discord_guild_id
        self.results = [g for g in results if g.get("Id") and g.get("Name")]
        options = []
        for idx, guild in enumerate(self.results[:25]):
            alliance = guild.get("AllianceName") or guild.get("Alliance") or "Sem aliança informada"
            label = str(guild["Name"])[:100]
            desc = f"ID: {guild['Id']} | {alliance}"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(idx)))
        if options:
            self.select.options = options
        else:
            self.select.disabled = True

    @discord.ui.select(placeholder="Escolha uma guilda", min_values=1, max_values=1, options=[discord.SelectOption(label="Nenhuma", value="none")])
    async def select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not interaction.guild or interaction.guild.id != self.discord_guild_id or not _admin(interaction):
            await interaction.response.send_message("Ação não autorizada.", ephemeral=True)
            return
        try:
            guild = self.results[int(select.values[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message("Seleção inválida.", ephemeral=True)
            return
        view = ConfirmGuildView(self.cog, self.discord_guild_id, guild)
        alliance = guild.get("AllianceName") or guild.get("Alliance") or "Não informada"
        await interaction.response.send_message(
            f"Confirme a guilda **{guild['Name']}**\nAliança: **{alliance}**\nGuild ID: `{guild['Id']}`",
            view=view, ephemeral=True
        )

class ConfirmGuildView(discord.ui.View):
    def __init__(self, cog, discord_guild_id: int, guild_data: dict):
        super().__init__(timeout=120)
        self.cog, self.discord_guild_id, self.guild_data = cog, discord_guild_id, guild_data

    @discord.ui.button(label="Confirmar guilda", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.guild.id != self.discord_guild_id or not _admin(interaction):
            await interaction.response.send_message("Ação não autorizada.", ephemeral=True)
            return
        cfg = await self.cog.repos.get_config(self.discord_guild_id) or ServerConfig(self.discord_guild_id)
        cfg.albion_guild_id = str(self.guild_data["Id"])
        cfg.albion_guild_name = str(self.guild_data["Name"])
        cfg.alliance_name = self.guild_data.get("AllianceName") or self.guild_data.get("Alliance")
        cfg.updated_at = datetime.now(timezone.utc)
        # Changing guild invalidates the old combat activation; require fresh validation.
        cfg.combat_enabled = False
        cfg.activated_at = None
        await self.cog.repos.save_config(cfg)
        await interaction.response.edit_message(content=f"Guilda salva: **{cfg.albion_guild_name}** (`{cfg.albion_guild_id}`). O tracker continua desativado.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Configuração cancelada.", view=None)

class CombatConfigModal(discord.ui.Modal, title="Configurar tracker de combate"):
    kills_channel = discord.ui.TextInput(label="ID do canal de kills (vazio = desabilitado)", required=False, max_length=25)
    deaths_channel = discord.ui.TextInput(label="ID do canal de mortes (vazio = desabilitado)", required=False, max_length=25)
    kills_enabled = discord.ui.TextInput(label="Notificar kills? (sim/não)", default="sim", max_length=3)
    deaths_enabled = discord.ui.TextInput(label="Notificar mortes? (sim/não)", default="sim", max_length=3)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("Somente administradores podem alterar esta configuração.", ephemeral=True)
            return
        cfg = await self.cog.repos.get_config(interaction.guild.id)
        if not cfg or not cfg.albion_guild_id:
            await interaction.response.send_message("Configure e confirme a guilda do Albion primeiro com `/configurar`.", ephemeral=True)
            return
        kill_on = str(self.kills_enabled).strip().lower() in {"sim", "s", "yes", "true"}
        death_on = str(self.deaths_enabled).strip().lower() in {"sim", "s", "yes", "true"}
        kill_id = str(self.kills_channel).strip()
        death_id = str(self.deaths_channel).strip()
        kill_channel = None
        death_channel = None
        try:
            if kill_on:
                kill_channel = interaction.guild.get_channel(int(kill_id))
                if not isinstance(kill_channel, discord.TextChannel):
                    raise ValueError("O canal de kills precisa ser um canal de texto válido.")
            if death_on:
                death_channel = interaction.guild.get_channel(int(death_id))
                if not isinstance(death_channel, discord.TextChannel):
                    raise ValueError("O canal de mortes precisa ser um canal de texto válido.")
            for channel in [c for c in (kill_channel, death_channel) if c]:
                perms = channel.permissions_for(interaction.guild.me)
                if not perms.view_channel or not perms.send_messages or not perms.embed_links:
                    raise ValueError(f"Permissões insuficientes em #{channel.name}: ver canal, enviar mensagens e incorporar links.")
            if not kill_on and not death_on:
                raise ValueError("Habilite kills, mortes ou ambos.")
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(f"Configuração inválida: {exc}", ephemeral=True)
            return
        cfg.kills_enabled = kill_on
        cfg.deaths_enabled = death_on
        cfg.kills_channel_id = int(kill_id) if kill_on else None
        cfg.deaths_channel_id = int(death_id) if death_on else None
        cfg.combat_enabled = True
        cfg.activated_at = datetime.now(timezone.utc)
        cfg.updated_at = datetime.now(timezone.utc)
        await self.cog.repos.save_config(cfg)
        await interaction.response.send_message("Tracker ativado com configuração validada. Eventos anteriores ao instante de ativação serão ignorados.", ephemeral=True)

class ConfigurationCog(commands.Cog):
    def __init__(self, bot, albion, repos, tracker):
        self.bot, self.albion, self.repos, self.tracker = bot, albion, repos, tracker

    @app_commands.command(name="configurar", description="Configura a guilda do Albion e o tracker deste servidor")
    @app_commands.guild_only()
    async def configurar(self, interaction: discord.Interaction):
        if not _admin(interaction):
            await interaction.response.send_message("Somente administradores deste servidor podem usar este comando.", ephemeral=True)
            return
        view = ConfigurationView(self)
        await interaction.response.send_message("**Configuração do Albion Bot**\nEscolha uma ação. O tracker começa desativado.", view=view, ephemeral=True)

class ConfigurationView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="Pesquisar/alterar guilda", style=discord.ButtonStyle.primary)
    async def guild(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _admin(interaction):
            await interaction.response.send_message("Ação não autorizada.", ephemeral=True)
            return
        await interaction.response.send_modal(GuildSearchModal(self.cog))

    @discord.ui.button(label="Configurar e ativar tracker", style=discord.ButtonStyle.success)
    async def combat(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _admin(interaction):
            await interaction.response.send_message("Ação não autorizada.", ephemeral=True)
            return
        await interaction.response.send_modal(CombatConfigModal(self.cog))
