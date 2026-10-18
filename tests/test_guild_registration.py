import pytest
from app.integrations.albion.client import AlbionAPIError
from app.modules.guild_registration.cog import (
    ConfirmGuildView, ConfirmResetView, GuildPanelView, GuildRegistrationCog, GuildResultsView, GuildSearchModal,
)
from app.modules.guild_registration.service import guild_options, link_guild, reset_guild
from app.repositories.models import GuildLink, KillboardSettings, ServerConfig
from tests.fakes import ACTIVATION, FakeInteraction, FakeSummaryChannel, config, event, make_tracker

SEARCH = {"guilds": [
    {"Id": "AAA", "Name": "TELETOM", "AllianceName": ""},
    {"Id": "BBB", "Name": "T E L E T O M", "AllianceName": "LTM"},
    {"Id": "AAA", "Name": "TELETOM"},  # duplicada
    {"Name": "Sem id"},
]}
TELETOM = GuildLink("BBB", "T E L E T O M", "LTM")
OTHER = GuildLink("CCC", "Outra", None)

class FakeSearchAlbion:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.terms = result if result is not None else SEARCH, fail, []

    async def search(self, term):
        self.terms.append(term)
        if self.fail:
            raise AlbionAPIError("indisponível")
        return self.result

def make_cog(albion=None):
    tracker, _ = make_tracker()
    return GuildRegistrationCog(None, albion or FakeSearchAlbion(), tracker.repos, tracker), tracker

# ---------- Regras ----------

def test_options_keep_api_order_without_duplicates_or_incomplete_entries():
    assert guild_options(SEARCH) == [GuildLink("AAA", "TELETOM", None), TELETOM]
    assert guild_options({}) == []

@pytest.mark.asyncio
async def test_first_registration_does_not_activate_killboard():
    cog, tracker = make_cog()
    result = await link_guild(tracker.repos, tracker, 1, TELETOM)
    cfg = await tracker.repos.get_config(1)
    assert result.changed and result.previous is None
    assert cfg.guild == GuildLink("BBB", "T E L E T O M", "LTM")
    assert not cfg.killboard_active and cfg.killboard.activated_at is None

@pytest.mark.asyncio
async def test_same_guild_again_changes_nothing_else():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(guild="BBB"))
    await tracker.enqueue(_scan([event(1, killer_guild="BBB")]))
    result = await link_guild(tracker.repos, tracker, 1, TELETOM)
    cfg = await tracker.repos.get_config(1)
    assert not result.changed and cfg.killboard_active and cfg.killboard.version == 1
    assert await tracker.repos.pending_count() == 1

@pytest.mark.asyncio
async def test_changing_guild_resets_killboard_and_discards_pending():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(guild="BBB"))
    await tracker.enqueue(_scan([event(1, killer_guild="BBB")]))
    result = await link_guild(tracker.repos, tracker, 1, OTHER)
    cfg = await tracker.repos.get_config(1)
    assert result.changed and result.previous.guild_id == "BBB" and result.killboard_was_active and result.pending_discarded == 1
    assert cfg.guild.guild_id == "CCC" and not cfg.killboard_active
    assert cfg.killboard.settings == KillboardSettings() and cfg.killboard.activated_at is None
    assert cfg.killboard.version == 2

@pytest.mark.asyncio
async def test_reset_removes_guild_and_killboard_only_for_that_server():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(discord_guild_id=1, guild="BBB"))
    await tracker.repos.save_config(config(discord_guild_id=2, guild="BBB"))
    assert await reset_guild(tracker.repos, tracker, 1)
    first, second = await tracker.repos.get_config(1), await tracker.repos.get_config(2)
    assert first.guild is None and not first.killboard_active and first.killboard.settings == KillboardSettings()
    assert second.guild.guild_id == "BBB" and second.killboard_active
    assert await reset_guild(tracker.repos, tracker, 3) is None

def _scan(events):
    from app.modules.combat_tracker.service import ScanResult
    from datetime import timedelta
    return ScanResult(events=events, observed_at=ACTIVATION + timedelta(minutes=10))

# ---------- Interface ----------

@pytest.mark.asyncio
async def test_non_admin_cannot_open_or_use_panels():
    cog, tracker = make_cog()
    interaction = FakeInteraction(admin=False)
    await GuildRegistrationCog.cadastrar.callback(cog, interaction)
    assert "Somente administradores" in interaction.private_texts()[0]
    view = ConfirmGuildView(cog, 1, TELETOM)
    assert not await view.interaction_check(FakeInteraction(admin=False))
    assert not await view.interaction_check(FakeInteraction(discord_guild_id=2))
    assert await tracker.repos.get_config(1) is None

@pytest.mark.asyncio
async def test_panel_shows_status_privately_and_reset_only_when_registered():
    cog, tracker = make_cog()
    interaction = FakeInteraction()
    await GuildRegistrationCog.cadastrar.callback(cog, interaction)
    content, kwargs = interaction.response.sent[0]
    assert kwargs["ephemeral"] and kwargs["view"].reset.disabled
    await tracker.repos.save_config(ServerConfig(1, GuildLink("BBB", "T E L E T O M", "LTM")))
    interaction = FakeInteraction()
    await GuildRegistrationCog.cadastrar.callback(cog, interaction)
    _, kwargs = interaction.response.sent[0]
    assert not kwargs["view"].reset.disabled
    assert any(field.value == "`BBB`" for field in kwargs["embed"].fields)

@pytest.mark.asyncio
async def test_search_defers_then_lists_results_without_choosing():
    albion = FakeSearchAlbion()
    cog, tracker = make_cog(albion)
    modal = GuildSearchModal(cog, 1)
    modal.name._value = "  teletom  "
    interaction = FakeInteraction()
    await modal.on_submit(interaction)
    assert interaction.response.deferred and albion.terms == ["teletom"]
    view = interaction.followup.sent[0][1]["view"]
    assert isinstance(view, GuildResultsView) and len(view.choose.options) == 2
    assert "ID: BBB | LTM" in view.choose.options[1].description
    assert await tracker.repos.get_config(1) is None

@pytest.mark.asyncio
async def test_search_failure_keeps_previous_registration():
    cog, tracker = make_cog(FakeSearchAlbion(fail=True))
    await tracker.repos.save_config(ServerConfig(1, GuildLink("BBB", "T E L E T O M", "LTM")))
    modal = GuildSearchModal(cog, 1)
    modal.name._value = "outra"
    interaction = FakeInteraction()
    await modal.on_submit(interaction)
    assert "não respondeu" in interaction.private_texts()[0]
    assert (await tracker.repos.get_config(1)).guild.guild_id == "BBB"

@pytest.mark.asyncio
async def test_search_without_results():
    cog, tracker = make_cog(FakeSearchAlbion(result={"guilds": []}))
    modal = GuildSearchModal(cog, 1)
    modal.name._value = "xyz"
    interaction = FakeInteraction()
    await modal.on_submit(interaction)
    assert "Nenhuma guilda encontrada" in interaction.private_texts()[0]

@pytest.mark.asyncio
async def test_selection_asks_confirmation_and_warns_about_replacement():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(guild="AAA"))
    view = GuildResultsView(cog, 1, guild_options(SEARCH))
    view.choose._values = ["1"]
    interaction = FakeInteraction()
    await view.choose.callback(interaction)
    edit = interaction.response.edits[0]
    assert isinstance(edit["view"], ConfirmGuildView) and "será substituída" in edit["content"]
    assert (await tracker.repos.get_config(1)).guild.guild_id == "AAA"

@pytest.mark.asyncio
async def test_confirm_publishes_summary_with_mention_once():
    cog, tracker = make_cog()
    view = ConfirmGuildView(cog, 1, TELETOM)
    channel = FakeSummaryChannel()
    interaction = FakeInteraction(channel=channel)
    assert await view.interaction_check(interaction)
    await view.confirm.callback(interaction)
    embed = channel.sent[0]
    assert "guilda cadastrada" in embed.title and embed.fields[0].value == "<@500>"
    assert any(field.value == "`BBB`" for field in embed.fields)
    assert any("killboard" in field.value for field in embed.fields if field.name == "Sem configuração")
    # Segundo clique no mesmo botão: bloqueado, nada é refeito nem publicado de novo.
    again = FakeInteraction(channel=channel)
    assert not await view.interaction_check(again)
    assert "já foi concluída" in again.private_texts()[0] and len(channel.sent) == 1

@pytest.mark.asyncio
async def test_confirm_same_guild_publishes_nothing():
    cog, tracker = make_cog()
    await tracker.repos.save_config(ServerConfig(1, GuildLink("BBB", "T E L E T O M", "LTM")))
    channel = FakeSummaryChannel()
    interaction = FakeInteraction(channel=channel)
    await ConfirmGuildView(cog, 1, TELETOM).confirm.callback(interaction)
    assert channel.sent == [] and "Nada mudou" in interaction.private_texts()[0]

@pytest.mark.asyncio
async def test_summary_failure_is_reported_privately_and_change_is_kept():
    cog, tracker = make_cog()
    interaction = FakeInteraction(channel=FakeSummaryChannel(fail=True))
    await ConfirmGuildView(cog, 1, TELETOM).confirm.callback(interaction)
    assert (await tracker.repos.get_config(1)).guild.guild_id == "BBB"
    assert any("não consegui publicar o resumo" in text for text in interaction.private_texts())

@pytest.mark.asyncio
async def test_reset_requires_confirmation_and_cancel_keeps_everything():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(guild="BBB"))
    panel = GuildPanelView(cog, 1, has_guild=True)
    interaction = FakeInteraction()
    await panel.reset.callback(interaction)
    confirm_view = interaction.response.edits[0]["view"]
    assert isinstance(confirm_view, ConfirmResetView) and "desativado e sua configuração apagada" in interaction.response.edits[0]["content"]
    assert (await tracker.repos.get_config(1)).killboard_active
    await confirm_view.cancel.callback(FakeInteraction())
    assert (await tracker.repos.get_config(1)).guild.guild_id == "BBB"

@pytest.mark.asyncio
async def test_confirmed_reset_publishes_summary():
    cog, tracker = make_cog()
    await tracker.repos.save_config(config(guild="BBB"))
    channel = FakeSummaryChannel()
    await ConfirmResetView(cog, 1).confirm.callback(FakeInteraction(channel=channel))
    cfg = await tracker.repos.get_config(1)
    assert cfg.guild is None and not cfg.killboard_active
    assert "cadastro redefinido" in channel.sent[0].title
