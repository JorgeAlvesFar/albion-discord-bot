"""Regras compartilhadas pelos comandos administrativos: permissão, painéis privados e resumo público."""
import logging
import discord

log = logging.getLogger(__name__)

def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and getattr(interaction.user, "guild_permissions", None) and interaction.user.guild_permissions.administrator)

async def reply(interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
    """Responde de forma privada, usando followup se a interação já foi confirmada (defer ou resposta anterior)."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)

async def require_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True
    await reply(interaction, "Somente administradores deste servidor podem usar esta configuração.")
    return False

class AdminView(discord.ui.View):
    """Painel privado de um administrador, preso ao servidor onde foi aberto.

    Ao expirar, o painel avisa que expirou em vez de deixar botões que falhariam com "interação falhou".
    """

    def __init__(self, discord_guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.discord_guild_id = discord_guild_id
        self.origin: discord.Interaction | None = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.discord_guild_id:
            await reply(interaction, "Este painel pertence a outro servidor.")
            return False
        if self.finished:
            await reply(interaction, "Esta operação já foi concluída. Use o comando de novo para outra alteração.")
            return False
        return await require_admin(interaction)

    def finish(self) -> None:
        """Marca a operação como concluída: cliques repetidos não executam a ação de novo."""
        self.finished = True
        self.stop()

    async def on_timeout(self) -> None:
        if self.origin is None or self.finished:
            return
        try:
            await self.origin.edit_original_response(content="Este painel expirou. Use o comando de novo.", embed=None, view=None)
        except discord.HTTPException:
            pass

class OwnerView(discord.ui.View):
    """Painel privado de um membro: só quem o abriu, no mesmo servidor, pode usá-lo."""

    def __init__(self, discord_guild_id: int, user_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.discord_guild_id, self.user_id = discord_guild_id, user_id
        self.origin: discord.Interaction | None = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.discord_guild_id or interaction.user.id != self.user_id:
            await reply(interaction, "Este painel pertence a outra pessoa.")
            return False
        if self.finished:
            await reply(interaction, "Esta operação já foi concluída. Use o comando de novo se precisar.")
            return False
        return True

    def finish(self) -> None:
        self.finished = True
        self.stop()

    async def on_timeout(self) -> None:
        if self.origin is None or self.finished:
            return
        try:
            await self.origin.edit_original_response(content="Este painel expirou. Use o comando de novo.", embed=None, view=None)
        except discord.HTTPException:
            pass

async def publish_summary(interaction: discord.Interaction, embed: discord.Embed) -> bool:
    """Publica o resumo de uma operação concluída no canal do comando; se não conseguir, avisa em privado."""
    try:
        await interaction.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        log.exception("Falha ao publicar o resumo no canal %s.", getattr(interaction, "channel_id", None))
        await reply(
            interaction,
            "A operação foi concluída, mas não consegui publicar o resumo neste canal. "
            "Verifique se o bot pode ver o canal, enviar mensagens e incorporar links.",
        )
        return False

def summary_embed(module: str, operation: str, author: discord.abc.User, color: discord.Color = discord.Color.green()) -> discord.Embed:
    embed = discord.Embed(title=f"{module}: {operation}", color=color)
    embed.add_field(name="Realizado por", value=author.mention, inline=False)
    return embed
