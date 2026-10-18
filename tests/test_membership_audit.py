from datetime import datetime, timedelta, timezone
import pytest
from app.modules.member_registration.audit import (
    Activity, BRASILIA, MembershipAudit, Removal, RETRY_AFTER_FAILURE, next_report_at, removal_embed, report_embed,
)
from app.repositories.memory import MemoryRepositories
from app.repositories.models import AuditConfig, GuildLink, MemberLink, MembersConfig, ServerConfig
from tests.fakes import FakeBot, FakeChannel, http_error
from tests.test_member_registration import GUILD, ROLE, leonam, synced_setup

NINE_BRT = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # 09:00 em Brasília


def cfg(channel_id=30, hour=9):
    return ServerConfig(1, GuildLink(GUILD, "T E L E T O M"), MembersConfig(ROLE.id, "TLT"), audit=AuditConfig(channel_id, hour, 0))


def link():
    return MemberLink(1, 500, "p1", "LeonamUchiha", GUILD)


def field(embed, name):
    return next(item.value for item in embed.fields if item.name == name or item.name.startswith(name))


async def make_audit(config=None):
    repos = MemoryRepositories()
    current = config or cfg()
    await repos.save_config(current)
    channel = FakeChannel(current.audit.channel_id or 30)
    audit = MembershipAudit(FakeBot({channel.id: channel}), repos)
    return audit, repos, channel


def test_next_report_uses_brasilia_and_is_strictly_after_now():
    config = cfg(hour=9)
    assert next_report_at(config, NINE_BRT) == NINE_BRT + timedelta(days=1)
    before = datetime(2026, 10, 18, 11, 59, tzinfo=timezone.utc)  # 08:59 BRT
    assert next_report_at(config, before) == NINE_BRT
    assert next_report_at(config, before).astimezone(BRASILIA).hour == 9


def test_embeds_include_period_and_reason():
    current = cfg()
    embed = removal_embed(current, Removal(500, "LeonamUchiha", "p1", "perfil sem guilda", "cargo removido", NINE_BRT))
    assert field(embed, "Motivo") == "perfil sem guilda" and field(embed, "Membro") == "<@500>"
    report = report_embed(current, Activity(NINE_BRT), NINE_BRT + timedelta(days=1))
    assert "Período:" in report.description and field(report, "Removidos") == "Ninguém no período."


@pytest.mark.asyncio
async def test_removal_is_published_immediately_and_listed_in_the_daily_report():
    audit, repos, channel = await make_audit()
    await audit.record_removal(await repos.get_config(1), link(), "perfil na guilda Outra", "cargo <@&50> removido")
    assert len(channel.sent) == 1 and channel.sent[0].title == "Membro removido do registro"
    assert field(channel.sent[0], "Personagem") == "LeonamUchiha"
    assert await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)) == 0
    audit.record_cycle(1, checked=1, unknown=0, roster_unavailable=0, role_errors=0)
    assert await audit.tick(NINE_BRT) == 1
    assert channel.sent[1].title == "Relatório diário da verificação de membros"
    assert "LeonamUchiha" in field(channel.sent[1], "Removidos")


@pytest.mark.asyncio
async def test_daily_report_is_published_even_when_nothing_happened():
    audit, _, channel = await make_audit()
    assert await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)) == 0
    assert await audit.tick(NINE_BRT) == 1
    assert field(channel.sent[0], "Removidos") == "Ninguém no período."
    assert await audit.tick(NINE_BRT + timedelta(minutes=1)) == 0


@pytest.mark.asyncio
async def test_without_channel_nothing_is_published_but_activity_is_kept():
    audit, repos, channel = await make_audit(cfg(channel_id=None))
    await audit.record_removal(await repos.get_config(1), link(), "perfil sem guilda", "nenhum cargo configurado")
    assert channel.sent == [] and await audit.tick(NINE_BRT) == 0
    current = await repos.get_config(1)
    current.audit = AuditConfig(30, 9, 0)
    await repos.save_config(current)
    assert await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)) == 0
    assert await audit.tick(NINE_BRT) == 1
    assert "LeonamUchiha" in field(channel.sent[-1], "Removidos")


@pytest.mark.asyncio
async def test_failed_report_is_retried_without_losing_history():
    audit, repos, channel = await make_audit()
    await audit.record_removal(await repos.get_config(1), link(), "perfil sem guilda", "cargo removido")
    assert len(channel.sent) == 1
    channel.failures = [http_error(500)]
    assert await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)) == 0
    assert await audit.tick(NINE_BRT) == 0
    retry_at = NINE_BRT + RETRY_AFTER_FAILURE
    assert await audit.tick(retry_at - timedelta(seconds=1)) == 0
    assert await audit.tick(retry_at) == 1
    assert channel.sent[-1].title.startswith("Relatório") and "LeonamUchiha" in field(channel.sent[-1], "Removidos")


@pytest.mark.asyncio
async def test_changing_the_hour_reschedules_without_publishing():
    audit, repos, _ = await make_audit(cfg(hour=9))
    assert await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)) == 0
    current = await repos.get_config(1)
    current.audit = AuditConfig(30, 18, 0)
    await repos.save_config(current)
    assert await audit.tick(NINE_BRT) == 0
    eighteen = datetime(2026, 10, 18, 21, 0, tzinfo=timezone.utc)
    assert await audit.tick(eighteen) == 1


@pytest.mark.asyncio
async def test_servers_do_not_share_audit_messages():
    repos = MemoryRepositories()
    await repos.save_config(ServerConfig(1, GuildLink(GUILD, "A"), audit=AuditConfig(31, 9, 0)))
    await repos.save_config(ServerConfig(2, GuildLink(GUILD, "B"), audit=AuditConfig(32, 9, 0)))
    channels = {31: FakeChannel(31), 32: FakeChannel(32)}
    audit = MembershipAudit(FakeBot(channels), repos)
    await audit.record_removal(await repos.get_config(1), MemberLink(1, 500, "p1", "Alpha", GUILD), "saiu", "cargo removido")
    assert len(channels[31].sent) == 1 and channels[32].sent == []
    await audit.tick(datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc))
    await audit.tick(NINE_BRT)
    assert len(channels[31].sent) == 2 and len(channels[32].sent) == 1


@pytest.mark.asyncio
async def test_member_sync_announces_a_removal_in_the_configured_channel():
    sync, albion, repos, member = await synced_setup()
    current = await repos.get_config(1)
    current.audit = AuditConfig(30, 9, 0)
    await repos.save_config(current)
    channel = FakeChannel(30)
    sync.bot.channels = {30: channel}
    sync.audit = MembershipAudit(sync.bot, repos)
    albion.roster[GUILD] = ["outro"]
    albion.players["p1"]["GuildId"] = "OTHER"
    stats = await sync.run_once()
    assert stats.left == 1 and len(channel.sent) == 1
    assert channel.sent[0].title == "Membro removido do registro" and field(channel.sent[0], "Personagem") == leonam().name
    assert not (await repos.get_link(1, 500)).active
    assert ROLE not in member.roles
