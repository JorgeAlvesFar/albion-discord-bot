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

async def _log_saved_config(repos, discord_guild_id: int, action: str, user) -> None:
    saved = await repos.get_config(discord_guild_id)
    if not saved:
        log.error("Configuração não encontrada após salvar (%s) DiscordGuildId=%s.", action, discord_guild_id)
        return
    log.info(
        "Configuração salva (%s) por %s (%s): DiscordGuildId=%s AlbionGuild=%s (%s) Aliança=%s "
        "tracker_ativo=%s config_válida=%s kills=%s canal_kills=%s mortes=%s canal_mortes=%s ativado_em=%s",
        action, user, user.id, saved.discord_guild_id, saved.albion_guild_name, saved.albion_guild_id, saved.alliance_name,
        saved.combat_enabled, saved.combat_config_valid, saved.kills_enabled, saved.kills_channel_id,
        saved.deaths_enabled, saved.deaths_channel_id, saved.activated_at.isoformat() if saved.activated_at else None,
    )

def _tracker_config_embed(cfg: ServerConfig, user) -> discord.Embed:
    embed = discord.Embed(title="✅ Tracker de combate configurado", color=discord.Color.green(), timestamp=cfg.activated_at)
    embed.add_field(name="Guilda Albion", value=cfg.albion_guild_name or "Não informada", inline=True)
    embed.add_field(name="Aliança", value=cfg.alliance_name or "Sem aliança", inline=True)
    embed.add_field(name="Guild ID", value=f"`{cfg.albion_guild_id}`", inline=True)
    embed.add_field(name="Canal de kills", value=f"<#{cfg.kills_channel_id}>" if cfg.kills_channel_id else "Desabilitado", inline=True)
    embed.add_field(name="Canal de mortes", value=f"<#{cfg.deaths_channel_id}>" if cfg.deaths_channel_id else "Desabilitado", inline=True)
    embed.add_field(name="Configurado por", value=user.mention, inline=True)
    warnings = []
    if not cfg.kills_channel_id:
        warnings.append("Kills não serão publicadas (nenhum canal escolhido).")
    if not cfg.deaths_channel_id:
        warnings.append("Mortes não serão publicadas (nenhum canal escolhido).")
    warnings.append("A configuração fica em memória e é perdida se o bot reiniciar.")
    embed.add_field(name="Avisos", value="\n".join(warnings), inline=False)
    embed.set_footer(text="Serão publicados os eventos que aparecerem a partir de agora.")
    return embed

class GuildSearchModal(discord.ui.Modal, title="Pesquisar guilda do Albion"):
    name = discord.ui.TextInput(label="Nome da guilda", placeholder="Digite o nome para pesquisar", max_length=100)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("Somente administradores deste servidor podem configurar o bot.", ephemeral=True)
            return
        # O Discord invalida a interação se não houver resposta em 3s; a API do Albion pode demorar mais.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.cog.albion.search(str(self.name).strip())
        except AlbionAPIError:
            await interaction.followup.send("A API do Albion falhou. A configuração não foi alterada; tente novamente mais tarde.", ephemeral=True)
            return
        guilds = result.get("guilds", [])
        if not guilds:
            await interaction.followup.send("Nenhuma guilda encontrada. Confira o nome e tente novamente.", ephemeral=True)
            return
        view = GuildResultsView(self.cog, interaction.guild.id, guilds[:25])
        await interaction.followup.send("Selecione a guilda correta. Confira o nome e o ID antes de confirmar.", view=view, ephemeral=True)

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
        await _log_saved_config(self.cog.repos, self.discord_guild_id, "guilda", interaction.user)
        await interaction.response.edit_message(content=f"Guilda salva: **{cfg.albion_guild_name}** (`{cfg.albion_guild_id}`). O tracker continua desativado.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Configuração cancelada.", view=None)

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]

class CombatConfigView(discord.ui.View):
    def __init__(self, cog, discord_guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.discord_guild_id = discord_guild_id
        self.kills_channel_id: int | None = None
        self.deaths_channel_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.discord_guild_id or not _admin(interaction):
            await interaction.response.send_message("Ação não autorizada.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=TEXT_CHANNEL_TYPES, placeholder="Canal de kills", min_values=0, max_values=1, row=0)
    async def kills_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.kills_channel_id = select.values[0].id if select.values else None
        await interaction.response.defer()

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=TEXT_CHANNEL_TYPES, placeholder="Canal de mortes", min_values=0, max_values=1, row=1)
    async def deaths_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.deaths_channel_id = select.values[0].id if select.values else None
        await interaction.response.defer()

    @discord.ui.button(label="Ativar tracker", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        if not cfg or not cfg.albion_guild_id:
            await interaction.response.send_message("Configure e confirme a guilda do Albion primeiro com `/configurar`.", ephemeral=True)
            return
        if self.kills_channel_id is None and self.deaths_channel_id is None:
            await interaction.response.send_message("Escolha pelo menos um canal: kills, mortes ou ambos.", ephemeral=True)
            return
        for kind, channel_id in (("kills", self.kills_channel_id), ("mortes", self.deaths_channel_id)):
            if channel_id is None:
                continue
            channel = interaction.guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(f"O canal de {kind} não está mais disponível. Escolha outro.", ephemeral=True)
                return
            perms = channel.permissions_for(interaction.guild.me)
            if not perms.view_channel or not perms.send_messages or not perms.embed_links:
                await interaction.response.send_message(f"Permissões insuficientes em {channel.mention}: ver canal, enviar mensagens e incorporar links.", ephemeral=True)
                return
        cfg.kills_enabled = self.kills_channel_id is not None
        cfg.deaths_enabled = self.deaths_channel_id is not None
        cfg.kills_channel_id = self.kills_channel_id
        cfg.deaths_channel_id = self.deaths_channel_id
        cfg.combat_enabled = True
        cfg.activated_at = datetime.now(timezone.utc)
        cfg.updated_at = datetime.now(timezone.utc)
        await self.cog.repos.save_config(cfg)
        await _log_saved_config(self.cog.repos, self.discord_guild_id, "tracker", interaction.user)
        await interaction.response.edit_message(content="Tracker ativado. A configuração foi publicada neste canal.", view=None)
        try:
            await interaction.channel.send(embed=_tracker_config_embed(cfg, interaction.user))
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao publicar a configuração do tracker no canal %s.", interaction.channel_id)
            await interaction.followup.send(
                "O tracker foi ativado, mas não consegui publicar a configuração neste canal. Verifique se o bot pode enviar mensagens e incorporar links aqui.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Configuração do tracker cancelada.", view=None)

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
        cfg = await self.cog.repos.get_config(interaction.guild.id)
        if not cfg or not cfg.albion_guild_id:
            await interaction.response.send_message("Configure e confirme a guilda do Albion primeiro em **Pesquisar/alterar guilda**.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Tracker de combate — {cfg.albion_guild_name}**\nEscolha onde as kills e as mortes serão publicadas.",
            view=CombatConfigView(self.cog, interaction.guild.id),
            ephemeral=True,
        )
