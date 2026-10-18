import logging
import discord
from discord import app_commands
from discord.ext import commands
from app.integrations.albion.client import AlbionAPIError
from app.modules.common import OwnerView, reply
from app.modules.member_registration.service import (
    MAX_OPTIONS, PlayerOption, RegistrationError, active_link, apply_member_settings, confirm_in_guild, player_options, save_registration,
)

log = logging.getLogger(__name__)

async def _current_config(cog, interaction: discord.Interaction):
    cfg = await cog.repos.get_config(interaction.guild.id)
    if not cfg or not cfg.guild:
        await reply(interaction, "Este servidor ainda não cadastrou a guilda do Albion. Peça a um administrador para usar `/cadastrar`.")
        return None
    return cfg

async def _finish_registration(cog, interaction: discord.Interaction, cfg, player: PlayerOption, respond) -> None:
    """Grava o vínculo e aplica cargo/apelido; `respond` mostra o resultado privado (editando ou enviando)."""
    try:
        await save_registration(cog.repos, cfg, interaction.user.id, player)
    except RegistrationError as exc:
        await respond(str(exc))
        return
    lines = [f"✅ Você foi registrado como **{player.name}** na guilda **{cfg.guild.name}**."]
    result = await apply_member_settings(interaction.user, cfg, player.name)
    lines += result.done
    if result.problems:
        lines.append("O registro está válido, mas:")
        lines += [f"• {problem}" for problem in result.problems]
        lines.append("Avise um administrador do servidor.")
    await respond("\n".join(lines))

async def _process_choice(cog, interaction: discord.Interaction, option: PlayerOption, respond) -> None:
    cfg = await _current_config(cog, interaction)
    if cfg is None:
        return
    try:
        player = await confirm_in_guild(cog.albion, cfg, option)
    except RegistrationError as exc:
        await respond(str(exc))
        return
    current = active_link(await cog.repos.get_link(cfg.discord_guild_id, interaction.user.id), cfg)
    if current and current.player_id != player.player_id:
        view = ReplaceLinkView(cog, cfg.discord_guild_id, interaction.user.id, player)
        view.origin = interaction
        await respond(
            f"Você já está registrado como **{current.player_name}**. Cada membro pode ter um único personagem vinculado.\n"
            f"Substituir pelo personagem **{player.name}**?",
            view=view,
        )
        return
    await _finish_registration(cog, interaction, cfg, player, respond)

class CharacterModal(discord.ui.Modal, title="Registrar personagem do Albion"):
    name = discord.ui.TextInput(label="Nome do personagem no Albion", placeholder="Exatamente como aparece no jogo", min_length=2, max_length=32)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        # Reconhece a interação antes das consultas à API, que podem passar de 3 s.
        await interaction.response.defer(ephemeral=True, thinking=True)

        async def respond(content, **kwargs):
            await reply(interaction, content, **kwargs)

        term = str(self.name).strip()
        try:
            exact, options = player_options(await self.cog.albion.search(term), term)
        except AlbionAPIError:
            await respond("A API do Albion não respondeu. Nada foi alterado; tente novamente em instantes.")
            return
        if exact:
            await _process_choice(self.cog, interaction, exact, respond)
            return
        if not options:
            await respond(f"Nenhum personagem encontrado para **{term}**. Confira o nome exatamente como aparece no jogo.")
            return
        note = f"\nMostrando {MAX_OPTIONS} de {len(options)} resultados; digite o nome completo se o seu não aparecer." if len(options) > MAX_OPTIONS else ""
        view = PlayerSelectView(self.cog, interaction.guild.id, interaction.user.id, options[:MAX_OPTIONS])
        view.origin = interaction
        await respond(f"Encontrei mais de um personagem para **{term}**. Escolha o seu:{note}", view=view)

class PlayerSelectView(OwnerView):
    def __init__(self, cog, discord_guild_id: int, user_id: int, options: list[PlayerOption]):
        super().__init__(discord_guild_id, user_id)
        self.cog, self.options = cog, options
        self.choose.options = [
            discord.SelectOption(label=o.name[:100], description=f"Guilda: {o.guild_name or 'sem guilda'}"[:100], value=str(i))
            for i, o in enumerate(options)
        ]

    @discord.ui.select(placeholder="Escolha o seu personagem", min_values=1, max_values=1, options=[discord.SelectOption(label="-", value="-")])
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        option = self.options[int(select.values[0])]
        self.finish()
        await interaction.response.defer()

        async def respond(content, **kwargs):
            await interaction.edit_original_response(content=content, view=kwargs.get("view"))

        await _process_choice(self.cog, interaction, option, respond)

class ReplaceLinkView(OwnerView):
    def __init__(self, cog, discord_guild_id: int, user_id: int, player: PlayerOption):
        super().__init__(discord_guild_id, user_id)
        self.cog, self.player = cog, player

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        await interaction.response.edit_message(content="Registro cancelado. Seu vínculo atual foi mantido.", view=None)

    @discord.ui.button(label="Substituir", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        await interaction.response.defer()
        cfg = await _current_config(self.cog, interaction)
        if cfg is None:
            return

        async def respond(content, **kwargs):
            await interaction.edit_original_response(content=content, view=None)

        await _finish_registration(self.cog, interaction, cfg, self.player, respond)

class MemberRegistrationCog(commands.Cog):
    def __init__(self, bot, albion, repos):
        self.bot, self.albion, self.repos = bot, albion, repos

    @app_commands.command(name="registrar", description="Vincula o seu personagem do Albion Online à sua conta Discord")
    @app_commands.guild_only()
    async def registrar(self, interaction: discord.Interaction):
        if await _current_config(self, interaction) is None:
            return
        await interaction.response.send_modal(CharacterModal(self))
