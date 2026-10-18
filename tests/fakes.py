import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import discord
from app.integrations.albion.client import AlbionAPIError
from app.modules.combat_tracker.service import CombatTracker
from app.repositories.memory import MemoryRepositories
from app.repositories.models import GuildLink, KillboardConfig, KillboardSettings, MembersConfig, ServerConfig

GUILD = "G1"
ACTIVATION = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

def now() -> datetime:
    return datetime.now(timezone.utc)

def event(event_id, killer_guild="X", victim_guild="Y", ts="2026-10-18T12:05:00Z"):
    return {"EventId": event_id, "TimeStamp": ts, "Killer": {"Name": "k", "GuildId": killer_guild}, "Victim": {"Name": "v", "GuildId": victim_guild}}

def config(discord_guild_id=1, guild=GUILD, kills=10, deaths=20, activated_at=ACTIVATION, version=1, role=None, tag=None):
    """Servidor com guilda cadastrada e killboard ativo (kills/deaths=None desliga o tipo)."""
    settings = KillboardSettings(kills is not None, kills, deaths is not None, deaths)
    return ServerConfig(
        discord_guild_id, GuildLink(guild, "Guild") if guild else None, MembersConfig(role, tag),
        KillboardConfig(settings, activated_at, version),
    )

def deactivate(cfg: ServerConfig) -> None:
    cfg.killboard = replace(cfg.killboard, activated_at=None, version=cfg.killboard.version + 1)

def reactivate(cfg: ServerConfig, at: datetime = ACTIVATION) -> None:
    cfg.killboard = replace(cfg.killboard, activated_at=at, version=cfg.killboard.version + 1)

def bump_version(cfg: ServerConfig) -> None:
    cfg.killboard = replace(cfg.killboard, version=cfg.killboard.version + 1)

def http_error(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="falha simulada"), "falha simulada")

class FakeAlbion:
    def __init__(self, feed=None):
        self.feed = feed or []
        self.offsets = []
        self.fail_at_offset = None

    async def get_recent_events(self, limit, offset):
        if offset == self.fail_at_offset:
            raise AlbionAPIError("indisponível")
        self.offsets.append(offset)
        return self.feed[offset:offset + limit]

class FakeChannel(discord.TextChannel):
    """Canal falso: `failures` é uma lista de exceções levantadas, uma por envio, antes de começar a aceitar."""
    def __init__(self, channel_id, failures=None, block: asyncio.Event | None = None, **perms):
        self.id = channel_id
        self.name = f"canal-{channel_id}"
        self.guild = SimpleNamespace(me=None)
        self.sent = []
        self.failures = list(failures or [])
        self.block = block
        self.perms = SimpleNamespace(**{"view_channel": True, "send_messages": True, "embed_links": True, "attach_files": True, **perms})

    def permissions_for(self, member):
        return self.perms

    async def send(self, embed=None, file=None):
        if self.block is not None:
            await self.block.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(embed)
        return SimpleNamespace(id=1000 + len(self.sent))

class FakeBot:
    def __init__(self, channels):
        self.channels = channels
        self.closed = False

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        return self.closed

class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent, self.edits, self.modals, self.deferred = [], [], [], []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        assert not self.done, "interação respondida duas vezes"
        self.done = True
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        assert not self.done, "interação respondida duas vezes"
        self.done = True
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        assert not self.done, "interação respondida duas vezes"
        self.done = True
        self.deferred.append(kwargs)

    async def send_modal(self, modal):
        assert not self.done, "interação respondida duas vezes"
        self.done = True
        self.modals.append(modal)

class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

class FakeSummaryChannel:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, embed=None, **kwargs):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        self.sent.append(embed)

class FakeRole:
    def __init__(self, role_id, name="Membro", position=1, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = role_id, name, position, managed, default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

def bot_member(top_position=10, manage_roles=True, manage_nicknames=True):
    return SimpleNamespace(
        top_role=FakeRole(1, "Bot", top_position),
        guild_permissions=SimpleNamespace(manage_roles=manage_roles, manage_nicknames=manage_nicknames),
    )

class FakeGuild:
    def __init__(self, discord_guild_id=1, roles=(), me=None, owner_id=1):
        self.id = discord_guild_id
        self.roles = {role.id: role for role in roles}
        self.me = me
        self.owner_id = owner_id
        self.members = {}

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def get_channel(self, channel_id):
        return getattr(self, "channels", {}).get(channel_id)

class FakeMember:
    """Membro do Discord: `fail_roles`/`fail_nick` simulam o Discord recusando a alteração."""
    def __init__(self, guild, user_id=500, top_position=1, nick=None, fail_roles=False, fail_nick=False, admin=False):
        self.guild, self.id, self.nick = guild, user_id, nick
        self.mention = f"<@{user_id}>"
        self.roles = []
        self.top_role = FakeRole(2, "topo", top_position)
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.fail_roles, self.fail_nick = fail_roles, fail_nick
        guild.members[user_id] = self

    async def add_roles(self, role, reason=None):
        if self.fail_roles:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)

    async def edit(self, nick=None, reason=None):
        if self.fail_nick:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.nick = nick

class FakeInteraction:
    def __init__(self, discord_guild_id=1, user_id=500, admin=True, channel=None, guild=None, user=None):
        self.guild = guild or SimpleNamespace(id=discord_guild_id, me=None)
        self.user = user or SimpleNamespace(id=user_id, mention=f"<@{user_id}>", guild_permissions=SimpleNamespace(administrator=admin))
        self.channel = channel or FakeSummaryChannel()
        self.channel_id = 99
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)

    def private_texts(self):
        texts = [content for content, _ in self.response.sent + self.followup.sent if content]
        texts += [edit.get("content") for edit in self.response.edits + self.original_edits if edit.get("content")]
        return texts

def make_tracker(feed=None, channels=None, albion=None, **kwargs):
    channels = channels if channels is not None else {10: FakeChannel(10), 20: FakeChannel(20)}
    tracker = CombatTracker(FakeBot(channels), albion or FakeAlbion(feed), MemoryRepositories(), **kwargs)
    return tracker, channels
