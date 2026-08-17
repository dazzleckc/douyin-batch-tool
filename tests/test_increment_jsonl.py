import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.models import OutlineResult, OutputRecord, ScrapeCheckpoint, VideoInfo
from src.pipeline import (
    _increment_jsonl_path,
    _increment_summary,
    _load_checkpoint,
    _persist_success_record,
    _recover_from_partial,
    _save_checkpoint,
    run_ai_phase,
    run_scrape_phase,
)


class FakeDB:
    def __init__(self):
        self.parsed: set[str] = set()
        self.failed: set[str] = set()

    def is_parsed(self, aweme_id: str) -> bool:
        return aweme_id in self.parsed

    def mark_parsed(self, aweme_id: str, title: str = "") -> None:
        self.parsed.add(aweme_id)
        self.failed.discard(aweme_id)

    def mark_parse_failed(self, aweme_id: str, error: str) -> None:
        self.failed.add(aweme_id)

    def close(self) -> None:
        pass


class FakeDoubao:
    def __init__(self, *args, **kwargs):
        pass

    async def _ensure_context(self) -> None:
        pass

    async def generate_outline(self, title: str, url: str, description: str):
        aweme_id = url.rsplit("/", 1)[-1]
        return OutlineResult(
            aweme_id=aweme_id,
            outline_markdown=f"outline-{aweme_id}",
            raw_response="simulated",
            success=True,
        )

    async def close(self) -> None:
        pass


def record(aweme_id: str, batch_id: str = "") -> OutputRecord:
    return OutputRecord(
        aweme_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        title=f"title-{aweme_id}",
        outline=f"outline-{aweme_id}",
        status="success",
        timestamp="2026-08-09T12:00:00",
        batch_id=batch_id,
    )


def read_jsonl(path: str | Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class IncrementJsonlContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.output_dir = self.tempdir.name
        self.user_id = "user_1"
        self.cumulative = os.path.join(
            self.output_dir, f"{self.user_id}_outline.jsonl"
        )
        self.db = FakeDB()

    def tearDown(self):
        self.tempdir.cleanup()

    def persist(self, item: OutputRecord) -> str:
        return _persist_success_record(
            self.user_id,
            self.output_dir,
            self.cumulative,
            item,
            self.db,
        )

    def test_two_rounds_keep_cumulative_and_isolate_second_increment(self):
        for aweme_id in ("1001", "1002"):
            self.persist(record(aweme_id, "batch_first"))

        touched = set()
        for aweme_id in ("2001", "2002"):
            path = self.persist(record(aweme_id, "batch_second"))
            if path:
                touched.add(path)

        cumulative = read_jsonl(self.cumulative)
        second_path = _increment_jsonl_path(
            self.user_id, self.output_dir, "batch_second"
        )
        second = read_jsonl(second_path)

        self.assertEqual({r["aweme_id"] for r in cumulative}, {"1001", "1002", "2001", "2002"})
        self.assertEqual([r["aweme_id"] for r in second], ["2001", "2002"])
        self.assertEqual(len({r["aweme_id"] for r in second}), len(second))
        self.assertEqual(_increment_summary(touched), (2, [second_path]))
        required = {"aweme_id", "url", "title", "outline", "status", "timestamp", "batch_id"}
        self.assertTrue(all(required <= set(row) for row in second))

    def test_interruption_resume_is_complete_and_has_no_duplicates(self):
        checkpoint_path = os.path.join(self.output_dir, f"{self.user_id}_checkpoint.json")
        checkpoint = ScrapeCheckpoint(
            user_url="https://www.douyin.com/user/user_1",
            user_id=self.user_id,
            created_at="2026-08-09T12:00:00",
            videos=[
                {
                    "aweme_id": "3001",
                    "url": "https://www.douyin.com/video/3001",
                    "batch_id": "stable_batch",
                },
                {
                    "aweme_id": "3002",
                    "url": "https://www.douyin.com/video/3002",
                    "batch_id": "stable_batch",
                },
            ],
            total=2,
            new_count=2,
            batch_id="stable_batch",
            batch_aweme_ids=["3001", "3002"],
        )
        _save_checkpoint(checkpoint, checkpoint_path)
        resumed = _load_checkpoint(checkpoint_path)
        self.assertEqual(resumed.batch_id, "stable_batch")
        self.assertEqual(resumed.batch_aweme_ids, ["3001", "3002"])

        first = record("3001", "stable_batch")
        second = record("3002", "stable_batch")

        self.persist(first)
        self.persist(first)
        self.persist(second)
        self.persist(second)

        increment = read_jsonl(
            _increment_jsonl_path(self.user_id, self.output_dir, "stable_batch")
        )
        self.assertEqual([row["aweme_id"] for row in increment], ["3001", "3002"])

    def test_historical_partial_and_jsonl_do_not_enter_current_increment(self):
        historical = record("4001")
        self.persist(historical)
        partial_path = Path(self.output_dir) / f"{self.user_id}_partial_20260808_120000.json"
        partial_path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "url": historical.url,
                            "title": historical.title,
                            "outline": historical.outline,
                            "status": "success",
                        }
                    ],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        videos = [
            {"aweme_id": "4001", "url": historical.url, "title": historical.title},
            {
                "aweme_id": "4002",
                "url": "https://www.douyin.com/video/4002",
                "title": "current",
                "batch_id": "current_batch",
            },
        ]

        recovered = _recover_from_partial(
            self.user_id,
            self.output_dir,
            videos,
            self.db,
            False,
            self.cumulative,
        )
        self.assertEqual(recovered[-1], set())

        self.persist(record("4002", "current_batch"))
        current = read_jsonl(
            _increment_jsonl_path(self.user_id, self.output_dir, "current_batch")
        )
        self.assertEqual([row["aweme_id"] for row in current], ["4002"])

    def test_failed_then_retry_success_enters_increment_once(self):
        self.db.mark_parse_failed("5001", "temporary failure")
        item = record("5001", "retry_batch")

        self.persist(item)
        self.persist(item)

        increment = read_jsonl(
            _increment_jsonl_path(self.user_id, self.output_dir, "retry_batch")
        )
        self.assertEqual([row["aweme_id"] for row in increment], ["5001"])
        self.assertIn("5001", self.db.parsed)
        self.assertNotIn("5001", self.db.failed)

    def test_no_new_success_creates_no_increment_file(self):
        count, files = _increment_summary(set())

        self.assertEqual(count, 0)
        self.assertEqual(files, [])
        self.assertEqual(list(Path(self.output_dir).glob("*_increment_*.jsonl")), [])

    def test_increment_write_failure_does_not_mark_db_parsed(self):
        item = record("6001", "write_failure_batch")
        from src import pipeline

        real_append = pipeline._append_jsonl_once
        calls = 0

        def fail_second_write(path, payload, aweme_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated increment write failure")
            return real_append(path, payload, aweme_id)

        with mock.patch("src.pipeline._append_jsonl_once", side_effect=fail_second_write):
            with self.assertRaisesRegex(OSError, "simulated increment"):
                self.persist(item)

        self.assertNotIn("6001", self.db.parsed)
        self.assertEqual([row["aweme_id"] for row in read_jsonl(self.cumulative)], ["6001"])
        self.assertFalse(
            os.path.exists(
                _increment_jsonl_path(
                    self.user_id, self.output_dir, "write_failure_batch"
                )
            )
        )

    def test_old_checkpoint_without_batch_metadata_is_compatible_and_safe(self):
        checkpoint_path = Path(self.output_dir) / f"{self.user_id}_checkpoint.json"
        checkpoint_path.write_text(
            json.dumps(
                {
                    "user_url": "https://www.douyin.com/user/user_1",
                    "user_id": self.user_id,
                    "created_at": "2026-08-08T12:00:00",
                    "videos": [
                        {
                            "aweme_id": "7001",
                            "url": "https://www.douyin.com/video/7001",
                            "title": "legacy",
                        }
                    ],
                    "total": 1,
                    "new_count": 1,
                    "skipped_count": 0,
                }
            ),
            encoding="utf-8",
        )

        checkpoint = _load_checkpoint(str(checkpoint_path))
        self.assertEqual(checkpoint.batch_id, "")
        self.assertEqual(checkpoint.batch_aweme_ids, [])
        self.assertFalse(checkpoint.batch_completed)
        self.assertNotIn("batch_id", checkpoint.videos[0])

        self.persist(record("7001"))
        self.assertEqual(list(Path(self.output_dir).glob("*_increment_*.jsonl")), [])

    def test_run_ai_phase_two_round_offline_golden_path(self):
        shared_db = FakeDB()
        config = SimpleNamespace(doubao_cookie="")
        checkpoint_path = os.path.join(self.output_dir, f"{self.user_id}_checkpoint.json")

        def checkpoint(batch_id: str, aweme_ids: list[str], previous: list[dict]) -> ScrapeCheckpoint:
            current = [
                {
                    "aweme_id": aweme_id,
                    "url": f"https://www.douyin.com/video/{aweme_id}",
                    "title": f"title-{aweme_id}",
                    "description": "simulated",
                    "batch_id": batch_id,
                }
                for aweme_id in aweme_ids
            ]
            return ScrapeCheckpoint(
                user_url="https://www.douyin.com/user/user_1",
                user_id=self.user_id,
                created_at="2026-08-09T12:00:00",
                videos=previous + current,
                total=len(previous) + len(current),
                new_count=len(current),
                batch_id=batch_id,
                batch_aweme_ids=aweme_ids,
            )

        first = checkpoint("round_one", ["8001", "8002"], [])
        _save_checkpoint(first, checkpoint_path)
        with (
            mock.patch("src.pipeline.ProcessDB", return_value=shared_db),
            mock.patch("src.pipeline.DoubaoClient", FakeDoubao),
        ):
            first_result = asyncio.run(
                run_ai_phase(
                    checkpoint_path,
                    self.output_dir,
                    False,
                    (0, 0),
                    config,
                    concurrency=2,
                )
            )

            second = checkpoint("round_two", ["9001", "9002"], first.videos)
            _save_checkpoint(second, checkpoint_path)
            second_result = asyncio.run(
                run_ai_phase(
                    checkpoint_path,
                    self.output_dir,
                    False,
                    (0, 0),
                    config,
                    concurrency=2,
                )
            )

        cumulative = read_jsonl(self.cumulative)
        second_increment = read_jsonl(
            _increment_jsonl_path(self.user_id, self.output_dir, "round_two")
        )
        self.assertEqual(first_result.increment_count, 2)
        self.assertEqual(second_result.increment_count, 2)
        self.assertEqual(
            [row["aweme_id"] for row in cumulative],
            ["8001", "8002", "9001", "9002"],
        )
        self.assertEqual(
            [row["aweme_id"] for row in second_increment],
            ["9001", "9002"],
        )
        self.assertEqual(
            len({row["aweme_id"] for row in cumulative}), len(cumulative)
        )

    def test_partial_skipped_recovered_from_jsonl_is_not_counted_twice(self):
        shared_db = FakeDB()
        config = SimpleNamespace(doubao_cookie="")
        checkpoint_path = os.path.join(
            self.output_dir, f"{self.user_id}_checkpoint.json"
        )
        checkpoint = ScrapeCheckpoint(
            user_url="https://www.douyin.com/user/user_1",
            user_id=self.user_id,
            created_at="2026-08-09T12:00:00",
            videos=[
                {
                    "aweme_id": "9051",
                    "url": "https://www.douyin.com/video/9051",
                    "title": "legacy",
                }
            ],
            total=1,
            new_count=0,
        )
        _save_checkpoint(checkpoint, checkpoint_path)
        _persist_success_record(
            self.user_id,
            self.output_dir,
            self.cumulative,
            record("9051"),
            shared_db,
        )
        partial_path = Path(self.output_dir) / (
            f"{self.user_id}_partial_20260808_120000.json"
        )
        partial_path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "aweme_id": "9051",
                            "url": "https://www.douyin.com/video/9051",
                            "title": "legacy",
                            "outline": "",
                            "status": "skipped",
                        }
                    ],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

        with (
            mock.patch("src.pipeline.ProcessDB", return_value=shared_db),
            mock.patch("src.pipeline.DoubaoClient", FakeDoubao),
        ):
            result = asyncio.run(
                run_ai_phase(
                    checkpoint_path,
                    self.output_dir,
                    False,
                    (0, 0),
                    config,
                    concurrency=1,
                )
            )

        final_rows = json.loads(Path(result.output_file).read_text(encoding="utf-8"))
        self.assertEqual(result.total, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(final_rows[0]["url"], "https://www.douyin.com/video/9051")

    def test_resume_reports_existing_current_batch_increment_without_duplicates(self):
        shared_db = FakeDB()
        config = SimpleNamespace(doubao_cookie="")
        checkpoint_path = os.path.join(
            self.output_dir, f"{self.user_id}_checkpoint.json"
        )
        checkpoint = ScrapeCheckpoint(
            user_url="https://www.douyin.com/user/user_1",
            user_id=self.user_id,
            created_at="2026-08-09T12:00:00",
            videos=[
                {
                    "aweme_id": aweme_id,
                    "url": f"https://www.douyin.com/video/{aweme_id}",
                    "title": f"title-{aweme_id}",
                    "batch_id": "resume_batch",
                }
                for aweme_id in ("9061", "9062")
            ],
            total=2,
            new_count=2,
            batch_id="resume_batch",
            batch_aweme_ids=["9061", "9062"],
        )
        _save_checkpoint(checkpoint, checkpoint_path)
        for aweme_id in checkpoint.batch_aweme_ids:
            _persist_success_record(
                self.user_id,
                self.output_dir,
                self.cumulative,
                record(aweme_id, "resume_batch"),
                shared_db,
            )

        partial_path = Path(self.output_dir) / (
            f"{self.user_id}_partial_20260809_120000.json"
        )
        partial_path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "aweme_id": aweme_id,
                            "url": f"https://www.douyin.com/video/{aweme_id}",
                            "title": f"title-{aweme_id}",
                            "outline": f"outline-{aweme_id}",
                            "status": "success",
                            "timestamp": "2026-08-09T12:00:00",
                            "batch_id": "resume_batch",
                        }
                        for aweme_id in checkpoint.batch_aweme_ids
                    ],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

        with (
            mock.patch("src.pipeline.ProcessDB", return_value=shared_db),
            mock.patch("src.pipeline.DoubaoClient", FakeDoubao),
        ):
            result = asyncio.run(
                run_ai_phase(
                    checkpoint_path,
                    self.output_dir,
                    False,
                    (0, 0),
                    config,
                    concurrency=1,
                )
            )

        increment_path = _increment_jsonl_path(
            self.user_id, self.output_dir, "resume_batch"
        )
        self.assertEqual(result.increment_count, 2)
        self.assertEqual(result.increment_files, [increment_path])
        self.assertEqual(
            [row["aweme_id"] for row in read_jsonl(increment_path)],
            ["9061", "9062"],
        )
        completed = _load_checkpoint(checkpoint_path)
        self.assertTrue(completed.batch_completed)

        with (
            mock.patch("src.pipeline.ProcessDB", return_value=shared_db),
            mock.patch("src.pipeline.DoubaoClient", FakeDoubao),
        ):
            no_new_result = asyncio.run(
                run_ai_phase(
                    checkpoint_path,
                    self.output_dir,
                    False,
                    (0, 0),
                    config,
                    concurrency=1,
                )
            )

        self.assertEqual(no_new_result.increment_count, 0)
        self.assertEqual(no_new_result.increment_files, [])

    def test_scrape_persists_batch_identity_before_marking_db_known(self):
        checkpoint_path = os.path.join(self.output_dir, f"{self.user_id}_checkpoint.json")

        class ScrapeDB(FakeDB):
            def __init__(inner_self):
                super().__init__()
                inner_self.known: set[str] = set()
                inner_self.saw_checkpoint_before_mark = False

            def get_known_ids(inner_self) -> set[str]:
                return set(inner_self.known)

            def mark_known_batch(inner_self, aweme_ids: list[str]) -> None:
                inner_self.saw_checkpoint_before_mark = os.path.exists(checkpoint_path)
                inner_self.known.update(aweme_ids)

        processed_args: list[set[str]] = []

        class Scraper:
            def __init__(inner_self, *args, **kwargs):
                inner_self.last_known_seen_count = 0

            async def scrape_user_videos(inner_self, user_url, limit, processed_ids=None):
                processed_args.append(set(processed_ids or set()))
                inner_self.last_known_seen_count = len(processed_ids or set())
                return [
                    VideoInfo(
                        aweme_id="9101",
                        url="https://www.douyin.com/video/9101",
                        title="first",
                    ),
                    VideoInfo(
                        aweme_id="9102",
                        url="https://www.douyin.com/video/9102",
                        title="second",
                    ),
                ]

            async def close(inner_self):
                pass

        shared_db = ScrapeDB()
        shared_db.known.add("unrelated_other_target")
        with (
            mock.patch("src.pipeline.ProcessDB", return_value=shared_db),
            mock.patch("src.pipeline.VideoScraper", Scraper),
        ):
            first = asyncio.run(
                run_scrape_phase(
                    "https://www.douyin.com/user/user_1",
                    None,
                    self.output_dir,
                    SimpleNamespace(),
                )
            )
            second = asyncio.run(
                run_scrape_phase(
                    "https://www.douyin.com/user/user_1",
                    None,
                    self.output_dir,
                    SimpleNamespace(),
                )
            )

        saved = _load_checkpoint(checkpoint_path)
        self.assertTrue(shared_db.saw_checkpoint_before_mark)
        self.assertEqual(first.new_count, 2)
        self.assertTrue(first.batch_id)
        self.assertEqual(
            {video["batch_id"] for video in saved.videos}, {first.batch_id}
        )
        self.assertEqual(second.new_count, 0)
        self.assertEqual(second.batch_id, "")
        self.assertEqual(saved.batch_id, first.batch_id)
        self.assertEqual(processed_args[0], set())
        self.assertEqual(processed_args[1], {"9101", "9102"})


if __name__ == "__main__":
    unittest.main()
