import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.scraper import SELECTORS, VideoScraper


class TextElement:
    def __init__(self, text: str):
        self.text = text

    async def text_content(self):
        return self.text

    async def inner_text(self):
        return self.text


class ExpectedTotalPage:
    async def query_selector(self, selector: str):
        return TextElement("无关的 profile 区域")

    async def query_selector_all(self, selector: str):
        return [TextElement("无关的 profile 区域")]

    async def text_content(self, selector: str):
        return "投知君君买方视角 关注 22 粉丝 11.4万 作品 193 推荐"


class DelayedMetadataItem:
    def __init__(self):
        self.metadata_attempts = 0

    async def get_attribute(self, name: str):
        if name == "href":
            return "/video/1234567890123456789"
        return None

    async def query_selector_all(self, selector: str):
        self.metadata_attempts += 1
        if self.metadata_attempts == 1:
            return []
        return [TextElement("延迟渲染后出现的标题")]

    async def query_selector(self, selector: str):
        return None

    async def inner_text(self):
        return "延迟渲染后出现的标题"


class DelayedMetadataPage:
    def __init__(self, item):
        self.item = item

    async def query_selector_all(self, selector: str):
        return [self.item]

    async def evaluate(self, script: str):
        return None


class AttributeMetadataItem:
    async def query_selector_all(self, selector: str):
        return []

    async def query_selector(self, selector: str):
        return None

    async def get_attribute(self, name: str):
        if name == "aria-label":
            return "来自 aria-label 的视频标题"
        return None

    async def inner_text(self):
        return ""


class ImmediateMetadataItem:
    def __init__(self, aweme_id: str, title: str):
        self.aweme_id = aweme_id
        self.title = title

    async def get_attribute(self, name: str):
        if name == "href":
            return f"/video/{self.aweme_id}"
        return None

    async def query_selector_all(self, selector: str):
        return [TextElement(self.title)]

    async def query_selector(self, selector: str):
        return None

    async def inner_text(self):
        return self.title


class ProgressiveItemsPage:
    def __init__(self, frames):
        self.frames = frames
        self.read_count = 0

    async def query_selector_all(self, selector: str):
        index = min(self.read_count, len(self.frames) - 1)
        self.read_count += 1
        return self.frames[index]

    async def evaluate(self, script: str):
        return None


class ScopedItemsPage:
    def __init__(self, work_items, footer_items):
        self.work_items = work_items
        self.footer_items = footer_items
        self.requested_selectors = []

    async def query_selector_all(self, selector: str):
        self.requested_selectors.append(selector)
        if selector == SELECTORS["video_item"]:
            return self.work_items
        return self.work_items + self.footer_items

    async def evaluate(self, script: str):
        return None


class ScraperRecoveryTests(unittest.TestCase):
    def test_debug_page_is_not_truncated_before_video_dom(self):
        class DebugPage:
            async def content(self):
                return "x" * 60000 + '<a href="/video/123">作品</a>'

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_dir = os.getcwd()
            try:
                os.chdir(temp_dir)
                asyncio.run(VideoScraper._save_debug_page(DebugPage()))
                with open(
                    "output/debug_scraper_page.html", encoding="utf-8"
                ) as f:
                    saved = f.read()
            finally:
                os.chdir(previous_dir)

        self.assertGreater(len(saved), 60000)
        self.assertIn('/video/123', saved)

    def test_expected_total_falls_back_to_body_when_first_profile_match_is_decoy(self):
        total = asyncio.run(
            VideoScraper._extract_expected_total(ExpectedTotalPage())
        )

        self.assertEqual(total, 193)

    def test_delayed_metadata_candidate_is_retried_instead_of_discarded(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        item = DelayedMetadataItem()
        page = DelayedMetadataPage(item)

        with (
            mock.patch.object(
                VideoScraper,
                "_extract_expected_total",
                new=mock.AsyncMock(return_value=1),
            ),
            mock.patch.object(
                VideoScraper,
                "_smart_scroll",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch("src.scraper.asyncio.sleep", new=mock.AsyncMock()),
        ):
            videos = asyncio.run(
                scraper._scroll_and_extract(page, limit=1, processed_ids=set())
            )

        self.assertEqual([video.aweme_id for video in videos], ["1234567890123456789"])
        self.assertGreaterEqual(item.metadata_attempts, 2)
        self.assertEqual(scraper.last_unresolved_new_count, 0)

    def test_user_post_selector_excludes_footer_recommendations(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        known = ImmediateMetadataItem("1000000000000000001", "历史作品")
        first_new = ImmediateMetadataItem("2000000000000000001", "新增一")
        recommendation = ImmediateMetadataItem("3000000000000000001", "推荐区视频")
        page = ScopedItemsPage(
            work_items=[known, first_new],
            footer_items=[recommendation],
        )

        with (
            mock.patch.object(
                VideoScraper,
                "_extract_expected_total",
                new=mock.AsyncMock(return_value=3),
            ),
            mock.patch.object(
                VideoScraper,
                "_smart_scroll",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch("src.scraper.asyncio.sleep", new=mock.AsyncMock()),
        ):
            videos = asyncio.run(
                scraper._scroll_and_extract(
                    page,
                    limit=None,
                    processed_ids={
                        "1000000000000000001",
                        "0999999999999999999",
                    },
                )
            )

        self.assertEqual(
            [video.aweme_id for video in videos],
            ["2000000000000000001"],
        )
        self.assertEqual(scraper.last_known_seen_count, 1)
        self.assertTrue(page.requested_selectors)
        self.assertEqual(
            set(page.requested_selectors), {SELECTORS["video_item"]}
        )

    def test_metadata_falls_back_to_anchor_accessible_attributes(self):
        title, description = asyncio.run(
            VideoScraper._extract_title_and_desc(AttributeMetadataItem())
        )

        self.assertEqual(title, "来自 aria-label 的视频标题")
        self.assertEqual(description, "")

    def test_visible_count_gap_cannot_be_reported_as_no_new(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        scraper.last_expected_total = 193
        scraper.last_unresolved_new_count = 0

        error = scraper._result_consistency_error(
            processed_count=189, result_count=0
        )

        self.assertIn("差额为 4", error)
        self.assertIn("仅采集到 0", error)

    def test_candidates_exceeding_works_total_are_rejected(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        scraper.last_expected_total = 193
        error = scraper._result_consistency_error(
            processed_count=189, result_count=11
        )

        self.assertIn("差额为 4", error)
        self.assertIn("采集到 11", error)
        self.assertIn("混入推荐区", error)

    def test_checkpoint_larger_than_page_fails_without_scrolling(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        page = ProgressiveItemsPage([[]])

        with mock.patch.object(
            VideoScraper,
            "_extract_expected_total",
            new=mock.AsyncMock(return_value=193),
        ):
            videos = asyncio.run(
                scraper._scroll_and_extract(
                    page,
                    limit=None,
                    processed_ids={str(index) for index in range(200)},
                )
            )

        self.assertEqual(videos, [])
        self.assertEqual(page.read_count, 0)
        error = scraper._result_consistency_error(
            processed_count=200, result_count=0
        )
        self.assertIn("checkpoint 有 200 条", error)
        self.assertIn("滚动前停止", error)

    def test_equal_checkpoint_and_page_need_only_one_top_scan(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        page = ProgressiveItemsPage([[]])

        with mock.patch.object(
            VideoScraper,
            "_extract_expected_total",
            new=mock.AsyncMock(return_value=193),
        ):
            videos = asyncio.run(
                scraper._scroll_and_extract(
                    page,
                    limit=None,
                    processed_ids={str(index) for index in range(193)},
                )
            )

        self.assertEqual(videos, [])
        self.assertEqual(page.read_count, 1)
        self.assertIsNone(
            scraper._result_consistency_error(
                processed_count=193, result_count=0
            )
        )

    def test_equal_visible_and_checkpoint_counts_allow_empty_increment(self):
        scraper = VideoScraper(
            SimpleNamespace(request_interval_min=0, request_interval_max=0)
        )
        scraper.last_expected_total = 189
        scraper.last_unresolved_new_count = 0

        self.assertIsNone(scraper._empty_result_error(processed_count=189))


if __name__ == "__main__":
    unittest.main()
