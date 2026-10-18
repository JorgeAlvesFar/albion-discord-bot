import logging
import discord
from discord import app_commands
from discord.ext import commands
from app.modules.common import AdminView, publish_summary, reply, require_admin, summary_embed
from app.modules.members_config.service import (
    TAG_MAX, format_nickname, normalize_tag, reset_members, save_members, validate_role, validate_tag_permission,
)
from app.repositories.models import MembersConfig

log = logging.getLogger(__name__)

MODULE = "Registro de membros"
EXAMPLE_NAME = "NomeDoPersonagem"

def _role_text(role_id: int | None) -> str:
    return f"<@&{role_id}>" if role_id else "Não configurado (nenhum cargo é atribuído)"

def _tag_text(tag: str | None) -> str:
    return f"`[{tag}]`" if tag else "Sem tag (o apelido fica só com o nome do personagem)"

def _missing(role_id, tag) -> str:
    missing = [name for name, value in (("cargo", role_id), ("tag", tag)) if not value]
    return ", ".join(missing) or "Nada"

def _panel_embed(role_id, tag, saved_role_id, saved_tag) -> discord.Embed:
    embed = discord.Embed(title="Configuração do registro de membros", color=discord.Color.blurple())
    embed.description = (
        "Opções aplicadas a quem se registrar com `/registrar`. Ambas são opcionais.\n"
        "Escolha o cargo no menu, edite a tag e clique em **Salvar**."
    )
    embed.add_field(name="Cargo", value=_role_text(role_id), inline=False)
    embed.add_field(name="Tag no apelido", value=_tag_text(tag), inline=False)
    embed.add_field(name="Exemplo de apelido", value=f"`{format_nickname(tag, EXAMPLE_NAME)}`", inline=False)
    if (role_id, tag) != (saved_role_id, saved_tag):
        embed.set_footer(text="Há alterações não salvas.")
    return embed

def _summary(interaction, operation, cfg) -> discord.Embed:
    embed = summary_embed(MODULE, operation, interaction.user)
    members = cfg.members
    embed.add_field(name="Cargo", value=_role_text(members.role_id), inline=False)
    embed.add_field(name="Tag no apelido", value=_tag_text(members.tag), inline=False)
    embed.add_field(name="Exemplo de apelido", value=f"`{format_nickname(members.tag, EXAMPLE_NAME)}`", inline=False)
    embed.add_field(name="Sem configuração", value=_missing(members.role_id, members.tag), inline=False)
    embed.set_footer(text="Vale para os próximos registros. Cargos e apelidos já aplicados não são alterados.")
    return embed

class MembersPanelView(AdminView):
    def __init__(self, cog, discord_guild_id: int, saved_role_id: int | None, saved_tag: str | None):
        super().__init__(discord_guild_id)
        self.cog = cog
        self.saved_role_id, self.saved_tag = saved_role_id, saved_tag
        self.role_id, self.tag = saved_role_id, saved_tag
        if saved_role_id:
            self.role_select.default_values = [discord.Object(id=saved_role_id)]
        self.reset.disabled = saved_role_id is None and saved_tag is None

    def embed(self) -> discord.Embed:
        return _panel_embed(self.role_id, self.tag, self.saved_role_id, self.saved_tag)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Cargo dos membros registrados (opcional)", min_values=0, max_values=1, row=0)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.role_id = select.values[0].id if select.values else None
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Redefinir", style=discord.ButtonStyle.danger, row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ConfirmMembersResetView(self.cog, self.discord_guild_id)
        view.origin = interaction
        self.finish()
        await interaction.response.edit_message(
            content=(
                "**Redefinir a configuração do registro de membros?**\n"
                f"Serão removidos o cargo ({_role_text(self.saved_role_id)}) e a tag ({_tag_text(self.saved_tag)}) desta configuração.\n"
                "Cargos e apelidos já aplicados aos membros não são alterados. A guilda e o killboard não mudam."
            ),
            embed=None, view=view,
        )

    @discord.ui.button(label="Editar tag", style=discord.ButtonStyle.secondary, row=1)
    async def edit_tag(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TagModal(self))

    @discord.ui.button(label="Salvar", style=discord.ButtonStyle.success, row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        me = interaction.guild.me
        if self.role_id is not None:
            role = interaction.guild.get_role(self.role_id)
            if role is None:
                await reply(interaction, "O cargo escolhido não existe mais. Escolha outro.")
                return
            error = validate_role(role, me)
            if error:
                await reply(interaction, error)
                return
        error = validate_tag_permission(self.tag, me)
        if error:
            await reply(interaction, error)
            return
        self.finish()
        changed, cfg = await save_members(self.cog.repos, self.discord_guild_id, MembersConfig(self.role_id, self.tag))
        if not changed:
            await interaction.response.edit_message(content="Nada mudou: a configuração já estava assim.", embed=None, view=None)
            return
        await interaction.response.edit_message(content="Configuração do registro de membros salva. O resumo foi publicado neste canal.", embed=None, view=None)
        await publish_summary(interaction, _summary(interaction, "configuração salva", cfg))

class TagModal(discord.ui.Modal, title="Tag no apelido"):
    tag = discord.ui.TextInput(
        label=f"Tag (opcional, até {TAG_MAX} caracteres)", placeholder="Ex.: TLT  →  [TLT] NomeDoPersonagem",
        required=False, max_length=TAG_MAX + 2,
    )

    def __init__(self, panel: MembersPanelView):
        super().__init__()
        self.panel = panel
        self.tag.default = panel.tag or ""

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.panel.interaction_check(interaction):
            return
        tag, error = normalize_tag(str(self.tag))
        if error:
            await reply(interaction, error)
            return
        self.panel.tag = tag
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)

class ConfirmMembersResetView(AdminView):
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
        changed, cfg = await reset_members(self.cog.repos, self.discord_guild_id)
        if not changed:
            await interaction.response.edit_message(content="Não havia configuração de membros para redefinir.", view=None)
            return
        await interaction.response.edit_message(content="Configuração do registro de membros redefinida. O resumo foi publicado neste canal.", view=None)
        await publish_summary(interaction, _summary(interaction, "configuração redefinida", cfg))

class MembersConfigCog(commands.Cog):
    def __init__(self, bot, repos):
        self.bot, self.repos = bot, repos

    @app_commands.command(name="registrar-membros", description="Configura o cargo e a tag no apelido dos membros registrados")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def registrar_membros(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        cfg = await self.repos.get_config(interaction.guild.id)
        members = cfg.members if cfg else MembersConfig()
        view = MembersPanelView(self, interaction.guild.id, members.role_id, members.tag)
        view.origin = interaction
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
