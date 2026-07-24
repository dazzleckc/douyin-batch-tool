"""抖音视频采集模块 —— 通过 Playwright 浏览器自动化采集博主视频列表。

用法：
    from src.config import load_config
    from src.scraper import VideoScraper, ScraperError, UserNotFoundError, NoVideosError

    config = load_config()
    scraper = VideoScraper(config)
    try:
        videos = await scraper.scrape_user_videos("https://www.douyin.com/user/MS4w...")
    finally:
        await scraper.close()
"""

import asyncio
import random
import re

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import Config
from src.models import VideoInfo

# ---------------------------------------------------------------------------
# DOM 选择器常量
# 抖音页面结构经常变化，优先匹配语义化的 a[href] 而非 class 名
# ---------------------------------------------------------------------------
SELECTORS: dict[str, str] = {
    "video_item": 'a[href*="/video/"]',
    "video_title": 'p, span',
    "video_desc": 'p, span',
    "user_not_found": '[class*="error"], [class*="not-found"]',
    "private_account": '[class*="lock"], [class*="private"]',
    "no_content": '[class*="empty"]',
}

# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------


class ScraperError(Exception):
    """采集器通用异常。"""


class UserNotFoundError(ScraperError):
    """博主不存在（页面 404 或「用户不存在」提示）。"""


class NoVideosError(ScraperError):
    """博主无公开视频。"""


class AccessDeniedError(ScraperError):
    """访问被拒绝（风控验证码 / 私密账号 / 被限制）。"""


# ---------------------------------------------------------------------------
# VideoScraper
# ---------------------------------------------------------------------------


class VideoScraper:
    """抖音视频采集器：使用 Playwright 模拟浏览器采集博主视频列表。"""

    def __init__(self, config: Config) -> None:
        """保存配置，暂不启动浏览器。

        Args:
            config: 应用运行时配置（含 Cookie、headless 等参数）。
        """
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None

    # --- 公开方法 ------------------------------------------------------------

    async def scrape_user_videos(
        self, user_url: str, limit: int | None = None
    ) -> list[VideoInfo]:
        """采集指定博主主页的公开视频列表。

        Args:
            user_url: 抖音博主主页 URL（如 https://www.douyin.com/user/MS4w...）。
            limit:   最大采集视频数，None 表示不限制。

        Returns:
            list[VideoInfo]: 视频信息列表。

        Raises:
            UserNotFoundError: 博主不存在。
            NoVideosError:     博主无公开视频。
            AccessDeniedError: 访问被拒绝或触发风控。
            ScraperError:      其他采集阶段错误。
        """
        sec_uid = self._extract_sec_uid(user_url)
        cookies = self._parse_cookie(self._config.douyin_cookie)
        user_page_url = f"https://www.douyin.com/user/{sec_uid}"

        context = await self._create_context(cookies)
        page: Page = await context.new_page()

        try:
            # 1) 先访问抖音首页（模拟自然浏览，绕过"新Tab"拦截）
            try:
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(random.uniform(2, 4))
            except Exception as exc:
                raise ScraperError(f"抖音首页加载失败: {exc}") from exc

            # 2) 再导航到博主主页
            try:
                await page.goto(user_page_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                raise ScraperError(f"页面加载超时或失败: {exc}") from exc

            # 3) 等待首屏渲染，判断页面状态
            await asyncio.sleep(random.uniform(2, 5))
            await self._detect_error_state(page)

            # 3) 滚动加载并提取视频列表
            videos = await self._scroll_and_extract(page, limit)

            if not videos:
                # 保存截图帮助调试
                try:
                    await page.screenshot(path="debug_scraper_failure.png", full_page=True)
                except Exception:
                    pass
                raise NoVideosError(f"博主 {sec_uid} 没有可采集的公开视频（截图已保存到 debug_scraper_failure.png）")

            return videos

        finally:
            await context.close()

    async def close(self) -> None:
        """关闭浏览器与 Playwright 资源。"""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # --- 内部方法 ------------------------------------------------------------

    def _extract_sec_uid(self, user_url: str) -> str:
        """从抖音用户 URL 中提取 sec_uid。

        支持格式:
            - https://www.douyin.com/user/MS4wLjABAAAA...
            - https://www.douyin.com/user/MS4wLjAB...?modal_id=...
            - https://www.douyin.com/share/user/...
            - v.douyin.com 短链（不在此处展开，调用方应预先解析）

        Raises:
            ScraperError: URL 无法识别为有效的抖音用户链接。
        """
        pattern = r"douyin\.com/user/([A-Za-z0-9_-]+)"
        match = re.search(pattern, user_url)
        if not match:
            raise ScraperError(
                f"无法从 URL 提取用户标识 (sec_uid): {user_url}"
            )
        return match.group(1)

    @staticmethod
    def _parse_cookie(cookie_str: str) -> dict[str, str]:
        """解析 Cookie 字符串为键值对字典。

        支持两种格式:
            - 简单 key=value: ``"uid=123; sid=abc"``
            - 完整 Cookie header: 忽略 Domain/Path/Expires 等属性，仅提取 name=value。

        Args:
            cookie_str: 原始 Cookie 字符串。

        Returns:
            dict[str, str]: {cookie_name: cookie_value}。
        """
        result: dict[str, str] = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            # 跳过 Cookie 属性（Domain/Path/Expires/Secure/HttpOnly 等）
            if key.lower() in {
                "domain", "path", "expires", "max-age", "secure",
                "httponly", "samesite", "priority", "partitioned",
            }:
                continue
            if key:
                result[key] = value
        return result

    async def _create_context(self, cookies: dict[str, str]) -> BrowserContext:
        """启动浏览器并创建已注入 Cookie 的上下文。

        Args:
            cookies: 解析后的 Cookie 字典。

        Returns:
            BrowserContext: 已注入 Cookie 的浏览器上下文。
        """
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None:
            self._browser = await self._playwright.chromium.launch(
                headless=self._config.headless,
            )

        context = await self._browser.new_context()

        cookie_list = [
            {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
            for name, value in cookies.items()
        ]
        if cookie_list:
            await context.add_cookies(cookie_list)

        return context

    async def _detect_error_state(self, page: Page) -> None:
        """检测页面是否进入错误状态。

        检查顺序：用户不存在 → 访问被拒 → 无内容。
        选择器为占位值，需根据抖音实际 DOM 调整。

        Raises:
            UserNotFoundError / AccessDeniedError / NoVideosError。
        """
        # 检查 URL 是否被重定向到错误页
        current_url = page.url
        if "douyin.com" not in current_url:
            raise AccessDeniedError(f"页面被重定向: {current_url}")

        # 通过占位选择器检测（失败时不做硬判断，由后续滚动结果决定）
        not_found = await page.query_selector(SELECTORS["user_not_found"])
        if not_found is not None:
            text = await not_found.inner_text()
            if "不存在" in text or "not found" in text.lower():
                raise UserNotFoundError(f"博主不存在: {current_url}")

        private_el = await page.query_selector(SELECTORS["private_account"])
        if private_el is not None:
            raise AccessDeniedError("该账号为私密账号，无法采集")

        # 无内容不在此处抛出（交由 _scroll_and_extract 判断），仅做日志级检测
        empty_el = await page.query_selector(SELECTORS["no_content"])
        if empty_el is not None:
            # 页面明确无内容标记，可能无视频
            pass

    async def _scroll_and_extract(
        self, page: Page, limit: int | None
    ) -> list[VideoInfo]:
        """滚动加载页面并逐批提取视频信息。

        每次滚动后等待随机间隔，直到没有新视频出现或达到 limit 上限。

        Args:
            page:  已加载博主主页的 Playwright Page。
            limit: 最大采集数。

        Returns:
            list[VideoInfo]: 采集到的视频列表。
        """
        videos: list[VideoInfo] = []
        seen_ids: set[str] = set()
        no_new_count = 0
        max_no_new = 3  # 连续无新视频的停止阈值

        # 从 Config 读取间隔范围
        interval_min = self._config.request_interval_min
        interval_max = self._config.request_interval_max

        while True:
            # 提取当前视图中所有视频
            items = await page.query_selector_all(SELECTORS["video_item"])
            new_found = False

            for item in items:
                href = await item.get_attribute("href")
                if not href:
                    continue

                aweme_id = self._extract_aweme_id(href)
                if not aweme_id or aweme_id in seen_ids:
                    continue
                seen_ids.add(aweme_id)
                new_found = True

                # 组装完整 URL
                full_url = f"https://www.douyin.com{href}" if href.startswith("/") else href

                # 提取标题
                title = ""
                title_el = await item.query_selector(SELECTORS["video_title"])
                if title_el is not None:
                    title = (await title_el.inner_text()).strip()

                # 提取描述
                desc = ""
                desc_el = await item.query_selector(SELECTORS["video_desc"])
                if desc_el is not None:
                    desc = (await desc_el.inner_text()).strip()

                videos.append(VideoInfo(
                    aweme_id=aweme_id,
                    url=full_url,
                    title=title,
                    description=desc,
                ))

                if limit is not None and len(videos) >= limit:
                    return videos[:limit]

            # 判断是否继续滚动
            if not new_found:
                no_new_count += 1
            else:
                no_new_count = 0

            if no_new_count >= max_no_new:
                break

            # 滚动并等待新内容渲染
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(interval_min, interval_max))

        return videos

    @staticmethod
    def _extract_aweme_id(href: str) -> str:
        """从视频链接中提取 aweme_id。

        支持格式:
            - /video/7123456789012345678
            - /video/7123456789012345678?previous_page=...

        Args:
            href: 视频相对链接。

        Returns:
            str: aweme_id 数字串，解析失败返回空字符串。
        """
        match = re.search(r"/video/(\d+)", href)
        return match.group(1) if match else ""
