"""Testes integrados: os quatro módulos juntos, em mais de um servidor, com falhas e reinício."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
import discord
import pytest
from discord.ext import commands
from app.modules.combat_tracker import publisher, service
from app.modules.combat_tracker.cog import CombatCog
from app.modules.combat_tracker.killboard import save_killboard
from app.modules.combat_tracker.service import ScanResult
from app.modules.common import AdminView
from app.modules.guild_registration.cog import GuildRegistrationCog
from app.modules.guild_registration.service import link_guild
from app.modules.member_registration.cog import CharacterModal, MemberRegistrationCog
from app.modules.member_registration.service import PlayerOption, RegistrationError, active_link, save_registration
from app.modules.members_config.cog import MembersConfigCog
from app.modules.members_config.service import save_members
from app.repositories.models import FAILED, GuildLink, KillboardSettings, MembersConfig
from tests.fakes import now as _utcnow
from tests.fakes import FakeAlbion, FakeChannel, FakeGuild, FakeInteraction, FakeMember, FakeRole, bot_member, event, http_error, make_tracker

G1, G2 = "GUILD-1", "GUILD-2"

class WorldAlbion(FakeAlbion):
    """Feed global + pesquisa e perfis de personagens."""
    def __init__(self, feed=None):
        super().__init__(feed)
        self.players = {
            "p1": {"Id": "p1", "Name": "Alpha", "GuildId": G1, "GuildName": "Guilda 1"},
            "p2": {"Id": "p2", "Name": "Bravo", "GuildId": G2, "GuildName": "Guilda 2"},
            "p3": {"Id": "p3", "Name": "Charlie", "GuildId": G1, "GuildName": "Guilda 1"},
        }

    async def search(self, term):
        return {"guilds": [], "players": [p for p in self.players.values() if p["Name"].casefold() == term.casefold()]}

    async def get_player(self, player_id):
        return self.players[player_id]

def recent(event_id, seconds=30, **kwargs):
    return event(event_id, ts=(_utcnow() + timedelta(seconds=seconds)).isoformat(), **kwargs)

def scan(events):
    return ScanResult(events=events, observed_at=_utcnow() + timedelta(minutes=1))

async def publish_all(tracker):
    for record in await tracker.repos.due_events(_utcnow()):
        await tracker.publisher.process(record)

async def world():
    channels = {c: FakeChannel(c) for c in (11, 12, 21, 22)}
    tracker, _ = make_tracker(channels=channels)
    tracker.feed.albion = WorldAlbion()
    return tracker, channels

async def configure_server(tracker, gid, guild_id, kills, deaths, role=None, tag=None):
    await link_guild(tracker.repos, tracker, gid, GuildLink(guild_id, f"Guilda {guild_id[-1]}", None))
    if role or tag:
        await save_members(tracker.repos, gid, MembersConfig(role, tag))
    return await save_killboard(tracker.repos, tracker, gid, KillboardSettings(kills is not None, kills, deaths is not None, deaths))

async def register(tracker, gid, user_id, player_id, member=None):
    cfg = await tracker.repos.get_config(gid)
    profile = tracker.feed.albion.players[player_id]
    return await save_registration(tracker.repos, cfg, user_id, PlayerOption(player_id, profile["Name"], profile["GuildId"], profile["GuildName"]))

# ---------- Múltiplos servidores ----------

@pytest.mark.asyncio
async def test_two_servers_each_get_only_their_events_links_and_settings():
    tracker, channels = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=12, role=50, tag="UM")
    await configure_server(tracker, 2, G2, kills=21, deaths=22)
    await register(tracker, 1, 500, "p1")
    await register(tracker, 2, 500, "p2")  # o mesmo usuário Discord em outro servidor, com outro personagem
    await tracker.enqueue(scan([
        recent(1, killer_guild=G1, victim_guild=G2),  # kill da guilda 1 = morte da guilda 2
        recent(2, killer_guild=G2, victim_guild="X"),
        recent(3, killer_guild="X", victim_guild="Y"),
    ]))
    await publish_all(tracker)
    assert [len(channels[c].sent) for c in (11, 12, 21, 22)] == [1, 0, 1, 1]
    first, second = await tracker.repos.get_config(1), await tracker.repos.get_config(2)
    assert first.members == MembersConfig(50, "UM") and second.members == MembersConfig()
    assert [link.player_id for link in await tracker.repos.links_for(1)] == ["p1"]
    assert [link.player_id for link in await tracker.repos.links_for(2)] == ["p2"]

@pytest.mark.asyncio
async def test_commands_never_reach_another_server():
    tracker, _ = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    cog = CombatCog(None, tracker.repos, tracker)
    from app.modules.combat_tracker.cog import KillboardPanelView
    view = KillboardPanelView(cog, await tracker.repos.get_config(1))
    intruder = FakeInteraction(discord_guild_id=2)
    assert not await view.interaction_check(intruder)
    assert "outro servidor" in intruder.private_texts()[0]

# ---------- Independência dos módulos ----------

@pytest.mark.asyncio
async def test_no_module_starts_another():
    tracker, _ = await world()
    await link_guild(tracker.repos, tracker, 1, GuildLink(G1, "Guilda 1", None))
    cfg = await tracker.repos.get_config(1)
    assert not cfg.killboard_active and cfg.members.role_id is None
    await save_members(tracker.repos, 1, MembersConfig(50, "UM"))
    assert not cfg.killboard_active
    await register(tracker, 1, 500, "p1")
    assert not cfg.killboard_active
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(True, 11, False, None))
    assert cfg.members == MembersConfig(50, "UM") and len(await tracker.repos.links_for(1)) == 1

@pytest.mark.asyncio
async def test_guild_change_resets_killboard_and_does_not_carry_links():
    tracker, channels = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=None, role=50, tag="UM")
    await register(tracker, 1, 500, "p1")
    await tracker.enqueue(scan([recent(1, killer_guild=G1)]))
    result = await link_guild(tracker.repos, tracker, 1, GuildLink(G2, "Guilda 2", None))
    cfg = await tracker.repos.get_config(1)
    assert result.pending_discarded == 1 and not cfg.killboard_active and cfg.killboard.settings.kills_channel_id is None
    assert cfg.members == MembersConfig(50, "UM")  # membros não fazem parte do reset
    old_link = await tracker.repos.get_link(1, 500)
    assert old_link is not None and active_link(old_link, cfg) is None
    await register(tracker, 1, 600, "p2")  # personagem da nova guilda, por outro usuário
    await publish_all(tracker)
    assert channels[11].sent == []

# ---------- Eventos ----------

@pytest.mark.asyncio
async def test_late_event_from_before_activation_and_duplicates_across_servers():
    tracker, channels = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    await configure_server(tracker, 2, G1, kills=21, deaths=None)  # dois servidores acompanhando a mesma guilda
    late = event(1, killer_guild=G1, ts=(_utcnow() - timedelta(minutes=5)).isoformat())
    fresh = recent(2, killer_guild=G1)
    discards = await tracker.enqueue(scan([late, fresh]))
    await tracker.enqueue(scan([fresh]))  # leitura sobreposta
    await publish_all(tracker)
    assert discards["anterior_a_ativacao"] == 2
    assert len(channels[11].sent) == 1 and len(channels[21].sent) == 1

@pytest.mark.asyncio
async def test_permission_lost_after_activation_fails_permanently_without_blocking_other_server():
    tracker, channels = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    await configure_server(tracker, 2, G1, kills=21, deaths=None)
    me = SimpleNamespace()
    channels[11].guild = SimpleNamespace(me=me)
    channels[11].perms.send_messages = False
    await tracker.enqueue(scan([recent(1, killer_guild=G1)]))
    record = await tracker.repos.get_event(1, 1)
    await publish_all(tracker)
    assert record.deliveries["kill"].status == FAILED and "sem permissão" in record.deliveries["kill"].last_error
    assert channels[11].sent == [] and len(channels[21].sent) == 1

@pytest.mark.asyncio
async def test_large_battle_over_overlapping_scans():
    tracker, _ = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=12)
    feed = [recent(i, killer_guild="X") for i in range(100, 0, -1)]
    tracker.feed.albion.feed = feed
    await tracker.feed.scan()
    relevant, next_id = set(), 101
    for _ in range(5):  # 300 eventos novos por ciclo; 1 a cada 10 envolve a guilda
        batch = []
        for _ in range(300):
            involved = next_id % 10 == 0
            batch.append(recent(next_id, killer_guild=G1 if involved else "X"))
            if involved:
                relevant.add(next_id)
            next_id += 1
        feed[:0] = list(reversed(batch))
        result = await tracker.feed.scan()
        assert not result.hit_offset_limit and len(result.events) == 300
        await tracker.enqueue(result)
    queued = {key[1] for key in tracker.repos._events}
    assert queued == relevant
    # Pico acima do alcance da paginação: o coletor sinaliza a perda e o intervalo cai para o mínimo.
    feed[:0] = [recent(i, killer_guild="X") for i in range(next_id + 1500, next_id, -1)]
    result = await tracker.feed.scan()
    tracker._last_scan_started = 0
    tracker._adapt_interval(result, started=30)
    assert result.hit_offset_limit and tracker.interval == service.MIN_POLL_SECONDS

@pytest.mark.asyncio
async def test_running_loops_retry_slow_and_failing_discord_without_duplicates(monkeypatch):
    monkeypatch.setattr(publisher, "RETRY_DELAYS", (0.3,) * 5)
    monkeypatch.setattr(service, "MIN_POLL_SECONDS", 0)
    tracker, channels = await world()
    channels[11].failures = [http_error(503), http_error(502)]
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    tracker.feed.albion.feed = [recent(i, killer_guild="X") for i in range(10, 0, -1)]
    tracker.max_poll_seconds = 0
    tracker.start()
    await asyncio.sleep(1.2)
    tracker.feed.albion.feed[:0] = [recent(11, killer_guild=G1)]
    await asyncio.sleep(3)
    tracker.bot.closed = True
    await tracker.close()
    assert len(channels[11].sent) == 1 and await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_deactivation_during_a_send_keeps_that_message_and_drops_the_rest():
    tracker, channels = await world()
    release = asyncio.Event()
    channels[11].block = release
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    await tracker.enqueue(scan([recent(1, killer_guild=G1)]))
    record = await tracker.repos.get_event(1, 1)
    sending = asyncio.create_task(tracker.publisher.process(record))
    await asyncio.sleep(0.1)  # envio em andamento
    await save_killboard(tracker.repos, tracker, 1, KillboardSettings(False, 11, False, None))
    await tracker.enqueue(scan([recent(2, killer_guild=G1)]))
    release.set()
    await sending
    await publish_all(tracker)
    assert len(channels[11].sent) == 1 and await tracker.repos.pending_count() == 0

@pytest.mark.asyncio
async def test_restart_loses_state_and_does_not_replay_feed_history():
    tracker, channels = await world()
    await configure_server(tracker, 1, G1, kills=11, deaths=None)
    history = [recent(i, killer_guild=G1) for i in range(60, 0, -1)]
    # Reinício: memória nova. A configuração se perdeu e precisa ser refeita.
    restarted, restarted_channels = make_tracker(channels=channels)
    restarted.feed.albion = WorldAlbion(history)
    assert await restarted.repos.get_config(1) is None
    await configure_server(restarted, 1, G1, kills=11, deaths=None)
    first = await restarted.feed.scan()  # leitura inicial: só marca como vistos
    await restarted.enqueue(first)
    second = await restarted.feed.scan()
    await restarted.enqueue(second)
    await publish_all(restarted)
    assert first.first_scan and second.events == [] and channels[11].sent == []

# ---------- Interações ----------

@pytest.mark.asyncio
async def test_expired_panel_tells_the_user():
    panel = AdminView(1, timeout=0.05)
    origin = FakeInteraction()
    panel.origin = origin
    await asyncio.sleep(0.2)
    await panel.on_timeout()
    assert "expirou" in origin.original_edits[0]["content"]
    finished = AdminView(1)
    finished.origin = FakeInteraction()
    finished.finish()
    await finished.on_timeout()
    assert finished.origin.original_edits == []

@pytest.mark.asyncio
async def test_registration_end_to_end_with_configured_role_and_tag():
    tracker, _ = await world()
    await configure_server(tracker, 1, G1, kills=None, deaths=None, role=50, tag="UM")
    role = FakeRole(50, "Membro", 3)
    guild = FakeGuild(1, [role], bot_member())
    member = FakeMember(guild)
    cog = MemberRegistrationCog(None, tracker.feed.albion, tracker.repos)
    modal = CharacterModal(cog)
    modal.name._value = "Alpha"
    done = FakeInteraction(guild=guild, user=member)
    await modal.on_submit(done)
    assert role in member.roles and member.nick == "[UM] Alpha" and done.channel.sent == []
    with pytest.raises(RegistrationError):
        await register(tracker, 1, 777, "p1")
    assert not (await tracker.repos.get_config(1)).killboard_active

@pytest.mark.asyncio
async def test_command_tree_has_exactly_the_four_commands():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    tracker, _ = await world()
    for cog in (GuildRegistrationCog(bot, None, tracker.repos, tracker), MembersConfigCog(bot, tracker.repos),
                MemberRegistrationCog(bot, None, tracker.repos), CombatCog(bot, tracker.repos, tracker)):
        await bot.add_cog(cog)
    payloads = {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}
    assert set(payloads) == {"cadastrar", "registrar-membros", "registrar", "killboard"}
    admin_only = {name for name, p in payloads.items() if str(p.get("default_member_permissions")) == "8"}
    assert admin_only == {"cadastrar", "registrar-membros", "killboard"}
