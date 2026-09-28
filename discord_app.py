"""Discord slash-command UI and shared scheduler for 정훈봇."""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
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
JOB_DIR = core.data_dir() / "guild-jobs"  # Legacy schedules, read only during migration.
SHARED_JOB_PATH = core.data_dir() / "scheduled.sqlite3"
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
                    rarity TEXT NOT NULL DEFAULT 'N',
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    message_id TEXT
                )"""
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(previews)")}
            if "rarity" not in columns:
                connection.execute("ALTER TABLE previews ADD COLUMN rarity TEXT NOT NULL DEFAULT 'N'")

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
        image_path: Path, dialogue: str, slot: str, rarity: str = "N",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO previews
                (preview_id, guild_id, owner_id, image_path, dialogue, slot, rarity, created_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready')""",
                (preview_id, str(guild_id), str(owner_id), str(image_path), dialogue, slot,
                 rarity, datetime.now(timezone.utc).isoformat()),
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

    def recent_dialogues(self, guild_id: int, limit: int = 8) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT dialogue FROM previews WHERE guild_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(guild_id), limit),
            ).fetchall()
        return [row[0] for row in rows]

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


class BroadcastStore(core.JobStore):
    """One generated job per slot, with a separate delivery state per server."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path)
        with self.connection:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS deliveries (
                    day TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_id TEXT,
                    error TEXT,
                    PRIMARY KEY (day, slot, guild_id)
                )"""
            )

    def claim_delivery(self, day: str, slot: str, guild_id: int) -> bool:
        with self.connection:
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO deliveries (day, slot, guild_id, status) "
                "VALUES (?, ?, ?, 'posting')", (day, slot, str(guild_id)),
            )
            if inserted.rowcount:
                return True
            resumed = self.connection.execute(
                "UPDATE deliveries SET status = 'posting' WHERE day = ? AND slot = ? "
                "AND guild_id = ? AND status = 'paused'", (day, slot, str(guild_id)),
            )
            return resumed.rowcount == 1

    def finish_delivery(
        self, day: str, slot: str, guild_id: int, status: str,
        message_id: str | None = None, error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET status = ?, message_id = ?, error = ? "
                "WHERE day = ? AND slot = ? AND guild_id = ? AND status = 'posting'",
                (status, message_id, error, day, slot, str(guild_id)),
            )

    def has_sent_delivery(self, day: str, slot: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE day = ? AND slot = ? AND status = 'sent' LIMIT 1",
            (day, slot),
        ).fetchone() is not None

    def reset_posting_delivery(self, day: str, slot: str, guild_id: int) -> bool:
        with self.connection:
            result = self.connection.execute(
                "UPDATE deliveries SET status = 'paused', error = NULL "
                "WHERE day = ? AND slot = ? AND guild_id = ? AND status = 'posting'",
                (day, slot, str(guild_id)),
            )
        return result.rowcount == 1


def shared_store() -> BroadcastStore:
    return BroadcastStore(SHARED_JOB_PATH)


def migrate_legacy_jobs(store: BroadcastStore) -> None:
    """Preserve existing reservations and sent/uncertain posts on first upgrade."""
    if store.connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() or not JOB_DIR.is_dir():
        return
    paths = sorted(path for path in JOB_DIR.glob("*.sqlite3") if path.stem.isdecimal())
    if not paths:
        return
    legacy_rows = []
    for path in paths:
        with closing(sqlite3.connect(path)) as legacy:
            legacy.row_factory = sqlite3.Row
            rows = legacy.execute("SELECT * FROM jobs").fetchall()
            if rows:
                legacy_rows.append((path, rows))
    if not legacy_rows:
        return
    # Use a database with the newest reservation rather than an empty or stale
    # server database. All future servers then follow that one shared clock.
    _, rows = max(legacy_rows, key=lambda item: max(row["scheduled_at"] for row in item[1]))
    with store.connection:
        for row in rows:
            image_path = row["image_path"]
            status = row["status"]
            if status in {"ready", "posting"} and (not image_path or not Path(image_path).is_file()):
                status = "queued"
            elif status == "posting":
                status = "ready"
            store.connection.execute(
                """INSERT OR IGNORE INTO jobs
                (day, slot, scheduled_at, window_spec, status, image_path, caption,
                 dialogue, category, creative_mode, rarity, message_id, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["day"], row["slot"], row["scheduled_at"], row["window_spec"],
                 status, image_path, row["caption"], row["dialogue"], row["category"],
                 row["creative_mode"] if "creative_mode" in row.keys() else None,
                 row["rarity"] if "rarity" in row.keys() else None,
                 row["message_id"], row["error"]),
            )
        for path, legacy_jobs in legacy_rows:
            for row in legacy_jobs:
                if row["status"] in {"sent", "posting"}:
                    store.connection.execute(
                        "INSERT OR IGNORE INTO deliveries "
                        "(day, slot, guild_id, status, message_id) VALUES (?, ?, ?, ?, ?)",
                        (row["day"], row["slot"], path.stem, row["status"], row["message_id"]),
                    )


def upcoming_jobs(settings: core.Settings) -> list[sqlite3.Row]:
    now = datetime.now(settings.timezone)
    store = shared_store()
    try:
        migrate_legacy_jobs(store)
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
    store = shared_store()
    try:
        return list(store.connection.execute(
            "SELECT jobs.day, jobs.slot, jobs.scheduled_at, deliveries.status "
            "FROM deliveries JOIN jobs USING (day, slot) WHERE deliveries.guild_id = ? "
            "AND deliveries.status IN ('sent', 'posting') "
            "ORDER BY jobs.scheduled_at DESC LIMIT 3", (str(guild_id),),
        ))
    finally:
        store.close()


def process_broadcast(
    registry: GuildRegistry, settings: core.Settings, now: datetime | None = None,
    generate=core.generate_meme, post=core.post_to_discord,
) -> None:
    local_now = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    store = shared_store()
    try:
        migrate_legacy_jobs(store)
        store.recover_generation()
        previous_day = local_now.date() - timedelta(days=1)
        store.ensure_day(previous_day, settings)
        store.ensure_day(local_now.date(), settings)
        active_guilds = registry.active_guilds()
        for job in [*store.jobs_for_day(previous_day), *store.jobs_for_day(local_now.date())]:
            scheduled = datetime.fromisoformat(job["scheduled_at"])
            day, slot, status = job["day"], job["slot"], job["status"]
            image_path = Path(job["image_path"]) if job["image_path"] else core.data_dir() / "output" / f"{day}-{slot}.png"
            dialogue = job["dialogue"] or job["caption"] or "정훈봇 짤"
            rarity = job["rarity"] or ("SR" if job["creative_mode"] == "legend" else "N")
            if status == "queued" and local_now > scheduled + core.MAX_LATE:
                store.transition(day, slot, "queued", "skipped", error="Missed posting grace period")
                continue
            if status == "queued" and active_guilds and local_now >= scheduled - core.PREPARE_AHEAD:
                if not store.transition(day, slot, "queued", "generating"):
                    continue
                try:
                    scenario = core.choose_scenario(
                        store.recent_categories(), job["creative_mode"] or "regular"
                    )
                    dialogue = generate(settings, slot, image_path, store.recent_dialogues(), scenario)
                    rarity = scenario.rarity
                    store.transition(
                        day, slot, "generating", "ready", image_path=str(image_path),
                        dialogue=dialogue, category=scenario.key, rarity=rarity,
                    )
                    status = "ready"
                except Exception as exc:
                    LOG.exception("Failed to generate shared photo for %s %s", day, slot)
                    store.transition(day, slot, "generating", "failed", error=str(exc)[:500])
                    continue
            if status != "ready" or local_now < scheduled:
                continue
            if local_now > scheduled + core.MAX_LATE:
                final = "sent" if store.has_sent_delivery(day, slot) else "skipped"
                store.transition(day, slot, "ready", final)
                continue
            for guild_id in active_guilds:
                if not store.claim_delivery(day, slot, guild_id):
                    continue
                current = registry.get(guild_id)
                if not current or not current["enabled"] or not current["channel_id"]:
                    store.finish_delivery(day, slot, guild_id, "paused")
                    continue
                destination = replace(
                    settings, discord_bot_token=settings.discord_bot_token,
                    discord_channel_id=current["channel_id"], discord_webhook_url=None,
                )
                try:
                    message_id = post(destination, image_path, slot, dialogue, rarity)
                    store.finish_delivery(day, slot, guild_id, "sent", message_id)
                except core.PostingPaused:
                    store.finish_delivery(day, slot, guild_id, "paused")
                except Exception as exc:
                    # The network request may have succeeded. Do not retry this guild blindly.
                    store.finish_delivery(day, slot, guild_id, "posting", error=type(exc).__name__)
                    LOG.error(
                        "Posting result is uncertain for %s %s guild %s (%s)",
                        day, slot, guild_id, type(exc).__name__,
                    )
    finally:
        store.close()


def is_operator(interaction: discord.Interaction, settings: core.Settings) -> bool:
    return bool(
        interaction.guild_id
        and (
            interaction.user.id in settings.operator_ids
            or (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.administrator
            )
        )
    )


async def reject_if_not_operator(interaction: discord.Interaction, settings: core.Settings) -> bool:
    if is_operator(interaction, settings):
        return False
    if interaction.response.is_done():
        await interaction.followup.send("서버 관리자 또는 허용된 사용자만 정훈봇 명령을 사용할 수 있어요.", ephemeral=True)
    else:
        await interaction.response.send_message("서버 관리자 또는 허용된 사용자만 정훈봇 명령을 사용할 수 있어요.", ephemeral=True)
    return True


def settings_embed(guild_id: int, registry: GuildRegistry, settings: core.Settings) -> discord.Embed:
    saved = registry.get(guild_id)
    channel_id = saved["channel_id"] if saved else None
    enabled = bool(saved and saved["enabled"])
    embed = discord.Embed(
        title="정훈봇 설정",
        description="아래 채널 선택 메뉴에서 업로드할 채널을 고르세요. 모든 서버가 같은 예약 사진을 받습니다.",
        color=discord.Color.green() if enabled else discord.Color.orange(),
    )
    embed.add_field(name="업로드 채널", value=f"<#{channel_id}>" if channel_id else "미등록", inline=True)
    embed.add_field(name="자동 발송", value="켜짐" if enabled else "꺼짐", inline=True)
    embed.add_field(name="참조 사진", value=f"매번 {settings.photo_count}장", inline=True)
    embed.add_field(
        name="시간대 · 발송 구간",
        value=f"{settings.timezone}\n밤 {core.window_spec(settings.windows[0])} · 낮 {core.window_spec(settings.windows[1])}",
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
        jobs = upcoming_jobs(settings)
        lines = []
        for row in jobs:
            planned = datetime.fromisoformat(row["scheduled_at"])
            label = "밤" if row["slot"] == "dawn" else "낮"
            mode = " · 레전드 도전" if row["creative_mode"] == "legend" else ""
            lines.append(f"{label}{mode} · {planned:%m/%d %H:%M} ({settings.timezone}) · <t:{int(planned.timestamp())}:R>")
        embed.add_field(name="다음 예약", value="\n".join(lines) if lines else "아직 없음", inline=False)
        status_labels = {"sent": "발송 완료", "posting": "게시 여부 확인 필요"}
        history = recent_jobs(guild_id)
        if history:
            embed.add_field(
                name="최근 회차",
                value="\n".join(
                    f"{row['day']} {'밤' if row['slot'] == 'dawn' else '낮'} · {status_labels[row['status']]}"
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
        if await reject_if_not_operator(interaction, panel.settings):
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
        if interaction.user.id == self.owner_id and is_operator(interaction, self.settings):
            return True
        await interaction.response.send_message("이 설정 화면은 호출한 관리자 또는 허용 사용자만 사용할 수 있어요.", ephemeral=True)
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


class PreviewView(discord.ui.LayoutView):
    def __init__(
        self, registry: GuildRegistry, previews: PreviewStore, settings: core.Settings,
        preview_id: str, guild_id: int, owner_id: int, image_path: Path,
        dialogue: str, slot: str, rarity: str = "N",
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
        self.rarity = rarity
        card = discord.ui.Container(accent_color=core.RARITY_COLORS[rarity])
        card.add_item(discord.ui.TextDisplay(dialogue))
        card.add_item(discord.ui.MediaGallery().add_item(media=f"attachment://{image_path.name}"))
        card.add_item(discord.ui.TextDisplay(core.rarity_line(rarity, settings.card_display_name)))
        self.add_item(card)
        self.add_item(discord.ui.ActionRow(PublishButton(preview_id)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and interaction.guild_id == self.guild_id and is_operator(interaction, self.settings):
            return True
        await interaction.response.send_message("미리보기를 만든 관리자 또는 허용 사용자만 게시할 수 있어요.", ephemeral=True)
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
                core.post_to_discord, destination, self.image_path, self.slot,
                self.dialogue, self.rarity,
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
        matches.sort(key=lambda scenario: core.SCENARIO_WEIGHTS[scenario.key], reverse=True)
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
        )
        self.registry = registry
        self.previews = previews
        self.settings = settings
        self._preview_locks: dict[int, asyncio.Lock] = {}

    @app_commands.command(name="설정", description="업로드 채널을 선택하고 자동 발송을 관리합니다")
    async def configure(self, interaction: discord.Interaction) -> None:
        if await reject_if_not_operator(interaction, self.settings):
            return
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            embed=settings_embed(interaction.guild_id, self.registry, self.settings),
            view=SettingsView(self.registry, self.settings, interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )

    @app_commands.command(name="상태", description="업로드 채널, 다음 예약, 최근 발송을 확인합니다")
    async def status(self, interaction: discord.Interaction) -> None:
        if await reject_if_not_operator(interaction, self.settings):
            return
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            embed=status_embed(interaction.guild_id, self.registry, self.settings),
            ephemeral=True,
        )

    @app_commands.command(name="미리보기", description="짤을 한 장 생성합니다 · OpenAI API 비용 발생")
    @app_commands.describe(유형="비워두면 무작위 유형으로 생성합니다")
    @app_commands.describe(분위기="사진의 과장 정도", 시간대="사진의 낮·밤 조명만 선택", 이슈="최근 밈 패러디 후보를 확인할지", 목표="일반 짤 또는 레전드 짤 기획", 등급="비워두면 확률에 따라 추첨")
    @app_commands.autocomplete(유형=scenario_autocomplete)
    @app_commands.choices(
        분위기=[
            app_commands.Choice(name="자동", value="auto"),
            app_commands.Choice(name="일상적인 사진", value="natural"),
            app_commands.Choice(name="대놓고 웃긴 사진", value="bold"),
            app_commands.Choice(name="초현실적인 사진", value="surreal"),
            app_commands.Choice(name="과몰입 판타지·무대", value="cinematic"),
        ],
        시간대=[
            app_commands.Choice(name="낮 (11:30~13:30)", value="lunch"),
            app_commands.Choice(name="밤 (22:00~04:00)", value="dawn"),
        ],
        이슈=[
            app_commands.Choice(name="자동", value="auto"),
            app_commands.Choice(name="이슈 없이", value="off"),
            app_commands.Choice(name="최근 밈 후보 확인", value="try"),
        ],
        목표=[
            app_commands.Choice(name="일반 짤", value="regular"),
            app_commands.Choice(name="레전드 짤 도전", value="legend"),
        ],
        등급=[
            app_commands.Choice(name="N · 노멀", value="N"),
            app_commands.Choice(name="R · 레어", value="R"),
            app_commands.Choice(name="SR · 슈퍼 레어", value="SR"),
            app_commands.Choice(name="SSR · 초특급 레어", value="SSR"),
            app_commands.Choice(name="UR · 울트라 레어", value="UR"),
        ],
    )
    async def preview(
        self, interaction: discord.Interaction, 유형: str | None = None,
        분위기: app_commands.Choice[str] | None = None,
        시간대: app_commands.Choice[str] | None = None,
        이슈: app_commands.Choice[str] | None = None,
        목표: app_commands.Choice[str] | None = None,
        등급: app_commands.Choice[str] | None = None,
    ) -> None:
        if await reject_if_not_operator(interaction, self.settings):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        selected = next(
            (scenario for scenario in core.SCENARIOS if 유형 in (scenario.key, scenario.name)), None
        ) if 유형 else None
        if 유형 and selected is None:
            await interaction.response.send_message("유형 목록에서 선택해 주세요.", ephemeral=True)
            return
        mode = 목표.value if 목표 else "regular"
        selected_rarity = 등급.value if 등급 else None
        if selected_rarity and selected_rarity not in core.RARITY_WEIGHTS[mode]:
            await interaction.response.send_message(
                "일반 짤은 N~SSR, 레전드 짤은 SR~UR 등급을 선택할 수 있어요.", ephemeral=True
            )
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
                store = shared_store()
                try:
                    migrate_legacy_jobs(store)
                    scenario = (
                        core.with_rarity(selected, mode, selected_rarity)
                        if selected else core.choose_scenario(store.recent_categories(), mode, selected_rarity)
                    )
                    previous = [
                        *self.previews.recent_dialogues(guild_id),
                        *store.recent_dialogues(),
                    ][:12]
                finally:
                    store.close()
                dialogue = await asyncio.to_thread(
                    core.generate_meme, self.settings, slot, path, previous, scenario,
                    style, trend_mode,
                )
                self.previews.add(
                    preview_id, guild_id, interaction.user.id, path, dialogue, slot, scenario.rarity,
                )
                await interaction.followup.send(
                    file=discord.File(path),
                    view=PreviewView(
                        self.registry, self.previews, self.settings, preview_id,
                        guild_id, interaction.user.id, path, dialogue, slot, scenario.rarity,
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
        self._synced_guilds: set[int] = set()

    async def setup_hook(self) -> None:
        for row in self.previews.recent():
            self.add_view(PreviewView(
                self.registry, self.previews, self.settings, row["preview_id"],
                int(row["guild_id"]), int(row["owner_id"]), Path(row["image_path"]),
                row["dialogue"], row["slot"], row["rarity"],
            ))
        await self.tree.sync()
        self.scheduler.start()

    async def on_ready(self) -> None:
        LOG.info(
            "Logged in as %s; joined %s servers, automatic posting enabled in %s",
            self.user, len(self.guilds), len(self.registry.active_guilds()),
        )
        for guild in self.guilds:
            await self.sync_guild_commands(guild)

    async def sync_guild_commands(self, guild: discord.Guild) -> None:
        if guild.id in self._synced_guilds:
            return
        self.tree.copy_global_to(guild=guild)
        try:
            commands = await self.tree.sync(guild=guild)
        except discord.HTTPException:
            LOG.exception("Could not sync slash commands for guild %s", guild.id)
        else:
            self._synced_guilds.add(guild.id)
            LOG.info("Synced %s slash commands for guild %s", len(commands), guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild_commands(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._synced_guilds.discard(guild.id)
        self.registry.set_enabled(guild.id, False)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        saved = self.registry.get(channel.guild.id)
        if saved and saved["channel_id"] == str(channel.id):
            self.registry.set_enabled(channel.guild.id, False)

    @tasks.loop(seconds=30)
    async def scheduler(self) -> None:
        try:
            await asyncio.to_thread(process_broadcast, self.registry, self.settings)
        except Exception:
            LOG.exception("Shared scheduler error")

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
        print("No configured servers. An admin or allowed operator can register a channel with /정훈봇 설정.")
    for job in upcoming_jobs(settings):
        print(f"Shared {job['slot']}: {job['scheduled_at']} [{job['status']}, {job['creative_mode'] or 'regular'}]")
    for guild_id in configured:
        saved = registry.get(guild_id)
        state = "enabled" if saved["enabled"] else "paused"
        print(f"Server {guild_id} -> channel {saved['channel_id']} [{state}]")
        for job in recent_jobs(guild_id):
            print(f"  recent {job['day']} {job['slot']}: [{job['status']}]")
