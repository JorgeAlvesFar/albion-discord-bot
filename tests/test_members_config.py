import pytest
from app.modules.members_config.cog import ConfirmMembersResetView, MembersConfigCog, MembersPanelView, TagModal
from app.modules.members_config.service import TAG_MAX, format_nickname, normalize_tag, reset_members, save_members, validate_role
from app.repositories.memory import MemoryRepositories
from app.repositories.models import MembersConfig, ServerConfig
from tests.fakes import FakeGuild, FakeInteraction, FakeRole, FakeSummaryChannel, bot_member, config

MEMBER = FakeRole(50, "Membro", position=3)

def guild(roles=(MEMBER,), **bot):
    return FakeGuild(1, roles, bot_member(**bot))

def make_cog():
    return MembersConfigCog(None, MemoryRepositories())

# ---------- Regras ----------

@pytest.mark.parametrize("text, expected", [
    ("", (None, None)),
    ("   ", (None, None)),
    (None, (None, None)),
    ("TLT", ("TLT", None)),
    ("  [TLT]  ", ("TLT", None)),
    ("T  L  T", ("T L T", None)),
])
def test_normalize_tag(text, expected):
    assert normalize_tag(text) == expected

def test_normalize_tag_rejects_invalid():
    assert normalize_tag("A]B")[1] and normalize_tag("X" * (TAG_MAX + 1))[1]
    assert normalize_tag("X" * TAG_MAX) == ("X" * TAG_MAX, None)

def test_nickname_format_and_limit():
    assert format_nickname("TLT", "LeonamUchiha") == "[TLT] LeonamUchiha"
    assert format_nickname(None, "LeonamUchiha") == "LeonamUchiha"
    longest = format_nickname("X" * TAG_MAX, "N" * 16)
    assert len(longest) == 32 and longest.endswith("N" * 16)

def test_role_validation():
    me = bot_member(top_position=10)
    assert validate_role(MEMBER, me) is None
    assert "@everyone" in validate_role(FakeRole(1, "@everyone", 0, default=True), me)
    assert "integração" in validate_role(FakeRole(51, "Bot X", 2, managed=True), me)
    assert "acima" in validate_role(FakeRole(52, "Admin", 10), me)
    assert "Gerenciar cargos" in validate_role(MEMBER, bot_member(manage_roles=False))

@pytest.mark.asyncio
async def test_saving_members_keeps_guild_and_killboard():
    repos = MemoryRepositories()
    await repos.save_config(config(guild="BBB"))
    changed, cfg = await save_members(repos, 1, MembersConfig(50, "TLT"))
    assert changed and cfg.members == MembersConfig(50, "TLT")
    assert cfg.guild.guild_id == "BBB" and cfg.killboard_active and cfg.killboard.version == 1
    assert (await save_members(repos, 1, MembersConfig(50, "TLT")))[0] is False

@pytest.mark.asyncio
async def test_reset_members_only_touches_its_options_and_server():
    repos = MemoryRepositories()
    await repos.save_config(config(discord_guild_id=1, guild="BBB"))
    await repos.save_config(ServerConfig(2, members=MembersConfig(60, "X")))
    await save_members(repos, 1, MembersConfig(50, "TLT"))
    changed, cfg = await reset_members(repos, 1)
    assert changed and cfg.members.empty
    assert cfg.guild.guild_id == "BBB" and cfg.killboard_active
    other = await repos.get_config(2)
    assert other.members == MembersConfig(60, "X")
    assert (await reset_members(repos, 1))[0] is False

# ---------- Interface ----------

@pytest.mark.asyncio
async def test_non_admin_is_blocked():
    cog = make_cog()
    interaction = FakeInteraction(admin=False)
    await MembersConfigCog.registrar_membros.callback(cog, interaction)
    assert "Somente administradores" in interaction.private_texts()[0]

@pytest.mark.asyncio
async def test_panel_opens_privately_with_saved_values():
    cog = make_cog()
    await cog.repos.save_config(ServerConfig(1, members=MembersConfig(50, "TLT")))
    interaction = FakeInteraction()
    await MembersConfigCog.registrar_membros.callback(cog, interaction)
    _, kwargs = interaction.response.sent[0]
    view = kwargs["view"]
    assert kwargs["ephemeral"] and not view.reset.disabled
    assert [item.label for item in view.children if hasattr(item, "label") and item.label] == ["Redefinir", "Editar tag", "Salvar"]
    assert "`[TLT] NomeDoPersonagem`" in [field.value for field in kwargs["embed"].fields]

@pytest.mark.asyncio
async def test_save_without_role_or_tag_is_allowed_and_nothing_changes():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    interaction = FakeInteraction(guild=guild())
    await panel.save.callback(interaction)
    assert "Nada mudou" in interaction.private_texts()[0]

@pytest.mark.asyncio
async def test_save_role_and_tag_publishes_summary():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    panel.role_id = 50
    modal = TagModal(panel)
    modal.tag._value = "[TLT]"
    await modal.on_submit(FakeInteraction(guild=guild()))
    assert panel.tag == "TLT"
    channel = FakeSummaryChannel()
    await panel.save.callback(FakeInteraction(guild=guild(), channel=channel))
    cfg = await cog.repos.get_config(1)
    assert cfg.members == MembersConfig(50, "TLT")
    fields = {field.name: field.value for field in channel.sent[0].fields}
    assert fields["Realizado por"] == "<@500>" and fields["Cargo"] == "<@&50>" and fields["Tag no apelido"] == "`[TLT]`"
    assert fields["Sem configuração"] == "Nada"

@pytest.mark.asyncio
async def test_tag_only_reports_role_as_missing():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    panel.tag = "TLT"
    channel = FakeSummaryChannel()
    await panel.save.callback(FakeInteraction(guild=guild(), channel=channel))
    fields = {field.name: field.value for field in channel.sent[0].fields}
    assert fields["Sem configuração"] == "cargo" and "Não configurado" in fields["Cargo"]

@pytest.mark.asyncio
@pytest.mark.parametrize("bot, role_id, message", [
    ({"top_position": 2}, 50, "acima"),
    ({"manage_roles": False}, 50, "Gerenciar cargos"),
    ({}, 99, "não existe mais"),
])
async def test_invalid_role_is_not_saved_and_panel_stays_open(bot, role_id, message):
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    panel.role_id = role_id
    interaction = FakeInteraction(guild=guild(**bot))
    await panel.save.callback(interaction)
    assert message in interaction.private_texts()[0]
    assert await cog.repos.get_config(1) is None and not panel.finished

@pytest.mark.asyncio
async def test_tag_requires_manage_nicknames():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    panel.tag = "TLT"
    interaction = FakeInteraction(guild=guild(manage_nicknames=False))
    await panel.save.callback(interaction)
    assert "Gerenciar apelidos" in interaction.private_texts()[0] and await cog.repos.get_config(1) is None

@pytest.mark.asyncio
async def test_invalid_tag_keeps_previous_value():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, "OLD")
    modal = TagModal(panel)
    modal.tag._value = "X" * (TAG_MAX + 1)
    interaction = FakeInteraction(guild=guild())
    await modal.on_submit(interaction)
    assert panel.tag == "OLD" and "no máximo" in interaction.private_texts()[0]

@pytest.mark.asyncio
async def test_reset_requires_confirmation_and_keeps_other_modules():
    cog = make_cog()
    await cog.repos.save_config(config(guild="BBB"))
    await save_members(cog.repos, 1, MembersConfig(50, "TLT"))
    panel = MembersPanelView(cog, 1, 50, "TLT")
    interaction = FakeInteraction(guild=guild())
    await panel.reset.callback(interaction)
    edit = interaction.response.edits[0]
    assert isinstance(edit["view"], ConfirmMembersResetView) and "não são alterados" in edit["content"]
    assert (await cog.repos.get_config(1)).members.role_id == 50
    channel = FakeSummaryChannel()
    await edit["view"].confirm.callback(FakeInteraction(guild=guild(), channel=channel))
    cfg = await cog.repos.get_config(1)
    assert cfg.members.empty and cfg.killboard_active
    assert "configuração redefinida" in channel.sent[0].title

@pytest.mark.asyncio
async def test_second_click_after_save_does_nothing():
    cog = make_cog()
    panel = MembersPanelView(cog, 1, None, None)
    panel.tag = "TLT"
    channel = FakeSummaryChannel()
    await panel.save.callback(FakeInteraction(guild=guild(), channel=channel))
    again = FakeInteraction(guild=guild(), channel=channel)
    assert not await panel.interaction_check(again)
    assert len(channel.sent) == 1
