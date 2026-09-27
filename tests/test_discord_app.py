import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
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
        self.assertTrue(group.default_permissions.administrator)
        self.assertEqual(
            [parameter.name for parameter in group.get_command("미리보기").parameters],
            ["유형", "분위기", "시간대", "이슈"],
        )
        panel = discord_app.SettingsView(self.registry, self.settings, owner_id=1, guild_id=101)
        self.assertTrue(any(isinstance(item, discord_app.ChannelPicker) for item in panel.children))
        self.assertEqual(sum(getattr(item, "label", None) is not None for item in panel.children), 2)

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
            with patch.object(discord_app, "reject_if_not_admin", new=AsyncMock(return_value=False)), \
                    patch.object(discord_app, "JOB_DIR", self.root / "jobs"), \
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

    def test_two_servers_keep_independent_schedules(self):
        with patch.object(discord_app, "JOB_DIR", self.root / "jobs"):
            first = discord_app.job_store(101)
            second = discord_app.job_store(202)
            try:
                day = datetime.now(self.settings.timezone).date()
                first.ensure_day(day, self.settings)
                second.ensure_day(day, self.settings)
                first.transition(day.isoformat(), "lunch", "queued", "sent")
                self.assertEqual(len(first.jobs_for_day(day)), 2)
                self.assertEqual(len(second.jobs_for_day(day)), 2)
                first_lunch = next(row for row in first.jobs_for_day(day) if row["slot"] == "lunch")
                second_lunch = next(row for row in second.jobs_for_day(day) if row["slot"] == "lunch")
                self.assertEqual(first_lunch["status"], "sent")
                self.assertEqual(second_lunch["status"], "queued")
            finally:
                first.close()
                second.close()

    def test_pausing_during_post_keeps_job_unsent(self):
        self.registry.set_channel(101, 1001)
        with patch.object(discord_app, "JOB_DIR", self.root / "jobs"):
            store = discord_app.job_store(101)
            try:
                now = datetime(2026, 9, 28, 12, 0, tzinfo=self.settings.timezone)
                store.ensure_day(now.date(), self.settings)
                with store.connection:
                    store.connection.execute(
                        "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'lunch'",
                        ((now - timedelta(minutes=1)).isoformat(), now.date().isoformat()),
                    )

                def generate(_settings, _slot, path, _previous, _scenario):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fake image")
                    return "정훈 대사"

                def pause_before_post(_settings, _path, _slot, _dialogue):
                    self.registry.set_enabled(101, False)
                    raise bot.PostingPaused()

                with patch.object(bot, "ROOT", self.root):
                    bot.process_jobs(store, self.settings, now, generate, pause_before_post)
                lunch = next(row for row in store.jobs_for_day(now.date()) if row["slot"] == "lunch")
                self.assertEqual(lunch["status"], "ready")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
