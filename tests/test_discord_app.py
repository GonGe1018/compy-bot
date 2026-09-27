import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
import discord
import discord_app


class GuildConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = discord_app.GuildRegistry(self.root / "guilds.sqlite3")
        self.previews = discord_app.PreviewStore(self.root / "previews.sqlite3")
        self.settings = bot.Settings(
            timezone=ZoneInfo("Asia/Seoul"),
            windows=(
                bot.parse_window("dawn", "22:00-04:00"),
                bot.parse_window("lunch", "11:30-13:30"),
            ),
            image_model="gpt-image-2",
            image_quality="low",
            image_size="1024x1024",
            prompt_model="gpt-6-luna",
            photo_count=2,
            openai_key=None,
            discord_bot_token="test-token",
            discord_channel_id=None,
            discord_webhook_url=None,
        )

    def test_channel_registration_is_per_server_and_can_be_paused(self):
        self.registry.set_channel(101, 1001)
        self.registry.set_channel(202, 2002)
        self.assertEqual(self.registry.active_guilds(), [101, 202])
        self.registry.set_channel(101, 1003)
        self.registry.set_enabled(202, False)
        self.assertEqual(self.registry.get(101)["channel_id"], "1003")
        self.assertEqual(self.registry.get(202)["channel_id"], "2002")
        self.assertEqual(self.registry.active_guilds(), [101])

    def test_native_slash_group_and_admin_settings_panel_are_registered(self):
        client = discord_app.JunghoonClient(self.settings, self.registry, self.previews)
        group = client.tree.get_commands()[0]
        self.assertEqual(group.name, "정훈봇")
        self.assertEqual([command.name for command in group.commands], ["설정", "상태", "미리보기"])
        self.assertIsNone(group.default_permissions)
        self.assertEqual(
            [parameter.name for parameter in group.get_command("미리보기").parameters],
            ["유형", "분위기", "시간대", "이슈", "목표"],
        )
        panel = discord_app.SettingsView(self.registry, self.settings, owner_id=1, guild_id=101)
        self.assertTrue(any(isinstance(item, discord_app.ChannelPicker) for item in panel.children))
        self.assertEqual(sum(getattr(item, "label", None) is not None for item in panel.children), 2)

    def test_whitelisted_user_can_use_commands_without_admin_permission(self):
        allowed = SimpleNamespace(guild_id=101, user=SimpleNamespace(id=277763680022560768))
        denied = SimpleNamespace(guild_id=101, user=SimpleNamespace(id=999))
        self.assertTrue(discord_app.is_operator(allowed, self.settings))
        self.assertFalse(discord_app.is_operator(denied, self.settings))
        self.assertFalse(discord_app.is_operator(SimpleNamespace(guild_id=None, user=allowed.user), self.settings))
        panel = discord_app.SettingsView(self.registry, self.settings, owner_id=allowed.user.id, guild_id=101)
        allowed.response = SimpleNamespace(send_message=AsyncMock())
        denied.response = SimpleNamespace(send_message=AsyncMock())
        self.assertTrue(asyncio.run(panel.interaction_check(allowed)))
        self.assertFalse(asyncio.run(panel.interaction_check(denied)))

    def test_preview_button_survives_reload_and_posts_only_once(self):
        image_path = self.root / "preview.png"
        image_path.write_bytes(b"image")
        self.registry.set_channel(101, 1001)
        self.previews.add("preview123", 101, 77, image_path, "정훈 대사", "dawn")
        row = discord_app.PreviewStore(self.root / "previews.sqlite3").recent()[0]
        view = discord_app.PreviewView(
            self.registry, self.previews, self.settings, row["preview_id"],
            int(row["guild_id"]), int(row["owner_id"]), Path(row["image_path"]),
            row["dialogue"], row["slot"],
        )
        self.assertTrue(view.is_persistent())
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        async def publish_twice():
            with patch.object(discord_app.core, "post_to_discord", return_value="message-1") as post:
                await view.publish(interaction, view.children[0])
                await view.publish(interaction, view.children[0])
                return post.call_count

        self.assertEqual(asyncio.run(publish_twice()), 1)
        self.assertEqual(interaction.response.defer.await_count, 2)
        self.assertEqual(self.previews.get("preview123")["state"], "sent")

    def test_startup_restores_preview_button(self):
        image_path = self.root / "preview.png"
        image_path.write_bytes(b"image")
        self.previews.add("preview456", 101, 77, image_path, "정훈 대사", "lunch")
        client = discord_app.JunghoonClient(self.settings, self.registry, self.previews)

        async def setup():
            with patch.object(client.tree, "sync", new=AsyncMock()), \
                    patch.object(client.scheduler, "start"):
                await client.setup_hook()

        asyncio.run(setup())
        self.assertTrue(any(
            isinstance(view, discord_app.PreviewView) and view.preview_id == "preview456"
            for view in client.persistent_views
        ))

    def test_guild_slash_commands_are_synced_once(self):
        client = discord_app.JunghoonClient(self.settings, self.registry, self.previews)
        guild = discord.Object(id=1139034116138991616)

        async def sync_twice():
            with patch.object(client.tree, "sync", new=AsyncMock(return_value=[object()])) as sync:
                await client.sync_guild_commands(guild)
                await client.sync_guild_commands(guild)
                self.assertEqual(sync.await_count, 1)
                self.assertEqual(sync.await_args.kwargs["guild"], guild)

        asyncio.run(sync_twice())
        self.assertEqual([command.name for command in client.tree.get_commands(guild=guild)], ["정훈봇"])

    def test_preview_command_uses_selected_category_without_network(self):
        self.previews.add("earlier", 101, 77, self.root / "earlier.png", "아 뭐야", "lunch")
        self.previews.add("other-guild", 202, 88, self.root / "other.png", "다른 서버 대사", "lunch")
        client = discord_app.JunghoonClient(self.settings, self.registry, self.previews)
        group = client.tree.get_commands()[0]
        interaction = SimpleNamespace(
            guild_id=101,
            user=SimpleNamespace(id=77),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        def generate(_settings, _slot, path, _previous, scenario, style, trend_mode):
            self.assertEqual((scenario.key, style, trend_mode), ("expression", "bold", "off"))
            self.assertIn("아 뭐야", _previous)
            self.assertNotIn("다른 서버 대사", _previous)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake image")
            return "이거 봐 ㅋㅋ"

        async def run_preview():
            with patch.object(discord_app, "reject_if_not_operator", new=AsyncMock(return_value=False)), \
                    patch.object(discord_app, "SHARED_JOB_PATH", self.root / "scheduled.sqlite3"), \
                    patch.object(discord_app, "JOB_DIR", self.root / "legacy"), \
                    patch.object(discord_app.core, "ROOT", self.root), \
                    patch.object(discord_app.core, "generate_meme", side_effect=generate):
                await group.get_command("미리보기").callback(
                    group, interaction, "expression",
                    discord_app.app_commands.Choice(name="대놓고 웃긴 사진", value="bold"),
                    discord_app.app_commands.Choice(name="새벽", value="dawn"),
                    discord_app.app_commands.Choice(name="이슈 없이", value="off"),
                )

        asyncio.run(run_preview())
        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        sent = interaction.followup.send.await_args.kwargs
        self.assertEqual(sent["view"].slot, "dawn")
        self.assertEqual(sent["view"].dialogue, "이거 봐 ㅋㅋ")
        self.assertEqual(self.previews.get(sent["view"].preview_id)["state"], "ready")
        sent["file"].close()

    def test_two_servers_share_one_generation_and_schedule(self):
        self.registry.set_channel(101, 1001)
        self.registry.set_channel(202, 2002)
        now = datetime(2026, 9, 28, 12, 0, tzinfo=self.settings.timezone)
        with patch.object(discord_app, "SHARED_JOB_PATH", self.root / "scheduled.sqlite3"), \
                patch.object(discord_app, "JOB_DIR", self.root / "legacy"), \
                patch.object(bot, "data_dir", return_value=self.root):
            store = discord_app.shared_store()
            try:
                store.ensure_day(now.date(), self.settings)
                with store.connection:
                    store.connection.execute(
                        "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'lunch'",
                        (now.isoformat(), now.date().isoformat()),
                    )
            finally:
                store.close()
            calls = {"generate": 0, "posts": []}

            def generate(_settings, _slot, path, _previous, _scenario):
                calls["generate"] += 1
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"shared image")
                return "정훈 대사"

            def post(destination, path, _slot, dialogue):
                self.assertEqual(path.read_bytes(), b"shared image")
                self.assertEqual(dialogue, "정훈 대사")
                calls["posts"].append(destination.discord_channel_id)
                return f"message-{destination.discord_channel_id}"

            discord_app.process_broadcast(self.registry, self.settings, now, generate, post)
            discord_app.process_broadcast(self.registry, self.settings, now, generate, post)
            self.assertEqual(calls, {"generate": 1, "posts": ["1001", "2002"]})
            store = discord_app.shared_store()
            try:
                lunch = next(row for row in store.jobs_for_day(now.date()) if row["slot"] == "lunch")
                self.assertEqual(lunch["status"], "ready")
                self.assertEqual(lunch["dialogue"], "정훈 대사")
                deliveries = store.connection.execute(
                    "SELECT guild_id, status FROM deliveries WHERE day = ? AND slot = 'lunch' ORDER BY guild_id",
                    (now.date().isoformat(),),
                ).fetchall()
                self.assertEqual([(row[0], row[1]) for row in deliveries], [("101", "sent"), ("202", "sent")])
            finally:
                store.close()

    def test_failed_delivery_does_not_regenerate_or_block_another_server(self):
        self.registry.set_channel(101, 1001)
        self.registry.set_channel(202, 2002)
        now = datetime(2026, 9, 28, 12, 0, tzinfo=self.settings.timezone)
        with patch.object(discord_app, "SHARED_JOB_PATH", self.root / "scheduled.sqlite3"), \
                patch.object(discord_app, "JOB_DIR", self.root / "legacy"), \
                patch.object(bot, "data_dir", return_value=self.root):
            store = discord_app.shared_store()
            try:
                store.ensure_day(now.date(), self.settings)
                with store.connection:
                    store.connection.execute(
                        "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'lunch'",
                        (now.isoformat(), now.date().isoformat()),
                    )
            finally:
                store.close()

            attempts = []

            def generate(_settings, _slot, path, _previous, _scenario):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"shared image")
                return "정훈 대사"

            def post(destination, _path, _slot, _dialogue):
                attempts.append(destination.discord_channel_id)
                if destination.discord_channel_id == "1001":
                    raise TimeoutError("Response lost")
                return "message-2"

            discord_app.process_broadcast(self.registry, self.settings, now, generate, post)
            discord_app.process_broadcast(self.registry, self.settings, now, generate, post)
            self.assertEqual(attempts, ["1001", "2002"])
            store = discord_app.shared_store()
            try:
                deliveries = store.connection.execute(
                    "SELECT guild_id, status FROM deliveries WHERE day = ? AND slot = 'lunch' ORDER BY guild_id",
                    (now.date().isoformat(),),
                ).fetchall()
                self.assertEqual([(row[0], row[1]) for row in deliveries], [("101", "posting"), ("202", "sent")])
            finally:
                store.close()

    def test_legacy_reservation_and_sent_server_migrate_without_duplicate(self):
        self.registry.set_channel(101, 1001)
        self.registry.set_channel(202, 2002)
        now = datetime(2026, 9, 28, 12, 0, tzinfo=self.settings.timezone)
        legacy_dir = self.root / "guild-jobs"
        legacy_dir.mkdir()
        image_path = self.root / "already-generated.png"
        image_path.write_bytes(b"old image")
        first = bot.JobStore(legacy_dir / "101.sqlite3")
        second = bot.JobStore(legacy_dir / "202.sqlite3")
        try:
            for store in (first, second):
                store.ensure_day(now.date(), self.settings)
            with first.connection:
                first.connection.execute(
                    "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'lunch'",
                    (now.isoformat(), now.date().isoformat()),
                )
                first.connection.execute(
                    "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'dawn'",
                    ((now + timedelta(hours=12)).isoformat(), now.date().isoformat()),
                )
            with second.connection:
                second.connection.execute(
                    "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'dawn'",
                    ((now + timedelta(hours=11)).isoformat(), now.date().isoformat()),
                )
            first.transition(
                now.date().isoformat(), "lunch", "queued", "ready",
                image_path=str(image_path), dialogue="공유 대사", category="expression",
            )
            second.transition(now.date().isoformat(), "lunch", "queued", "sent", message_id="old-202")
        finally:
            first.close()
            second.close()
        with patch.object(discord_app, "SHARED_JOB_PATH", self.root / "scheduled.sqlite3"), \
                patch.object(discord_app, "JOB_DIR", legacy_dir), \
                patch.object(bot, "data_dir", return_value=self.root):
            posts = []

            def post(destination, path, _slot, dialogue):
                posts.append(destination.discord_channel_id)
                self.assertEqual(path.read_bytes(), b"old image")
                self.assertEqual(dialogue, "공유 대사")
                return "new-101"

            def should_not_generate(*_args):
                self.fail("A ready legacy image must be reused")

            discord_app.process_broadcast(self.registry, self.settings, now, should_not_generate, post)
            self.assertEqual(posts, ["1001"])
            store = discord_app.shared_store()
            try:
                lunch = next(row for row in store.jobs_for_day(now.date()) if row["slot"] == "lunch")
                self.assertEqual(lunch["scheduled_at"], now.isoformat())
                deliveries = store.connection.execute(
                    "SELECT guild_id, status FROM deliveries WHERE day = ? AND slot = 'lunch' ORDER BY guild_id",
                    (now.date().isoformat(),),
                ).fetchall()
                self.assertEqual([(row[0], row[1]) for row in deliveries], [("101", "sent"), ("202", "sent")])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
