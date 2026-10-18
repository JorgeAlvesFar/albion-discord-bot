import logging
import discord
from discord import app_commands
from discord.ext import commands
from app.modules.combat_tracker.killboard import LABELS, reset_killboard, save_killboard, validate_settings
from app.modules.common import AdminView, publish_summary, reply, require_admin, summary_embed
from app.repositories.models import KINDS, KillboardSettings

log = logging.getLogger(__name__)

MODULE = "Killboard"
TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]

def _kind_text(settings: KillboardSettings, kind: str) -> str:
    channel = settings.channel(kind)
    channel_text = f"<#{channel}>" if channel else "sem canal"
    return f"Ligado → {channel_text}" if settings.enabled(kind) else f"Desligado ({channel_text})"

def _state_text(cfg) -> str:
    if cfg.killboard_active:
        return f"Ativo desde <t:{int(cfg.killboard.activated_at.timestamp())}:f>"
    return "Desativado"

def _panel_embed(cfg, draft: KillboardSettings, saved: KillboardSettings) -> discord.Embed:
    embed = discord.Embed(title=f"Killboard — {cfg.guild.name}", color=discord.Color.blurple())
    embed.description = (
        "Escolha os canais, ligue ou desligue cada tipo e clique em **Salvar**. Kills e mortes podem usar o mesmo canal.\n"
        "Ao salvar com algum tipo ligado, só são publicados combates a partir daquele momento."
    )
    embed.add_field(name="Estado atual", value=_state_text(cfg), inline=False)
    embed.add_field(name="Kills", value=_kind_text(draft, "kill"), inline=True)
    embed.add_field(name="Mortes", value=_kind_text(draft, "death"), inline=True)
    if draft != saved:
        embed.set_footer(text="Há alterações não salvas. " + ("Ao salvar, o killboard fica ativo." if draft.publishes_something else "Ao salvar, o killboard fica desativado."))
    return embed

def _summary(interaction, operation: str, cfg) -> discord.Embed:
    active = cfg.killboard_active
    embed = summary_embed(MODULE, operation, interaction.user, discord.Color.green() if active else discord.Color.orange())
    embed.add_field(name="Guilda", value=f"{cfg.guild.name} (`{cfg.guild.guild_id}`)" if cfg.guild else "Não cadastrada", inline=False)
    embed.add_field(name="Estado", value=_state_text(cfg), inline=False)
    settings = cfg.killboard.settings
    embed.add_field(name="Kills", value=_kind_text(settings, "kill"), inline=True)
    embed.add_field(name="Mortes", value=_kind_text(settings, "death"), inline=True)
    missing = [LABELS[kind] for kind in KINDS if not settings.channel_for(kind)]
    embed.add_field(name="Sem configuração", value=", ".join(missing) or "Nada", inline=False)
    if active:
        embed.set_footer(text="Só são publicados combates a partir da ativação. A configuração fica em memória e é perdida se o bot reiniciar.")
    return embed

class KillboardPanelView(AdminView):
    def __init__(self, cog, cfg):
        super().__init__(cfg.discord_guild_id)
        self.cog = cog
        self.saved = self.draft = cfg.killboard.settings
        for select, kind in ((self.kills_channel, "kill"), (self.deaths_channel, "death")):
            if self.draft.channel(kind):
                select.default_values = [discord.Object(id=self.draft.channel(kind))]
        self.reset.disabled = self.saved.empty and cfg.killboard.activated_at is None
        self._refresh_toggles()

    def _refresh_toggles(self) -> None:
        for button, kind, label in ((self.toggle_kills, "kill", "Kills"), (self.toggle_deaths, "death", "Mortes")):
            enabled = self.draft.enabled(kind)
            button.label = f"{label}: {'ligado' if enabled else 'desligado'}"
            button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    async def _rerender(self, interaction: discord.Interaction) -> None:
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        self._refresh_toggles()
        await interaction.response.edit_message(embed=_panel_embed(cfg, self.draft, self.saved), view=self)

    async def _choose_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect, kind: str) -> None:
        channel_id = select.values[0].id if select.values else None
        self.draft = self.draft.with_channel(kind, channel_id)
        if channel_id and not self.saved.channel(kind) and not self.draft.enabled(kind):
            self.draft = self.draft.toggled(kind)  # escolher o canal pela primeira vez já liga o tipo
        await self._rerender(interaction)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=TEXT_CHANNEL_TYPES, placeholder="Canal de kills", min_values=0, max_values=1, row=0)
    async def kills_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._choose_channel(interaction, select, "kill")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=TEXT_CHANNEL_TYPES, placeholder="Canal de mortes", min_values=0, max_values=1, row=1)
    async def deaths_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._choose_channel(interaction, select, "death")

    @discord.ui.button(label="Redefinir", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ConfirmKillboardResetView(self.cog, self.discord_guild_id)
        view.origin = interaction
        self.finish()
        await interaction.response.edit_message(
            content=(
                "**Redefinir o killboard?**\n"
                "As notificações de kills e mortes serão **desativadas**, os canais escolhidos serão apagados e as notificações "
                "ainda não publicadas serão descartadas. A guilda e a configuração de membros não mudam."
            ),
            embed=None, view=view,
        )

    @discord.ui.button(label="Kills", row=2)
    async def toggle_kills(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft = self.draft.toggled("kill")
        await self._rerender(interaction)

    @discord.ui.button(label="Mortes", row=2)
    async def toggle_deaths(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft = self.draft.toggled("death")
        await self._rerender(interaction)

    @discord.ui.button(label="Salvar", style=discord.ButtonStyle.primary, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        if not cfg or not cfg.guild:
            self.finish()
            await interaction.response.edit_message(content="A guilda foi removida. Cadastre-a de novo com `/cadastrar`.", embed=None, view=None)
            return
        errors = validate_settings(self.draft, interaction.guild)
        if errors:
            await reply(interaction, "Não foi possível salvar:\n" + "\n".join(f"• {error}" for error in errors))
            return
        self.finish()
        result = await save_killboard(self.cog.repos, self.cog.tracker, self.discord_guild_id, self.draft)
        if result.operation is None:
            await interaction.response.edit_message(content="Nada mudou: o killboard já estava assim.", embed=None, view=None)
            return
        state = "ativo" if result.config.killboard_active else "desativado"
        await interaction.response.edit_message(content=f"Killboard salvo ({state}). O resumo foi publicado neste canal.", embed=None, view=None)
        await publish_summary(interaction, _summary(interaction, result.operation, result.config))

class ConfirmKillboardResetView(AdminView):
    def __init__(self, cog, discord_guild_id: int):
        super().__init__(discord_guild_id)
        self.cog = cog

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        await interaction.response.edit_message(content="Redefinição cancelada. Nada foi alterado.", view=None)

    @discord.ui.button(label="Confirmar redefinição", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        result = await reset_killboard(self.cog.repos, self.cog.tracker, self.discord_guild_id)
        if result.operation is None:
            await interaction.response.edit_message(content="O killboard já estava sem configuração.", view=None)
            return
        await interaction.response.edit_message(content="Killboard redefinido. O resumo foi publicado neste canal.", view=None)
        await publish_summary(interaction, _summary(interaction, "redefinido", result.config))

class CombatCog(commands.Cog):
    def __init__(self, bot, repos, tracker):
        self.bot, self.repos, self.tracker = bot, repos, tracker

    @app_commands.command(name="killboard", description="Configura as notificações de kills e mortes da guilda")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def killboard(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        cfg = await self.repos.get_config(interaction.guild.id)
        if not cfg or not cfg.guild:
            await reply(interaction, "Cadastre a guilda do Albion primeiro com `/cadastrar`.")
            return
        view = KillboardPanelView(self, cfg)
        view.origin = interaction
        await interaction.response.send_message(embed=_panel_embed(cfg, view.draft, view.saved), view=view, ephemeral=True)
