import logging
import discord
from discord import app_commands
from discord.ext import commands
from app.integrations.albion.client import AlbionAPIError
from app.modules.common import AdminView, publish_summary, reply, require_admin, summary_embed
from app.modules.guild_registration.service import MAX_OPTIONS, guild_options, link_guild, reset_guild, save_audit, validate_audit
from app.repositories.models import AuditConfig, GuildLink

log = logging.getLogger(__name__)

MODULE = "Cadastro da guilda"
TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]

def _alliance(name: str | None) -> str:
    return name or "Sem aliança informada"

def _hour_options(selected: int) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=f"{hour:02d}:00", value=str(hour), default=hour == selected) for hour in range(24)]

def _channel_text(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "Não configurado (nada é publicado)"

def _add_guild_fields(embed: discord.Embed, guild: GuildLink) -> None:
    embed.add_field(name="Guilda", value=guild.name, inline=True)
    embed.add_field(name="Aliança", value=_alliance(guild.alliance_name), inline=True)
    embed.add_field(name="Guild ID", value=f"`{guild.guild_id}`", inline=False)

def _add_audit_fields(embed: discord.Embed, audit: AuditConfig) -> None:
    embed.add_field(name="Canal da verificação", value=_channel_text(audit.channel_id), inline=True)
    embed.add_field(name="Relatório diário", value=f"{audit.report_time_text} (Brasília)", inline=True)

def _status_embed(cfg, draft: AuditConfig | None = None) -> discord.Embed:
    audit = draft if draft is not None else (cfg.audit if cfg else AuditConfig())
    embed = discord.Embed(title="Cadastro da guilda do Albion", color=discord.Color.blurple())
    if cfg and cfg.guild:
        _add_guild_fields(embed, cfg.guild)
        embed.add_field(name="Killboard", value="Ativo" if cfg.killboard_active else "Desativado", inline=True)
    else:
        embed.description = "Nenhuma guilda cadastrada neste servidor. Pesquise pelo nome da guilda para cadastrar."
    _add_audit_fields(embed, audit)
    if draft is not None and cfg and draft != cfg.audit:
        embed.set_footer(text="Há alterações não salvas na verificação de membros.")
    else:
        embed.set_footer(
            text="Pesquise para trocar a guilda. Escolha o canal e o horário da verificação e clique em Salvar verificação. "
            "Trocar ou redefinir a guilda desativa o killboard; redefinir também apaga o canal da verificação."
        )
    return embed

def _result_summary(interaction: discord.Interaction, operation: str, cfg, result) -> discord.Embed:
    embed = summary_embed(MODULE, operation, interaction.user)
    if cfg.guild:
        _add_guild_fields(embed, cfg.guild)
    if result.previous and (cfg.guild is None or result.previous.guild_id != cfg.guild.guild_id):
        embed.add_field(name="Guilda anterior", value=f"{result.previous.name} (`{result.previous.guild_id}`)", inline=False)
    killboard = "Desativado"
    if result.previous:
        killboard += " e redefinido" + (" (estava ativo)" if result.killboard_was_active else "")
    embed.add_field(name="Killboard", value=killboard, inline=False)
    _add_audit_fields(embed, cfg.audit)
    missing = [] if cfg.guild else ["guilda do Albion (use /cadastrar)"]
    if not cfg.killboard_active:
        missing.append("killboard (use /killboard)")
    if not cfg.audit.channel_id:
        missing.append("canal da verificação de membros")
    embed.add_field(name="Sem configuração", value="\n".join(missing) or "Nada", inline=False)
    return embed

def _audit_summary(interaction: discord.Interaction, cfg) -> discord.Embed:
    embed = summary_embed(MODULE, "verificação de membros configurada", interaction.user)
    if cfg.guild:
        _add_guild_fields(embed, cfg.guild)
    _add_audit_fields(embed, cfg.audit)
    if not cfg.audit.channel_id:
        embed.add_field(
            name="Sem configuração",
            value="canal da verificação (remoções e relatório diário não são publicados)",
            inline=False,
        )
    embed.set_footer(
        text="O relatório sai uma vez ao dia no horário de Brasília. Sem canal, as remoções continuam acontecendo, mas nada é publicado."
    )
    return embed

class GuildPanelView(AdminView):
    def __init__(self, cog, discord_guild_id: int, has_guild: bool, audit: AuditConfig | None = None):
        super().__init__(discord_guild_id)
        self.cog = cog
        self.draft = audit or AuditConfig()
        self.reset.disabled = not has_guild
        if self.draft.channel_id:
            self.log_channel.default_values = [discord.Object(id=self.draft.channel_id)]
        self.report_hour.options = _hour_options(self.draft.report_hour)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        await interaction.response.edit_message(embed=_status_embed(cfg, self.draft), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=TEXT_CHANNEL_TYPES,
        placeholder="Canal da verificação de membros (opcional)", min_values=0, max_values=1, row=0,
    )
    async def log_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.draft = AuditConfig(select.values[0].id if select.values else None, self.draft.report_hour, self.draft.report_minute)
        await self._rerender(interaction)

    @discord.ui.select(
        placeholder="Horário do relatório diário (Brasília)", min_values=1, max_values=1,
        options=_hour_options(9), row=1,
    )
    async def report_hour(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.draft = AuditConfig(self.draft.channel_id, int(select.values[0]), self.draft.report_minute)
        self.report_hour.options = _hour_options(self.draft.report_hour)
        await self._rerender(interaction)

    @discord.ui.button(label="Redefinir", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        if not cfg or not cfg.guild:
            await interaction.response.edit_message(content="Não há guilda cadastrada para redefinir.", embed=None, view=None)
            return
        view = ConfirmResetView(self.cog, self.discord_guild_id)
        view.origin = interaction
        self.finish()
        await interaction.response.edit_message(
            content=(
                f"**Redefinir o cadastro da guilda?**\n"
                f"Será removida a associação com **{cfg.guild.name}** (`{cfg.guild.guild_id}`).\n"
                f"O killboard depende da guilda: ele será **desativado e sua configuração apagada**.\n"
                f"O canal e o horário da verificação de membros também serão apagados."
            ),
            embed=None, view=view,
        )

    @discord.ui.button(label="Pesquisar guilda", style=discord.ButtonStyle.primary, row=2)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GuildSearchModal(self.cog, self.discord_guild_id))

    @discord.ui.button(label="Salvar verificação", style=discord.ButtonStyle.success, row=2)
    async def save_audit_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        errors = validate_audit(self.draft, interaction.guild)
        if errors:
            await reply(interaction, "Não foi possível salvar:\n" + "\n".join(f"• {error}" for error in errors))
            return
        changed, cfg = await save_audit(self.cog.repos, self.discord_guild_id, self.draft)
        self.finish()
        if not changed:
            await interaction.response.edit_message(content="Nada mudou: a verificação de membros já estava assim.", embed=None, view=None)
            return
        await interaction.response.edit_message(
            content="Verificação de membros configurada. O resumo foi publicado neste canal.", embed=None, view=None,
        )
        await publish_summary(interaction, _audit_summary(interaction, cfg))

class GuildSearchModal(discord.ui.Modal, title="Pesquisar guilda do Albion"):
    name = discord.ui.TextInput(label="Nome da guilda", placeholder="Digite o nome para pesquisar", min_length=2, max_length=100)

    def __init__(self, cog, discord_guild_id: int):
        super().__init__()
        self.cog, self.discord_guild_id = cog, discord_guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        # O Discord invalida a interação sem resposta em 3 s; a API do Albion pode demorar mais.
        await interaction.response.defer(ephemeral=True, thinking=True)
        term = str(self.name).strip()
        try:
            options = guild_options(await self.cog.albion.search(term))
        except AlbionAPIError:
            await reply(interaction, "A API do Albion não respondeu. Nada foi alterado; tente novamente em instantes.")
            return
        if not options:
            await reply(interaction, f"Nenhuma guilda encontrada para **{term}**. Confira o nome e tente novamente.")
            return
        note = f"\nMostrando {MAX_OPTIONS} de {len(options)} resultados; refine o nome se a guilda não aparecer." if len(options) > MAX_OPTIONS else ""
        view = GuildResultsView(self.cog, self.discord_guild_id, options[:MAX_OPTIONS])
        view.origin = interaction
        await reply(interaction, f"Selecione a guilda correta. Confira o nome, a aliança e o ID.{note}", view=view)

class GuildResultsView(AdminView):
    def __init__(self, cog, discord_guild_id: int, options: list[GuildLink]):
        super().__init__(discord_guild_id)
        self.cog, self.options = cog, options
        self.choose.options = [
            discord.SelectOption(label=o.name[:100], description=f"ID: {o.guild_id} | {_alliance(o.alliance_name)}"[:100], value=str(i))
            for i, o in enumerate(options)
        ]

    @discord.ui.select(placeholder="Escolha a guilda", min_values=1, max_values=1, options=[discord.SelectOption(label="-", value="-")])
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        option = self.options[int(select.values[0])]
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        warning = ""
        if cfg and cfg.guild and cfg.guild.guild_id != option.guild_id:
            warning = (
                f"\n\n⚠️ A guilda atual **{cfg.guild.name}** (`{cfg.guild.guild_id}`) será substituída. "
                "O killboard será **desativado e sua configuração apagada**; os vínculos de personagens não são transferidos."
            )
        view = ConfirmGuildView(self.cog, self.discord_guild_id, option)
        view.origin = interaction
        self.finish()
        await interaction.response.edit_message(
            content=f"Confirme a guilda **{option.name}**\nAliança: **{_alliance(option.alliance_name)}**\nGuild ID: `{option.guild_id}`{warning}",
            view=view,
        )

class ConfirmGuildView(AdminView):
    def __init__(self, cog, discord_guild_id: int, option: GuildLink):
        super().__init__(discord_guild_id)
        self.cog, self.option = cog, option

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        await interaction.response.edit_message(content="Cadastro cancelado. Nada foi alterado.", view=None)

    @discord.ui.button(label="Confirmar guilda", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        result = await link_guild(self.cog.repos, self.cog.tracker, self.discord_guild_id, self.option)
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        if not result.changed:
            await interaction.response.edit_message(content=f"**{self.option.name}** já era a guilda cadastrada. Nada mudou.", view=None)
            return
        await interaction.response.edit_message(content=f"Guilda **{cfg.guild.name}** cadastrada. O resumo foi publicado neste canal.", view=None)
        await publish_summary(interaction, _result_summary(interaction, "guilda alterada" if result.previous else "guilda cadastrada", cfg, result))

class ConfirmResetView(AdminView):
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
        result = await reset_guild(self.cog.repos, self.cog.tracker, self.discord_guild_id)
        if result is None:
            await interaction.response.edit_message(content="Não há guilda cadastrada para redefinir.", view=None)
            return
        cfg = await self.cog.repos.get_config(self.discord_guild_id)
        await interaction.response.edit_message(content="Cadastro da guilda redefinido. O resumo foi publicado neste canal.", view=None)
        await publish_summary(interaction, _result_summary(interaction, "cadastro redefinido", cfg, result))

class GuildRegistrationCog(commands.Cog):
    def __init__(self, bot, albion, repos, tracker):
        self.bot, self.albion, self.repos, self.tracker = bot, albion, repos, tracker

    @app_commands.command(name="cadastrar", description="Associa este servidor a uma guilda do Albion Online")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def cadastrar(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        cfg = await self.repos.get_config(interaction.guild.id)
        audit = cfg.audit if cfg else AuditConfig()
        view = GuildPanelView(self, interaction.guild.id, bool(cfg and cfg.guild), audit)
        view.origin = interaction
        await interaction.response.send_message(embed=_status_embed(cfg, view.draft), view=view, ephemeral=True)
