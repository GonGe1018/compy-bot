import tempfile
import base64
import json
import os
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image

import bot


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
            discord_bot_token=None,
            discord_channel_id=None,
            discord_webhook_url=None,
        )
        self.store = bot.JobStore(self.root / "test.sqlite3")
        self.addCleanup(self.store.close)

    def test_random_times_are_in_windows_and_persisted(self):
        day = date(2026, 9, 28)
        self.store.ensure_day(day, self.settings)
        first = self.store.jobs_for_day(day)
        self.store.ensure_day(day, self.settings)
        second = self.store.jobs_for_day(day)
        self.assertEqual([row["scheduled_at"] for row in first], [row["scheduled_at"] for row in second])
        self.assertEqual([row["creative_mode"] for row in first], [row["creative_mode"] for row in second])
        self.assertCountEqual([row["creative_mode"] for row in first], ["legend", "regular"])
        self.assertEqual(len(first), 2)
        for row in first:
            window = next(window for window in self.settings.windows if window.name == row["slot"])
            selected = datetime.fromisoformat(row["scheduled_at"])
            start = datetime.combine(day, window.start, self.settings.timezone)
            end_day = day + timedelta(days=1) if window.end < window.start else day
            end = datetime.combine(end_day, window.end, self.settings.timezone)
            self.assertTrue(start <= selected < end)

    def test_data_directory_can_be_moved_for_docker(self):
        target = self.root / "persistent"
        with patch.dict(os.environ, {"BOT_DATA_DIR": str(target)}):
            self.assertEqual(bot.data_dir(), target.resolve())

    def test_explicit_reference_pair_uses_only_named_photos_in_order(self):
        folder = self.root / "photos"
        folder.mkdir()
        for name in ("first.jpg", "second.jpg", "other.png"):
            (folder / name).write_bytes(b"photo")
        with patch.object(bot, "ROOT", self.root):
            self.assertEqual(
                [path.name for path in bot.photos_for_job(2, ("second.jpg", "first.jpg"))],
                ["second.jpg", "first.jpg"],
            )
            with self.assertRaises(ValueError):
                bot.photos_for_job(2, ("first.jpg", "missing.jpg"))
            with self.assertRaises(ValueError):
                bot.photos_for_job(2, ("first.jpg", "first.jpg"))
            with self.assertRaises(ValueError):
                bot.photos_for_job(2)

    def test_operator_ids_can_be_extended_without_admin_permissions(self):
        with patch.object(bot, "ROOT", self.root), patch.dict(
            os.environ, {"BOT_OPERATOR_IDS": "123456789, 987654321"}
        ):
            settings = bot.load_settings()
        self.assertEqual(settings.operator_ids, frozenset({123456789, 987654321}))

    def test_changed_window_replans_existing_unsent_job(self):
        day = date(2026, 9, 28)
        old_settings = replace(
            self.settings,
            windows=(bot.parse_window("dawn", "04:00-06:00"), self.settings.windows[1]),
        )
        self.store.ensure_day(day, old_settings)
        self.store.ensure_day(day, self.settings)
        dawn = next(row for row in self.store.jobs_for_day(day) if row["slot"] == "dawn")
        selected = datetime.fromisoformat(dawn["scheduled_at"])
        self.assertEqual(dawn["window_spec"], "22:00-04:00")
        self.assertTrue(datetime(2026, 9, 28, 22, tzinfo=self.settings.timezone) <= selected)
        self.assertTrue(selected < datetime(2026, 9, 29, 4, tzinfo=self.settings.timezone))

    def test_existing_database_assigns_legend_to_remaining_queued_slot(self):
        day = date(2026, 9, 28)
        self.store.ensure_day(day, self.settings)
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE jobs SET creative_mode = NULL WHERE day = ?", (day.isoformat(),)
            )
            self.store.connection.execute(
                "UPDATE jobs SET status = 'sent' WHERE day = ? AND slot = 'lunch'", (day.isoformat(),)
            )
        self.store.ensure_day(day, self.settings)
        rows = {row["slot"]: row for row in self.store.jobs_for_day(day)}
        self.assertEqual(rows["dawn"]["creative_mode"], "legend")
        self.assertEqual(rows["lunch"]["creative_mode"], "regular")

    def test_prepare_then_post_exactly_once(self):
        day = date(2026, 9, 28)
        self.store.ensure_day(day, self.settings)
        # Force an after-midnight time to prove the previous day's job is processed.
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE jobs SET scheduled_at = ? WHERE day = ? AND slot = 'dawn'",
                (datetime.combine(day + timedelta(days=1), time(2, 0), self.settings.timezone).isoformat(), day.isoformat()),
            )
        dawn = next(row for row in self.store.jobs_for_day(day) if row["slot"] == "dawn")
        scheduled = datetime.fromisoformat(dawn["scheduled_at"])
        calls = {"generate": 0, "post": 0}
        generated_modes = []

        def generate(_settings, _slot, path, _previous, scenario):
            calls["generate"] += 1
            generated_modes.append(scenario.mode)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake image")
            return "이거 내가 해냈다고?"

        def post(_settings, _path, _slot, dialogue, rarity):
            calls["post"] += 1
            self.assertEqual(dialogue, "이거 내가 해냈다고?")
            self.assertIn(rarity, bot.RARITY_WEIGHTS[generated_modes[0]])
            return "discord-message-1"

        with patch.object(bot, "ROOT", self.root):
            bot.process_jobs(self.store, self.settings, scheduled - timedelta(minutes=14), generate, post)
            self.assertEqual(calls, {"generate": 1, "post": 0})
            bot.process_jobs(self.store, self.settings, scheduled, generate, post)
            bot.process_jobs(self.store, self.settings, scheduled, generate, post)
        self.assertEqual(calls, {"generate": 1, "post": 1})
        updated = next(row for row in self.store.jobs_for_day(day) if row["slot"] == "dawn")
        self.assertEqual(updated["status"], "sent")
        self.assertEqual(updated["message_id"], "discord-message-1")
        self.assertEqual(updated["dialogue"], "이거 내가 해냈다고?")
        self.assertIn(updated["rarity"], bot.RARITY_WEIGHTS[updated["creative_mode"]])
        self.assertIn(updated["category"], {scenario.key for scenario in bot.SCENARIOS})
        self.assertEqual(generated_modes, [updated["creative_mode"]])

    def test_uncertain_post_is_not_retried_automatically(self):
        day = date(2026, 9, 28)
        self.store.ensure_day(day, self.settings)
        dawn = next(row for row in self.store.jobs_for_day(day) if row["slot"] == "dawn")
        scheduled = datetime.fromisoformat(dawn["scheduled_at"])
        attempts = []

        def generate(_settings, _slot, path, _previous, _scenario):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake image")
            return "이거 내가 해냈다고?"

        def post(_settings, _path, _slot, _dialogue, _rarity):
            attempts.append(1)
            raise TimeoutError("Response lost")

        with patch.object(bot, "ROOT", self.root):
            bot.process_jobs(self.store, self.settings, scheduled, generate, post)
            bot.process_jobs(self.store, self.settings, scheduled + timedelta(minutes=1), generate, post)
        self.assertEqual(len(attempts), 1)
        updated = next(row for row in self.store.jobs_for_day(day) if row["slot"] == "dawn")
        self.assertEqual(updated["status"], "posting")

    def test_discord_message_uses_bot_name_and_dialogue(self):
        image_path = self.root / "meme.png"
        image_path.write_bytes(b"fake image")
        settings = replace(self.settings, discord_webhook_url="https://discord.example/webhook")
        response = Mock()
        response.json.return_value = {"id": "message-123"}
        with patch.object(bot.requests, "post", return_value=response) as request:
            message_id = bot.post_to_discord(settings, image_path, "dawn", "@everyone 오늘은 내가 이겼다")
        self.assertEqual(message_id, "message-123")
        payload = json.loads(request.call_args.kwargs["data"]["payload_json"])
        self.assertEqual(payload["username"], "정훈봇")
        self.assertEqual(payload["flags"], 32768)
        card = payload["components"][0]
        self.assertEqual(card["components"][0]["content"], "@everyone 오늘은 내가 이겼다")
        self.assertEqual(card["components"][1]["items"][0]["media"]["url"], "attachment://meme.png")
        self.assertIn("N 등급", card["components"][2]["content"])
        self.assertNotIn("content", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_bot_token_posts_as_bot_user_to_selected_channel(self):
        image_path = self.root / "meme.png"
        image_path.write_bytes(b"fake image")
        settings = replace(
            self.settings,
            discord_bot_token="test-token",
            discord_channel_id="123456789012345678",
            discord_webhook_url="https://discord.example/webhook",
        )
        response = Mock()
        response.json.return_value = {"id": "message-456"}
        with patch.object(bot.requests, "post", return_value=response) as request:
            message_id = bot.post_to_discord(settings, image_path, "lunch", "오늘은 좀 괜찮은데")
        self.assertEqual(message_id, "message-456")
        self.assertEqual(request.call_args.args[0], "https://discord.com/api/v10/channels/123456789012345678/messages")
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bot test-token"})
        payload = json.loads(request.call_args.kwargs["data"]["payload_json"])
        self.assertEqual(payload["components"][0]["components"][0]["content"], "오늘은 좀 괜찮은데")
        self.assertNotIn("username", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_generated_photo_is_saved_without_text_overlay(self):
        first = self.root / "first.jpg"
        second = self.root / "second.png"
        Image.new("RGB", (4096, 2304), "blue").save(first, format="JPEG")
        Image.new("RGBA", (800, 600), "red").save(second, format="PNG")
        original_sizes = (first.stat().st_size, second.stat().st_size)
        image_bytes = b"image bytes from the model"
        client = Mock()
        received = []

        def inspect_upload(**kwargs):
            for upload in kwargs["image"]:
                with Image.open(upload) as image:
                    received.append((upload.name, image.format, image.mode, image.size))
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(image_bytes).decode("ascii"))]
            )

        client.images.edit.side_effect = inspect_upload
        destination = self.root / "output" / "update.png"
        with patch("openai.OpenAI", return_value=client), patch.object(
            bot, "photos_for_job", return_value=[first, second]
        ), patch.object(bot, "create_idea", return_value=("와 이게 되네 ㅋㅋ", "A candid photo.")):
            dialogue = bot.generate_meme(
                replace(self.settings, openai_key="test-key"), "lunch", destination, [], bot.SCENARIOS[0]
            )
        self.assertEqual(dialogue, "와 이게 되네 ㅋㅋ")
        self.assertEqual(destination.read_bytes(), image_bytes)
        self.assertEqual(received, [
            ("reference-1.png", "PNG", "RGB", (1638, 2048)),
            ("reference-2.png", "PNG", "RGB", (408, 408)),
        ])
        self.assertEqual((first.stat().st_size, second.stat().st_size), original_sizes)
        self.assertIn("Image 1 is the main identity", client.images.edit.call_args.kwargs["prompt"])

    def test_scenario_does_not_repeat_recent_categories(self):
        recent = [scenario.key for scenario in bot.SCENARIOS[:4]]
        for _ in range(50):
            self.assertNotIn(bot.choose_scenario(recent).key, recent)
            legend = bot.choose_scenario(recent, "legend")
            self.assertIn(legend.key, bot.LEGEND_CATEGORIES)
            self.assertNotIn(legend.key, recent)
            self.assertIn(legend.rarity, bot.RARITY_WEIGHTS["legend"])

    def test_card_rarity_changes_accent_and_small_label_without_changing_photo(self):
        normal = bot.card_components("photo.png", "오늘 뭐냐", "N", "정훈")[0]
        ultra = bot.card_components("photo.png", "오늘 뭐냐", "UR", "정훈")[0]
        self.assertNotEqual(normal["accent_color"], ultra["accent_color"])
        self.assertEqual(normal["components"][:2], ultra["components"][:2])
        self.assertTrue(normal["components"][2]["content"].startswith("-# "))
        self.assertIn("UR 등급", ultra["components"][2]["content"])
        self.assertIn("***", ultra["components"][2]["content"])

    def test_idea_uses_selected_category_and_keeps_words_out_of_photo(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=json.dumps({
            "dialogue": "아니 이게 왜 여기 있냐 ㅋㅋ",
            "image_prompt": "A deadpan absurd scene.",
        }))
        scenario = next(item for item in bot.SCENARIOS if item.key == "deadpan_absurd")
        with patch.object(bot.random, "random", return_value=1):
            dialogue, prompt = bot.create_idea(client, self.settings, "dawn", [], scenario)
        self.assertEqual(dialogue, "아니 이게 왜 여기 있냐 ㅋㅋ")
        self.assertEqual(prompt, "A deadpan absurd scene.")
        request = client.responses.create.call_args.kwargs
        self.assertIn(scenario.name, request["input"])
        self.assertIn("no dialogue, captions", request["instructions"])
        self.assertIn("image should be interesting", request["instructions"])

    def test_legend_idea_brainstorms_three_concepts_then_refines_one(self):
        client = Mock()
        client.responses.create.side_effect = [
            SimpleNamespace(output_text=json.dumps({
                "first": "A wide-angle selfie with a tiny parade behind him.",
                "second": "He is deadpan as an animal takes over a formal ceremony.",
                "third": "A huge prop dwarfs him at a bus stop.",
            })),
            SimpleNamespace(output_text=json.dumps({
                "dialogue": "아 시바 이게 뭐냐", "image_prompt": "One candid, funny photo."
            })),
        ]
        scenario = bot.with_rarity(bot.SCENARIOS[0], "legend", "SR")
        with patch.object(bot.random, "random", return_value=1):
            dialogue, prompt = bot.create_idea(client, self.settings, "lunch", [], scenario)
        self.assertEqual((dialogue, prompt), ("아 시바 이게 뭐냐", "One candid, funny photo."))
        self.assertEqual(client.responses.create.call_count, 2)
        final_request = client.responses.create.call_args.kwargs
        self.assertIn("Three candidate comedy concepts", final_request["input"])
        self.assertIn("daily legend attempt", final_request["instructions"])

    def test_trend_candidates_require_fresh_lighthearted_meme_signal(self):
        feed = """<rss><channel>
          <item><title>fun snack</title><pubDate>Sun, 27 Sep 2026 20:00:00 +0000</pubDate>
            <news_item><news_item_title>Snack meme becomes a viral craze</news_item_title></news_item></item>
          <item><title>심각한 사고</title><pubDate>Sun, 27 Sep 2026 20:00:00 +0000</pubDate>
            <news_item><news_item_title>사고가 화제</news_item_title></news_item></item>
          <item><title>old craze</title><pubDate>Mon, 01 Sep 2025 20:00:00 +0000</pubDate>
            <news_item><news_item_title>Old meme</news_item_title></news_item></item>
        </channel></rss>""".encode("utf-8")
        candidates = bot.parse_trend_candidates(feed, datetime(2026, 9, 27, 22, tzinfo=timezone.utc))
        self.assertEqual([item.title for item in candidates], ["fun snack"])

    def test_trend_is_optional_and_screened_in_idea_prompt(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=json.dumps({
            "dialogue": "와 이건 좀 웃긴데 ㅋㅋ", "image_prompt": "A playful snack scene."
        }))
        with patch.object(bot.random, "random", return_value=0), patch.object(
            bot, "fetch_trend_candidates", return_value=[bot.TrendCandidate("과자 밈", ("과자 밈 유행",))]
        ):
            bot.create_idea(client, self.settings, "lunch", [], bot.SCENARIOS[0])
        idea_input = client.responses.create.call_args.kwargs["input"]
        self.assertIn("과자 밈", idea_input)
        self.assertIn("If none qualifies, ignore all candidates", idea_input)


if __name__ == "__main__":
    unittest.main()
