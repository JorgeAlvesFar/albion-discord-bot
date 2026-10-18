import pytest
from app.integrations.albion.client import AlbionAPIError
from app.modules.member_registration.cog import CharacterModal, MemberRegistrationCog, PlayerSelectView, ReplaceLinkView
from app.modules.member_registration.service import (
    MemberSync, PlayerOption, RegistrationError, apply_member_settings, confirm_in_guild, player_options, save_registration,
)
from app.repositories.memory import MemoryRepositories
from app.repositories.models import GuildLink, MembersConfig, ServerConfig
from tests.fakes import FakeGuild, FakeInteraction, FakeMember, FakeRole, FakeSummaryChannel, bot_member

GUILD = "G1"
ROLE = FakeRole(50, "Membro", position=3)
PLAYERS = {
    "p1": {"Id": "p1", "Name": "LeonamUchiha", "GuildId": GUILD, "GuildName": "T E L E T O M"},
    "p2": {"Id": "p2", "Name": "Leonam", "GuildId": "OTHER", "GuildName": "Outra"},
    "p3": {"Id": "p3", "Name": "LeonamBR", "GuildId": None, "GuildName": ""},
}

class FakePlayersAlbion:
    def __init__(self):
        self.players = {key: dict(value) for key, value in PLAYERS.items()}
        self.roster = {GUILD: ["p1"]}
        self.fail_search = self.fail_player = self.fail_roster = False

    async def search(self, term):
        if self.fail_search:
            raise AlbionAPIError("indisponível")
        return {"guilds": [], "players": [p for p in self.players.values() if term.casefold() in p["Name"].casefold()]}

    async def get_player(self, player_id):
        if self.fail_player:
            raise AlbionAPIError("indisponível")
        return self.players[player_id]

    async def get_guild_members(self, guild_id):
        if self.fail_roster:
            raise AlbionAPIError("indisponível")
        return [{"Id": player_id} for player_id in self.roster.get(guild_id, [])]

def server_config(discord_guild_id=1, role=ROLE.id, tag="TLT"):
    return ServerConfig(discord_guild_id, GuildLink(GUILD, "T E L E T O M"), MembersConfig(role, tag))

def server(**bot):
    return FakeGuild(1, [ROLE], bot_member(**bot), owner_id=1)

async def make_cog(cfg=None):
    repos = MemoryRepositories()
    await repos.save_config(cfg or server_config())
    return MemberRegistrationCog(None, FakePlayersAlbion(), repos)

def leonam():
    return PlayerOption("p1", "LeonamUchiha", GUILD, "T E L E T O M")

# ---------- Pesquisa e validação ----------

def test_exact_name_match_is_picked_case_insensitively():
    exact, options = player_options({"players": list(PLAYERS.values())}, "leonamuchiha")
    assert exact.player_id == "p1" and len(options) == 3

def test_partial_name_is_ambiguous():
    exact, options = player_options({"players": list(PLAYERS.values())}, "Leo")
    assert exact is None and [o.player_id for o in options] == ["p1", "p2", "p3"]

@pytest.mark.asyncio
async def test_membership_is_confirmed_by_current_profile():
    albion = FakePlayersAlbion()
    cfg = server_config()
    assert (await confirm_in_guild(albion, cfg, leonam())).guild_id == GUILD
    with pytest.raises(RegistrationError, match="na guilda \\*\\*Outra\\*\\*"):
        await confirm_in_guild(albion, cfg, PlayerOption("p2", "Leonam", "OTHER", "Outra"))
    with pytest.raises(RegistrationError, match="sem guilda"):
        await confirm_in_guild(albion, cfg, PlayerOption("p3", "LeonamBR", None, None))
    # A pesquisa diz que está na guilda, mas o perfil atual diz que não: vale o perfil.
    albion.players["p1"]["GuildId"] = "OTHER"
    with pytest.raises(RegistrationError):
        await confirm_in_guild(albion, cfg, leonam())

@pytest.mark.asyncio
async def test_api_failure_is_not_treated_as_left_guild():
    albion = FakePlayersAlbion()
    albion.fail_player = True
    with pytest.raises(RegistrationError, match="não respondeu"):
        await confirm_in_guild(albion, server_config(), leonam())

# ---------- Exclusividade dos vínculos ----------

@pytest.mark.asyncio
async def test_character_cannot_be_linked_to_two_users():
    repos = MemoryRepositories()
    cfg = server_config()
    await save_registration(repos, cfg, 500, leonam())
    with pytest.raises(RegistrationError, match="outro membro"):
        await save_registration(repos, cfg, 501, leonam())
    assert (await repos.get_link(1, 501)) is None

@pytest.mark.asyncio
async def test_user_keeps_a_single_link_per_server():
    repos = MemoryRepositories()
    cfg = server_config()
    await save_registration(repos, cfg, 500, leonam())
    await save_registration(repos, cfg, 500, PlayerOption("p9", "Outro", GUILD, None))
    links = await repos.links_for(1)
    assert len(links) == 1 and links[0].player_id == "p9"

@pytest.mark.asyncio
async def test_links_are_isolated_by_server_and_old_guild_links_do_not_block():
    repos = MemoryRepositories()
    await save_registration(repos, server_config(discord_guild_id=1), 500, leonam())
    await save_registration(repos, server_config(discord_guild_id=2), 501, leonam())  # outro servidor: permitido
    # Servidor 1 trocou de guilda: o vínculo antigo (outra guilda) não impede outro usuário.
    other_guild = ServerConfig(1, GuildLink("G2", "Nova"))
    await save_registration(repos, other_guild, 502, PlayerOption("p1", "LeonamUchiha", "G2", None))
    assert (await repos.get_link(1, 502)).albion_guild_id == "G2"

# ---------- Cargo e apelido ----------

@pytest.mark.asyncio
async def test_role_and_tag_are_applied():
    guild = server()
    member = FakeMember(guild)
    result = await apply_member_settings(member, server_config(), "LeonamUchiha")
    assert ROLE in member.roles and member.nick == "[TLT] LeonamUchiha" and not result.problems

@pytest.mark.asyncio
async def test_without_role_or_tag_nothing_is_changed():
    guild = server()
    member = FakeMember(guild, nick="apelido")
    result = await apply_member_settings(member, server_config(role=None, tag=None), "LeonamUchiha")
    assert member.roles == [] and member.nick == "apelido" and result.done == [] and result.problems == []

@pytest.mark.asyncio
@pytest.mark.parametrize("member_kwargs, bot_kwargs, expected", [
    ({"fail_nick": True}, {}, "Apelido não alterado: o Discord recusou"),
    ({"user_id": 1}, {}, "dono do servidor"),
    ({"top_position": 10}, {}, "acima ou na mesma posição do cargo do bot"),
    ({}, {"manage_nicknames": False}, "Gerenciar apelidos"),
])
async def test_nickname_failures_do_not_block_role(member_kwargs, bot_kwargs, expected):
    guild = server(**bot_kwargs)
    member = FakeMember(guild, **member_kwargs)
    result = await apply_member_settings(member, server_config(), "LeonamUchiha")
    assert ROLE in member.roles and any(expected in problem for problem in result.problems)

@pytest.mark.asyncio
@pytest.mark.parametrize("bot_kwargs, member_kwargs, expected", [
    ({"top_position": 2}, {}, "acima ou na mesma posição do cargo mais alto do bot"),
    ({"manage_roles": False}, {}, "Gerenciar cargos"),
    ({}, {"fail_roles": True}, "o Discord recusou"),
])
async def test_role_failures_do_not_block_nickname(bot_kwargs, member_kwargs, expected):
    guild = server(**bot_kwargs)
    member = FakeMember(guild, **member_kwargs)
    result = await apply_member_settings(member, server_config(), "LeonamUchiha")
    assert member.nick == "[TLT] LeonamUchiha" and any(expected in problem for problem in result.problems)

@pytest.mark.asyncio
async def test_deleted_role_is_reported():
    guild = FakeGuild(1, [], bot_member())
    result = await apply_member_settings(FakeMember(guild), server_config(tag=None), "LeonamUchiha")
    assert "não existe mais" in result.problems[0]

# ---------- Interface ----------

@pytest.mark.asyncio
async def test_registration_requires_registered_guild():
    cog = await make_cog(ServerConfig(1))
    interaction = FakeInteraction()
    await MemberRegistrationCog.registrar.callback(cog, interaction)
    assert "/cadastrar" in interaction.private_texts()[0] and not interaction.response.modals

@pytest.mark.asyncio
async def test_registrar_opens_private_modal():
    cog = await make_cog()
    interaction = FakeInteraction()
    await MemberRegistrationCog.registrar.callback(cog, interaction)
    assert isinstance(interaction.response.modals[0], CharacterModal)

async def submit(cog, name, member):
    modal = CharacterModal(cog)
    modal.name._value = name
    interaction = FakeInteraction(guild=member.guild, user=member, channel=FakeSummaryChannel())
    await modal.on_submit(interaction)
    return interaction

@pytest.mark.asyncio
async def test_exact_name_registers_privately_with_role_and_nickname():
    cog = await make_cog()
    member = FakeMember(server())
    interaction = await submit(cog, "LeonamUchiha", member)
    assert interaction.response.deferred[0] == {"ephemeral": True, "thinking": True}
    text = interaction.private_texts()[0]
    assert "registrado como **LeonamUchiha**" in text and "[TLT] LeonamUchiha" in text
    assert interaction.followup.sent[0][1]["ephemeral"] and interaction.channel.sent == []
    assert (await cog.repos.get_link(1, 500)).player_id == "p1"

@pytest.mark.asyncio
async def test_nickname_failure_keeps_valid_link():
    cog = await make_cog()
    member = FakeMember(server(), fail_nick=True)
    interaction = await submit(cog, "LeonamUchiha", member)
    assert "O registro está válido" in interaction.private_texts()[0]
    assert await cog.repos.get_link(1, 500) is not None

@pytest.mark.asyncio
async def test_character_from_other_guild_is_refused():
    cog = await make_cog()
    interaction = await submit(cog, "Leonam", FakeMember(server()))
    # "Leonam" é exato para p2 (outra guilda).
    assert "na guilda **Outra**" in interaction.private_texts()[0]
    assert await cog.repos.get_link(1, 500) is None

@pytest.mark.asyncio
async def test_ambiguous_name_asks_to_choose_then_registers():
    cog = await make_cog()
    member = FakeMember(server())
    interaction = await submit(cog, "Leo", member)
    view = interaction.followup.sent[0][1]["view"]
    assert isinstance(view, PlayerSelectView) and len(view.choose.options) == 3
    assert await cog.repos.get_link(1, 500) is None
    view.choose._values = ["0"]
    click = FakeInteraction(guild=member.guild, user=member)
    assert await view.interaction_check(click)
    await view.choose.callback(click)
    assert "registrado como **LeonamUchiha**" in click.private_texts()[0]
    other = FakeInteraction(guild=member.guild, user=FakeMember(member.guild, user_id=777))
    assert not await view.interaction_check(other)

@pytest.mark.asyncio
async def test_not_found_and_api_failure():
    cog = await make_cog()
    member = FakeMember(server())
    assert "Nenhum personagem" in (await submit(cog, "zzz", member)).private_texts()[0]
    cog.albion.fail_search = True
    assert "não respondeu" in (await submit(cog, "LeonamUchiha", member)).private_texts()[0]
    assert await cog.repos.get_link(1, 500) is None

@pytest.mark.asyncio
async def test_existing_link_requires_confirmation_to_replace():
    cog = await make_cog()
    cfg = await cog.repos.get_config(1)
    await save_registration(cog.repos, cfg, 500, PlayerOption("p9", "Antigo", GUILD, None))
    member = FakeMember(server())
    interaction = await submit(cog, "LeonamUchiha", member)
    view = interaction.followup.sent[0][1]["view"]
    assert isinstance(view, ReplaceLinkView) and "Antigo" in interaction.private_texts()[0]
    assert (await cog.repos.get_link(1, 500)).player_id == "p9"
    await view.cancel.callback(FakeInteraction(guild=member.guild, user=member))
    assert (await cog.repos.get_link(1, 500)).player_id == "p9"
    interaction = await submit(cog, "LeonamUchiha", member)
    view = interaction.followup.sent[0][1]["view"]
    await view.replace.callback(FakeInteraction(guild=member.guild, user=member))
    assert (await cog.repos.get_link(1, 500)).player_id == "p1"

@pytest.mark.asyncio
async def test_character_linked_to_someone_else_is_refused():
    cog = await make_cog()
    await save_registration(cog.repos, await cog.repos.get_config(1), 999, leonam())
    interaction = await submit(cog, "LeonamUchiha", FakeMember(server()))
    assert "outro membro" in interaction.private_texts()[0]

# ---------- Saída e retorno à guilda ----------

class SyncBot:
    def __init__(self, guilds):
        self.guilds = {guild.id: guild for guild in guilds}

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)

async def synced_setup():
    repos = MemoryRepositories()
    cfg = server_config()
    await repos.save_config(cfg)
    guild = server()
    member = FakeMember(guild, nick="[TLT] LeonamUchiha")
    member.roles.append(ROLE)
    await save_registration(repos, cfg, 500, leonam())
    albion = FakePlayersAlbion()
    return MemberSync(SyncBot([guild]), albion, repos), albion, repos, member

@pytest.mark.asyncio
async def test_member_who_leaves_loses_role_but_keeps_link_and_nickname_then_regains_it():
    sync, albion, repos, member = await synced_setup()
    albion.roster[GUILD] = ["outro"]
    albion.players["p1"]["GuildId"] = "OTHER"
    stats = await sync.run_once()
    link = await repos.get_link(1, 500)
    assert stats.left == 1 and ROLE not in member.roles and member.nick == "[TLT] LeonamUchiha"
    assert link is not None and link.in_guild is False
    albion.roster[GUILD] = ["p1"]
    stats = await sync.run_once()
    assert stats.returned == 1 and ROLE in member.roles and link.in_guild

@pytest.mark.asyncio
async def test_api_failures_never_remove_roles():
    sync, albion, repos, member = await synced_setup()
    albion.fail_roster = True
    assert (await sync.run_once()).skipped_servers == 1
    albion.fail_roster = False
    albion.roster[GUILD] = []  # resposta vazia suspeita
    assert (await sync.run_once()).skipped_servers == 1
    albion.roster[GUILD] = ["outro"]
    albion.fail_player = True  # ausente da lista, mas o perfil não pôde ser confirmado
    assert (await sync.run_once()).unknown == 1
    albion.fail_player = False  # ausente da lista, mas o perfil ainda diz que está na guilda
    await sync.run_once()
    assert ROLE in member.roles and (await repos.get_link(1, 500)).in_guild

@pytest.mark.asyncio
async def test_sync_without_role_changes_nothing():
    sync, albion, repos, member = await synced_setup()
    cfg = await repos.get_config(1)
    cfg.members = MembersConfig(None, cfg.members.tag)
    albion.roster[GUILD] = ["outro"]
    albion.players["p1"]["GuildId"] = "OTHER"
    stats = await sync.run_once()
    assert stats.left == 1 and stats.roles_removed == 0 and ROLE in member.roles
