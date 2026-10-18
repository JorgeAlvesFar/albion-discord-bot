from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.modules.combat_tracker.cog import CombatCog, ConfirmKillboardResetView, KillboardPanelView
from app.modules.combat_tracker.killboard import reset_killboard, save_killboard, validate_settings
from app.modules.combat_tracker.service import ScanResult
from app.repositories.models import GuildLink, KillboardSettings, MembersConfig, ServerConfig
from tests.fakes import now as _utcnow
from tests.fakes import FakeChannel, FakeGuild, FakeInteraction, FakeSummaryChannel, GUILD, bot_member, event, make_tracker

def discord_guild(channels):
    guild = FakeGuild(1, [], bot_member())
    guild.channels = channels
    return guild

async def setup(role=None, tag=None):
    channels = {10: FakeChannel(10), 20: FakeChannel(20)}
    tracker, _ = make_tracker(channels=channels)
    await tracker.repos.save_config(ServerConfig(1, GuildLink(GUILD, "Guild"), MembersConfig(role, tag)))
    return tracker, channels

def scan(events, minutes=1):
    return ScanResult(events=events, observed_at=_utcnow() + timedelta(minutes=minutes))

async def publish_all(tracker):
    for record in await tracker.repos.due_events(_utcnow()):
        await tracker.publisher.process(record)

def recent(event_id, **kwargs):
    return event(event_id, ts=(_utcnow() + timedelta(seconds=30)).isoformat(), **kwargs)

# ---------- Validação ----------

def test_validation_only_checks_enabled_kinds():
    guild = discord_guild({10: FakeChannel(10), 30: FakeChannel(30, send_messages=False, attach_files=False)})
    assert validate_settings(KillboardSettings(False, None, False, 999), guild) == []
    assert validate_settings(KillboardSettings(True, 10, True, 10), guild) == []  # mesmo canal para os dois tipos
    errors = validate_settings(KillboardSettings(True, None, True, 999), guild)
    assert "Escolha o canal de kills" in errors[0] and "não existe mais" in errors[1]
    errors = validate_settings(KillboardSettings(True, 30, False, None), guild)
    assert "enviar mensagens, anexar arquivos" in errors[0]

# ---------- Salvar e redefinir ----------

@pytest.mark.asyncio
async def test_options_are_off_by_default_and_nothing_is_tracked():
    tracker, _ = await setup()
    cfg = await tracker.repos.get_config(1)
    assert cfg.killboard.settings == KillboardSettings() and not cfg.killboard_active
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD)]))
    assert await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_activation_sets_reference_time_and_version():
    tracker, _ = await setup()
    before = _utcnow()
    result = await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))
    cfg = result.config
    assert result.operation == "ativado" and cfg.killboard_active and cfg.killboard.activated_at >= before and cfg.killboard.version == 1
    assert (await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))).operation is None

@pytest.mark.asyncio
async def test_saving_requires_registered_guild():
    tracker, _ = make_tracker()
    await tracker.repos.save_config(ServerConfig(1))
    with pytest.raises(ValueError):
        await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))

@pytest.mark.asyncio
async def test_kills_and_deaths_follow_independent_switches():
    tracker, channels = await setup()
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(False, 10, True, 20))
    discards = await tracker.enqueue(scan([recent(1, killer_guild=GUILD), recent(2, victim_guild=GUILD)]))
    await publish_all(tracker)
    assert discards["tipo_desativado"] == 1 and channels[10].sent == [] and len(channels[20].sent) == 1

@pytest.mark.asyncio
async def test_same_channel_for_both_kinds():
    tracker, channels = await setup()
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, True, 10))
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD), recent(2, victim_guild=GUILD)]))
    await publish_all(tracker)
    assert len(channels[10].sent) == 2

@pytest.mark.asyncio
async def test_editing_active_killboard_resets_reference_and_discards_pending():
    tracker, channels = await setup()
    first = (await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))).config.killboard.activated_at
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD)]))
    result = await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 20, False, None))
    assert result.operation == "alterado" and result.pending_discarded == 1 and result.config.killboard.activated_at >= first
    await publish_all(tracker)
    assert channels[10].sent == [] and channels[20].sent == []

@pytest.mark.asyncio
async def test_deactivation_stops_publications_and_keeps_channels():
    tracker, channels = await setup()
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, True, 20))
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD)]))
    result = await save_killboard(tracker.repos, tracker, 1, KillboardSettings(False, 10, False, 20))
    cfg = result.config
    assert result.operation == "desativado" and result.pending_discarded == 1
    assert not cfg.killboard_active and cfg.killboard.activated_at is None and (cfg.killboard.settings.kills_channel_id, cfg.killboard.settings.deaths_channel_id) == (10, 20)
    await tracker.enqueue(scan([recent(2, killer_guild=GUILD)]))
    await publish_all(tracker)
    assert channels[10].sent == [] and await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_reactivation_does_not_replay_history():
    tracker, channels = await setup()
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))
    old = recent(1, killer_guild=GUILD)
    await tracker.enqueue(scan([old]))
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(False, 10, False, None))
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))
    # O mesmo evento visto de novo e um combate anterior à reativação não são publicados.
    before_reactivation = event(2, killer_guild=GUILD, ts=(_utcnow() - timedelta(minutes=5)).isoformat())
    discards = await tracker.enqueue(scan([old, before_reactivation]))
    await publish_all(tracker)
    assert discards["duplicado"] == 1 and discards["anterior_a_ativacao"] == 1 and channels[10].sent == []

@pytest.mark.asyncio
async def test_reset_only_touches_killboard():
    tracker, _ = await setup(role=50, tag="TLT")
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, True, 20))
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD)]))
    result = await reset_killboard(tracker.repos, tracker, 1)
    cfg = result.config
    assert result.operation == "redefinido" and result.pending_discarded == 1
    assert cfg.killboard.settings == KillboardSettings() and not cfg.killboard_active
    assert (cfg.guild.guild_id, cfg.members) == (GUILD, MembersConfig(50, "TLT"))
    assert (await reset_killboard(tracker.repos, tracker, 1)).operation is None

@pytest.mark.asyncio
async def test_servers_are_isolated():
    tracker, channels = await setup()
    await tracker.repos.save_config(ServerConfig(2, GuildLink(GUILD, "Guild")))
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))
    await save_killboard(tracker.repos, tracker, 2, KillboardSettings(True, 20, False, None))
    await save_killboard(tracker.repos, tracker, 2, KillboardSettings(False, 20, False, None))
    await tracker.enqueue(scan([recent(1, killer_guild=GUILD)]))
    await publish_all(tracker)
    assert len(channels[10].sent) == 1 and channels[20].sent == []
    assert (await tracker.repos.get_config(1)).killboard_active

# ---------- Interface ----------

def make_cog(tracker):
    return CombatCog(None, tracker.repos, tracker)

def interaction(channels=None, **kwargs):
    return FakeInteraction(guild=discord_guild(channels or {10: FakeChannel(10), 20: FakeChannel(20)}), **kwargs)

@pytest.mark.asyncio
async def test_requires_guild_and_admin():
    tracker, _ = make_tracker()
    cog = make_cog(tracker)
    await tracker.repos.save_config(ServerConfig(1))
    first = interaction()
    await CombatCog.killboard.callback(cog, first)
    assert "/cadastrar" in first.private_texts()[0]
    second = interaction(admin=False)
    await CombatCog.killboard.callback(cog, second)
    assert "Somente administradores" in second.private_texts()[0]

@pytest.mark.asyncio
async def test_panel_layout_and_defaults():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    opened = interaction()
    await CombatCog.killboard.callback(cog, opened)
    _, kwargs = opened.response.sent[0]
    view = kwargs["view"]
    labels = [item.label for item in view.children if getattr(item, "label", None)]
    assert kwargs["ephemeral"] and labels == ["Redefinir", "Kills: desligado", "Mortes: desligado", "Salvar"]
    assert view.reset.disabled and "Desativado" in kwargs["embed"].fields[0].value

@pytest.mark.asyncio
async def test_choosing_channel_turns_kind_on_and_save_publishes_summary():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    view.kills_channel._values = [SimpleNamespace(id=10)]
    await view.kills_channel.callback(interaction())
    assert view.draft.kills_enabled and view.toggle_kills.label == "Kills: ligado"
    channel = FakeSummaryChannel()
    await view.save.callback(interaction(channel=channel))
    fields = {field.name: field.value for field in channel.sent[0].fields}
    assert "Killboard: ativado" == channel.sent[0].title
    assert fields["Realizado por"] == "<@500>" and fields["Kills"] == "Ligado → <#10>" and fields["Sem configuração"] == "mortes"
    assert "Ativo desde" in fields["Estado"]

@pytest.mark.asyncio
async def test_invalid_configuration_is_not_saved():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    view.draft = KillboardSettings(True, 10, True, None)
    attempt = interaction(channels={10: FakeChannel(10, embed_links=False)})
    await view.save.callback(attempt)
    text = attempt.private_texts()[0]
    assert "incorporar links" in text and "Escolha o canal de mortes" in text
    assert not view.finished and not (await tracker.repos.get_config(1)).killboard_active

@pytest.mark.asyncio
async def test_toggle_off_and_save_deactivates():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, False, None))
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    await view.toggle_kills.callback(interaction())
    channel = FakeSummaryChannel()
    await view.save.callback(interaction(channel=channel))
    assert channel.sent[0].title == "Killboard: desativado"
    assert not (await tracker.repos.get_config(1)).killboard_active

@pytest.mark.asyncio
async def test_unchanged_save_publishes_nothing():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    channel = FakeSummaryChannel()
    done = interaction(channel=channel)
    await view.save.callback(done)
    assert channel.sent == [] and "Nada mudou" in done.private_texts()[0]

@pytest.mark.asyncio
async def test_reset_requires_confirmation():
    tracker, _ = await setup()
    cog = make_cog(tracker)
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 10, True, 20))
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    clicked = interaction()
    await view.reset.callback(clicked)
    confirm = clicked.response.edits[0]["view"]
    assert isinstance(confirm, ConfirmKillboardResetView) and "serão **desativadas**" in clicked.response.edits[0]["content"]
    assert (await tracker.repos.get_config(1)).killboard_active
    channel = FakeSummaryChannel()
    await confirm.confirm.callback(interaction(channel=channel))
    assert channel.sent[0].title == "Killboard: redefinido" and not (await tracker.repos.get_config(1)).killboard_active
