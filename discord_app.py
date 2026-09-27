"""Discord slash-command UI and per-server scheduler for 정훈봇."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import secrets
import sqlite3
from typing import Iterator

import discord
from discord import app_commands
from discord.ext import tasks

import bot as core


LOG = logging.getLogger("compy_bot.discord")
REGISTRY_PATH = core.data_dir() / "guilds.sqlite3"
JOB_DIR = core.data_dir() / "guild-jobs"
PREVIEW_PATH = core.data_dir() / "previews.sqlite3"


class GuildRegistry:
    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id TEXT PRIMARY KEY,
                    channel_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0
                )"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, guild_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()

    def set_channel(self, guild_id: int, channel_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO guild_settings (guild_id, channel_id, enabled) VALUES (?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, enabled = 1""",
                (str(guild_id), str(channel_id)),
            )

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE guild_settings SET enabled = ? WHERE guild_id = ? AND channel_id IS NOT NULL",
                (int(enabled), str(guild_id)),
            )

    def active_guilds(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT guild_id FROM guild_settings WHERE enabled = 1 AND channel_id IS NOT NULL"
            ).fetchall()
        return sorted(int(row[0]) for row in rows)

    def configured_guilds(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT guild_id FROM guild_settings").fetchall()
        return sorted(int(row[0]) for row in rows)


class PreviewStore:
    """Keep preview button details across bot restarts and prevent duplicate posts."""

    def __init__(self, path: Path = PREVIEW_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS previews (
                    preview_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    dialogue TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    message_id TEXT
                )"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(
        self, preview_id: str, guild_id: int, owner_id: int,
        image_path: Path, dialogue: str, slot: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO previews
                (preview_id, guild_id, owner_id, image_path, dialogue, slot, created_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ready')""",
                (preview_id, str(guild_id), str(owner_id), str(image_path), dialogue, slot,
                 datetime.now(timezone.utc).isoformat()),
            )

    def get(self, preview_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM previews WHERE preview_id = ?", (preview_id,)
            ).fetchone()

    def recent(self) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self._connect() as connection:
            return list(connection.execute(
                "SELECT * FROM previews WHERE created_at >= ?", (cutoff,)
            ))

    def claim(self, preview_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE previews SET state = 'posting' WHERE preview_id = ? AND state = 'ready'",
                (preview_id,),
            )
        return result.rowcount == 1

    def finish(self, preview_id: str, state: str, message_id: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE previews SET state = ?, message_id = ? WHERE preview_id = ? AND state = 'posting'",
                (state, message_id, preview_id),
            )


def job_store(guild_id: int) -> core.JobStore:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    return core.JobStore(JOB_DIR / f"{guild_id}.sqlite3")


def upcoming_jobs(guild_id: int, settings: core.Settings) -> list[sqlite3.Row]:
    now = datetime.now(settings.timezone)
    store = job_store(guild_id)
    try:
        rows: list[sqlite3.Row] = []
        for offset in (-1, 0, 1):
            day = now.date() + timedelta(days=offset)
            store.ensure_day(day, settings)
            rows.extend(store.jobs_for_day(day))
        return sorted(
            (
                row for row in rows
                if datetime.fromisoformat(row["scheduled_at"]) >= now
                and row["status"] in {"queued", "generating", "ready"}
            ),
            key=lambda row: row["scheduled_at"],
        )[:2]
    finally:
        store.close()


def recent_jobs(guild_id: int) -> list[sqlite3.Row]:
    store = job_store(guild_id)
    try:
        return list(store.connection.execute(
            "SELECT * FROM jobs WHERE status IN ('sent', 'failed', 'posting') "
            "ORDER BY scheduled_at DESC LIMIT 3"
        ))
    finally:
        store.close()


def process_guild(registry: GuildRegistry, settings: core.Settings, guild_id: int) -> None:
    store = job_store(guild_id)

    def post(_settings: core.Settings, image_path: Path, slot: str, dialogue: str) -> str:
        current = registry.get(guild_id)
        if not current or not current["enabled"] or not current["channel_id"]:
            raise core.PostingPaused("Server posting was paused")
        destination = replace(
            settings,
            discord_bot_token=settings.discord_bot_token,
            discord_channel_id=current["channel_id"],
            discord_webhook_url=None,
        )
        return core.post_to_discord(destination, image_path, slot, dialogue)

    try:
        store.recover_generation()
        core.process_jobs(store, settings, datetime.now(settings.timezone), post=post)
    finally:
        store.close()


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild_id
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


async def reject_if_not_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return False
    if interaction.response.is_done():
        await interaction.followup.send("서버 관리자만 정훈봇을 설정할 수 있어요.", ephemeral=True)
    else:
        await interaction.response.send_message("서버 관리자만 정훈봇을 설정할 수 있어요.", ephemeral=True)
    return True


def settings_embed(guild_id: int, registry: GuildRegistry, settings: core.Settings) -> discord.Embed:
    saved = registry.get(guild_id)
    channel_id = saved["channel_id"] if saved else None
    enabled = bool(saved and saved["enabled"])
    embed = discord.Embed(
        title="정훈봇 설정",
        description="아래 채널 선택 메뉴에서 업로드할 채널을 고르세요. 선택하면 자동 발송이 켜집니다.",
        color=discord.Color.green() if enabled else discord.Color.orange(),
    )
    embed.add_field(name="업로드 채널", value=f"<#{channel_id}>" if channel_id else "미등록", inline=True)
    embed.add_field(name="자동 발송", value="켜짐" if enabled else "꺼짐", inline=True)
    embed.add_field(name="참조 사진", value=f"매번 {settings.photo_count}장", inline=True)
    embed.add_field(
        name="시간대 · 발송 구간",
        value=f"{settings.timezone}\n밤 {core.window_spec(settings.windows[0])} · 점심 {core.window_spec(settings.windows[1])}",
        inline=False,
    )
    if not enabled and channel_id:
        embed.set_footer(text="자동 발송이 꺼져 있습니다. 다시 켜면 지나간 회차는 발송하지 않습니다.")
    return embed


def status_embed(guild_id: int, registry: GuildRegistry, settings: core.Settings) -> discord.Embed:
    embed = settings_embed(guild_id, registry, settings)
    embed.title = "정훈봇 상태"
    embed.description = "현재 설정과 발송 내역입니다. 설정 변경은 `/정훈봇 설정`에서 할 수 있어요."
    saved = registry.get(guild_id)
    channel_id = saved["channel_id"] if saved else None
    if channel_id:
        jobs = upcoming_jobs(guild_id, settings)
        lines = []
        for row in jobs:
            planned = datetime.fromisoformat(row["scheduled_at"])
            label = "밤" if row["slot"] == "dawn" else "점심"
            lines.append(f"{label} · {planned:%m/%d %H:%M} ({settings.timezone}) · <t:{int(planned.timestamp())}:R>")
        embed.add_field(name="다음 예약", value="\n".join(lines) if lines else "아직 없음", inline=False)
        status_labels = {"sent": "발송 완료", "failed": "생성 실패", "posting": "게시 여부 확인 필요"}
        history = recent_jobs(guild_id)
        if history:
            embed.add_field(
                name="최근 회차",
                value="\n".join(
                    f"{row['day']} {'밤' if row['slot'] == 'dawn' else '점심'} · {status_labels[row['status']]}"
                    for row in history
                ),
                inline=False,
            )
    return embed


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="정훈봇이 사진을 올릴 채널 선택",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: SettingsView = self.view  # type: ignore[assignment]
        if await reject_if_not_admin(interaction):
            return
        assert interaction.guild is not None
        selected = interaction.guild.get_channel(self.values[0].id)
        member = interaction.guild.me
        if not isinstance(selected, discord.TextChannel) or member is None:
            await interaction.response.send_message("채널 권한을 확인할 수 없어요. 다른 채널을 선택해 주세요.", ephemeral=True)
            return
        permissions = selected.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages and permissions.attach_files):
            await interaction.response.send_message(
                "정훈봇에 이 채널의 채널 보기·메시지 보내기·파일 첨부 권한을 주세요.", ephemeral=True
            )
            return
        panel.registry.set_channel(interaction.guild_id, selected.id)
        await interaction.response.edit_message(
            embed=settings_embed(interaction.guild_id, panel.registry, panel.settings),
            view=SettingsView(panel.registry, panel.settings, interaction.user.id, interaction.guild_id),
        )


class SettingsView(discord.ui.View):
    def __init__(self, registry: GuildRegistry, settings: core.Settings, owner_id: int, guild_id: int):
        super().__init__(timeout=600)
        self.registry = registry
        self.settings = settings
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.add_item(ChannelPicker())
        saved = registry.get(guild_id)
        enabled = bool(saved and saved["enabled"])
        self.toggle.label = "자동 발송 끄기" if enabled else "자동 발송 켜기"  # type: ignore[attr-defined]
        self.toggle.style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success  # type: ignore[attr-defined]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and is_admin(interaction):
            return True
        await interaction.response.send_message("이 설정 화면은 호출한 서버 관리자만 사용할 수 있어요.", ephemeral=True)
        return False

    @discord.ui.button(label="자동 발송 켜기/끄기", style=discord.ButtonStyle.primary, row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild_id is not None
        saved = self.registry.get(interaction.guild_id)
        if not saved or not saved["channel_id"]:
            await interaction.response.send_message("먼저 업로드 채널을 선택해 주세요.", ephemeral=True)
            return
        self.registry.set_enabled(interaction.guild_id, not bool(saved["enabled"]))
        await interaction.response.edit_message(
            embed=settings_embed(interaction.guild_id, self.registry, self.settings),
            view=SettingsView(self.registry, self.settings, self.owner_id, interaction.guild_id),
        )

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild_id is not None
        await interaction.response.edit_message(
            embed=settings_embed(interaction.guild_id, self.registry, self.settings),
            view=SettingsView(self.registry, self.settings, self.owner_id, interaction.guild_id),
        )


class PublishButton(discord.ui.Button):
    def __init__(self, preview_id: str):
        super().__init__(
            label="이대로 채널에 게시",
            style=discord.ButtonStyle.success,
            custom_id=f"junghoon:publish:{preview_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: PreviewView = self.view  # type: ignore[assignment]
        await panel.publish(interaction, self)


class PreviewView(discord.ui.View):
    def __init__(
        self, registry: GuildRegistry, previews: PreviewStore, settings: core.Settings,
        preview_id: str, guild_id: int, owner_id: int, image_path: Path,
        dialogue: str, slot: str,
    ):
        super().__init__(timeout=None)
        self.registry = registry
        self.previews = previews
        self.settings = settings
        self.preview_id = preview_id
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.image_path = image_path
        self.dialogue = dialogue
        self.slot = slot
        self.add_item(PublishButton(preview_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and interaction.guild_id == self.guild_id and is_admin(interaction):
            return True
        await interaction.response.send_message("미리보기를 만든 서버 관리자만 게시할 수 있어요.", ephemeral=True)
        return False

    async def publish(self, interaction: discord.Interaction, button: PublishButton) -> None:
        # Acknowledge the button immediately; the Discord upload may take much longer.
        await interaction.response.defer()
        saved = self.registry.get(self.guild_id)
        if not saved or not saved["channel_id"]:
            await interaction.followup.send("먼저 /정훈봇 설정에서 채널을 선택해 주세요.", ephemeral=True)
            return
        if not self.image_path.is_file():
            await interaction.followup.send("미리보기 파일을 찾을 수 없어요. 새 미리보기를 만들어 주세요.", ephemeral=True)
            return
        if not self.previews.claim(self.preview_id):
            previous = self.previews.get(self.preview_id)
            if previous and previous["state"] == "sent":
                result = "이미 채널에 게시한 미리보기예요."
            else:
                result = "이 미리보기의 게시 요청이 이미 처리 중이거나 결과 확인이 필요해요. 채널을 확인해 주세요."
            await interaction.followup.send(result, ephemeral=True)
            return
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            LOG.warning("Could not disable preview button for guild %s", self.guild_id)
        destination = replace(
            self.settings,
            discord_bot_token=self.settings.discord_bot_token,
            discord_channel_id=saved["channel_id"],
            discord_webhook_url=None,
        )
        try:
            message_id = await asyncio.to_thread(
                core.post_to_discord, destination, self.image_path, self.slot, self.dialogue
            )
        except Exception as exc:
            self.previews.finish(self.preview_id, "uncertain")
            LOG.error("Preview posting result is uncertain for guild %s (%s)", self.guild_id, type(exc).__name__)
            await interaction.followup.send(
                "게시 결과를 확인할 수 없어요. 채널을 확인한 뒤 새 미리보기를 만들어 주세요.", ephemeral=True
            )
            return
        self.previews.finish(self.preview_id, "sent", message_id)
        await interaction.followup.send(
            f"<#{saved['channel_id']}>에 게시했어요. 메시지 ID: `{message_id}`", ephemeral=True
        )


async def scenario_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    query = current.casefold().strip()
    matches = [
        scenario for scenario in core.SCENARIOS
        if query in scenario.name.casefold() or query in scenario.key.casefold()
    ]
    if not query:
        matches.sort(key=lambda scenario: core.FAVORITE_WEIGHTS.get(scenario.key, 1), reverse=True)
    return [
        app_commands.Choice(name=scenario.name, value=scenario.key)
        for scenario in matches[:25]
    ]


class JunghoonCommands(app_commands.Group):
    def __init__(self, registry: GuildRegistry, previews: PreviewStore, settings: core.Settings):
        super().__init__(
            name="정훈봇",
            description="정훈봇 설정, 상태, 미리보기",
            guild_only=True,
            default_permissions=discord.Permissions(administrator=True),
        )
        self.registry = registry
        self.previews = previews
        self.settings = settings
        self._preview_locks: dict[int, asyncio.Lock] = {}

    @app_commands.command(name="설정", description="업로드 채널을 선택하고 자동 발송을 관리합니다")
    async def configure(self, interaction: discord.Interaction) -> None:
        if await reject_if_not_admin(interaction):
            return
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            embed=settings_embed(interaction.guild_id, self.registry, self.settings),
            view=SettingsView(self.registry, self.settings, interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )

    @app_commands.command(name="상태", description="업로드 채널, 다음 예약, 최근 발송을 확인합니다")
    async def status(self, interaction: discord.Interaction) -> None:
        if await reject_if_not_admin(interaction):
            return
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            embed=status_embed(interaction.guild_id, self.registry, self.settings),
            ephemeral=True,
        )

    @app_commands.command(name="미리보기", description="짤을 한 장 생성합니다 · OpenAI API 비용 발생")
    @app_commands.describe(유형="비워두면 무작위 유형으로 생성합니다")
    @app_commands.describe(분위기="사진의 과장 정도", 시간대="대사의 시간대", 이슈="최근 밈 패러디 후보를 확인할지")
    @app_commands.autocomplete(유형=scenario_autocomplete)
    @app_commands.choices(
        분위기=[
            app_commands.Choice(name="자동", value="auto"),
            app_commands.Choice(name="일상적인 사진", value="natural"),
            app_commands.Choice(name="대놓고 웃긴 사진", value="bold"),
            app_commands.Choice(name="초현실적인 사진", value="surreal"),
        ],
        시간대=[
            app_commands.Choice(name="점심", value="lunch"),
            app_commands.Choice(name="새벽", value="dawn"),
        ],
        이슈=[
            app_commands.Choice(name="자동", value="auto"),
            app_commands.Choice(name="이슈 없이", value="off"),
            app_commands.Choice(name="최근 밈 후보 확인", value="try"),
        ],
    )
    async def preview(
        self, interaction: discord.Interaction, 유형: str | None = None,
        분위기: app_commands.Choice[str] | None = None,
        시간대: app_commands.Choice[str] | None = None,
        이슈: app_commands.Choice[str] | None = None,
    ) -> None:
        if await reject_if_not_admin(interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        selected = next(
            (scenario for scenario in core.SCENARIOS if 유형 in (scenario.key, scenario.name)), None
        ) if 유형 else None
        if 유형 and selected is None:
            await interaction.response.send_message("유형 목록에서 선택해 주세요.", ephemeral=True)
            return
        lock = self._preview_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message("이 서버에서 이미 미리보기를 만드는 중이에요.", ephemeral=True)
            return
        async with lock:
            await interaction.response.defer(ephemeral=True, thinking=True)
            preview_id = secrets.token_hex(12)
            path = core.data_dir() / "output" / f"preview-{guild_id}-{preview_id}.png"
            slot = 시간대.value if 시간대 else "lunch"
            style = 분위기.value if 분위기 else "auto"
            trend_mode = 이슈.value if 이슈 else "auto"
            try:
                store = job_store(guild_id)
                try:
                    scenario = selected or core.choose_scenario(store.recent_categories())
                    previous = store.recent_dialogues()
                finally:
                    store.close()
                dialogue = await asyncio.to_thread(
                    core.generate_meme, self.settings, slot, path, previous, scenario,
                    style, trend_mode,
                )
                self.previews.add(preview_id, guild_id, interaction.user.id, path, dialogue, slot)
                await interaction.followup.send(
                    content=f"**유형:** {scenario.name}\n**정훈봇 대사:** {dialogue}",
                    file=discord.File(path),
                    view=PreviewView(
                        self.registry, self.previews, self.settings, preview_id,
                        guild_id, interaction.user.id, path, dialogue, slot,
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                LOG.exception("Failed to generate preview for guild %s", guild_id)
                await interaction.followup.send("미리보기를 만들지 못했어요. API 키와 모델 설정을 확인해 주세요.", ephemeral=True)


class JunghoonClient(discord.Client):
    def __init__(
        self, settings: core.Settings, registry: GuildRegistry,
        previews: PreviewStore | None = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        super().__init__(intents=intents)
        self.settings = settings
        self.registry = registry
        self.previews = previews or PreviewStore()
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(JunghoonCommands(registry, self.previews, settings))

    async def setup_hook(self) -> None:
        for row in self.previews.recent():
            self.add_view(PreviewView(
                self.registry, self.previews, self.settings, row["preview_id"],
                int(row["guild_id"]), int(row["owner_id"]), Path(row["image_path"]),
                row["dialogue"], row["slot"],
            ))
        await self.tree.sync()
        self.scheduler.start()

    async def on_ready(self) -> None:
        LOG.info(
            "Logged in as %s; joined %s servers, automatic posting enabled in %s",
            self.user, len(self.guilds), len(self.registry.active_guilds()),
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.registry.set_enabled(guild.id, False)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        saved = self.registry.get(channel.guild.id)
        if saved and saved["channel_id"] == str(channel.id):
            self.registry.set_enabled(channel.guild.id, False)

    @tasks.loop(seconds=30)
    async def scheduler(self) -> None:
        for guild_id in self.registry.active_guilds():
            try:
                await asyncio.to_thread(process_guild, self.registry, self.settings, guild_id)
            except Exception:
                LOG.exception("Scheduler error for guild %s", guild_id)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()


def run_bot(settings: core.Settings) -> None:
    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required for slash commands; put a freshly reset token in .env")
    client = JunghoonClient(settings, GuildRegistry())
    client.run(settings.discord_bot_token, log_handler=None)


def print_plan(settings: core.Settings) -> None:
    registry = GuildRegistry()
    configured = registry.configured_guilds()
    if not configured:
        print("No configured servers. An administrator can register a channel with /정훈봇 설정.")
    for guild_id in configured:
        saved = registry.get(guild_id)
        state = "enabled" if saved["enabled"] else "paused"
        print(f"Server {guild_id} -> channel {saved['channel_id']} [{state}]")
        for job in upcoming_jobs(guild_id, settings):
            print(f"  {job['slot']}: {job['scheduled_at']} [{job['status']}]")
        for job in recent_jobs(guild_id):
            print(f"  recent {job['day']} {job['slot']}: [{job['status']}]")
