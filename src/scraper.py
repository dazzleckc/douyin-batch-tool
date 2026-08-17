"""抖音视频采集模块 —— 通过 Playwright 浏览器自动化采集博主视频列表。

登录态优先级：
    1. douyin_state.json（storage_state，由 login_douyin.py 生成）
    2. DOUYIN_COOKIE 环境变量（回退方案）

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
import json as _json
import os as _os
import random
import re
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import Config
from src.models import VideoInfo

# 支持短链解析
_HTTPX_CLIENT = None

def _get_http_client():
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None:
        _HTTPX_CLIENT = httpx.Client(follow_redirects=True, timeout=10)
    return _HTTPX_CLIENT

def resolve_douyin_url(raw_url: str) -> str:
    """解析抖音短链为完整 URL。"""
    # 移除复制粘贴时混入的非 URL 字符（如 "$7 CA1282 9@0.com :9pm" 等）
    url = raw_url.strip()
    # 如果包含空格，取第一个看起来像 URL 的部分
    if " " in url:
        parts = url.split()
        for p in parts:
            if "douyin.com" in p or "iesdouyin.com" in p:
                url = p
                break
    
    if "v.douyin.com" not in url:
        return url  # 不是短链，直接返回
    
    try:
        client = _get_http_client()
        resp = client.head(url)
        final_url = str(resp.url)
        return final_url
    except Exception:
        return url  # 解析失败，退回原始 URL

# ---------------------------------------------------------------------------
# DOM 选择器常量
# 抖音页面结构经常变化，优先匹配语义化的 a[href] 而非 class 名
# ---------------------------------------------------------------------------
SELECTORS: dict[str, str] = {
    "video_item": '[data-e2e="user-post-list"] a[href*="/video/"]',
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


class NotLoggedInError(ScraperError):
    """未登录或登录态过期，无法正常采集。"""


# ---------------------------------------------------------------------------
# VideoScraper
# ---------------------------------------------------------------------------


class VideoScraper:
    """抖音视频采集器：使用 Playwright 模拟浏览器采集博主视频列表。

    登录态优先级：storage_state (douyin_state.json) > Cookie 注入。
    支持增量模式：传入 ProcessDB 后，采集到已处理视频时自动停止滚动。
    """

    def __init__(self, config: Config, process_db=None) -> None:
        """保存配置，暂不启动浏览器。

        Args:
            config:     应用运行时配置（含 Cookie、headless、storage_state 路径等）。
            process_db: 可选，ProcessDB 实例。传入后启用增量采集模式。
        """
        self._config = config
        self._process_db = process_db
        self._playwright = None
        self._browser: Browser | None = None
        self.last_expected_total: int | None = None
        self.last_visible_video_count = 0
        self.last_known_seen_count = 0
        self.last_unresolved_new_count = 0
        self.last_accounted_total = 0

    # --- 公开方法 ------------------------------------------------------------

    async def scrape_user_videos(
        self,
        user_url: str,
        limit: int | None = None,
        processed_ids: set[str] | None = None,
    ) -> list[VideoInfo]:
        """采集指定博主主页的公开视频列表。

        Args:
            user_url: 抖音博主主页 URL（如 https://www.douyin.com/user/MS4w...）。
            limit:         最大采集视频数，None 表示不限制。
            processed_ids: 当前博主已在 checkpoint 中的 aweme_id；未传时
                           兼容旧调用，从 process_db 读取。

        Returns:
            list[VideoInfo]: 视频信息列表。

        Raises:
            UserNotFoundError: 博主不存在。
            NoVideosError:     博主无公开视频。
            AccessDeniedError: 访问被拒绝或触发风控。
            ScraperError:      其他采集阶段错误。
        """
        sec_uid = self._extract_sec_uid(user_url)
        user_page_url = f"https://www.douyin.com/user/{sec_uid}"

        # 加载已处理ID列表（增量模式）
        if processed_ids is None:
            processed_ids = set()
            if self._process_db is not None:
                processed_ids = self._process_db.get_known_ids()
        else:
            processed_ids = set(processed_ids)

        context = await self._create_context()
        page: Page = await context.new_page()

        try:
            # 1) 访问博主主页
            try:
                await page.goto(user_page_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                raise ScraperError(f"页面加载超时或失败: {exc}") from exc

            # 2) 等待首屏视频列表渲染（SPA 需要额外等待）
            await asyncio.sleep(random.uniform(5, 8))
            await page.evaluate("window.scrollTo(0, 600)")
            await asyncio.sleep(2)
            await self._detect_error_state(page)

            # 2.5) 检测登录状态（登录态过期/无效时明确报错，不静默继续）
            await self._check_login_state(page)

            # 3) 滚动加载并提取视频列表
            videos = await self._scroll_and_extract(page, limit, processed_ids)

            inconsistent_error = self._result_consistency_error(
                len(processed_ids), len(videos)
            )
            if inconsistent_error:
                await self._save_debug_page(page)
                raise ScraperError(inconsistent_error)

            if not videos:
                # 增量模式：全部已知则正常返回空列表（非错误）
                if processed_ids:
                    return videos
                await self._save_debug_page(page)
                raise NoVideosError(
                    f"博主 {sec_uid} 没有可采集的公开视频"
                    f"（页面源码已保存到 output/debug_scraper_page.html）"
                )

            return videos

        finally:
            await context.close()

    def _result_consistency_error(
        self, processed_count: int, result_count: int
    ) -> str | None:
        """拒绝与页面作品总数冲突的结果，避免推荐区链接进入 checkpoint。"""
        if self.last_expected_total is None:
            return self._empty_result_error(processed_count) if result_count == 0 else None

        if processed_count > self.last_expected_total:
            return (
                f"当前博主 checkpoint 有 {processed_count} 条，但页面仅显示 "
                f"{self.last_expected_total} 个作品。已在滚动前停止；"
                "请先修复 checkpoint 中的非作品记录。页面源码已保存到 "
                "output/debug_scraper_page.html"
            )

        expected_increment = self.last_expected_total - processed_count
        if result_count > expected_increment:
            return (
                f"页面与 checkpoint 的差额为 {expected_increment}，但采集到 "
                f"{result_count} 个新增候选，疑似混入推荐区视频。"
                "已停止且不会更新 checkpoint；页面源码已保存到 "
                "output/debug_scraper_page.html"
            )
        if result_count < expected_increment:
            return (
                f"页面与 checkpoint 的差额为 {expected_increment}，但仅采集到 "
                f"{result_count} 个新增候选。已停止以避免漏采；页面源码已保存到 "
                "output/debug_scraper_page.html"
            )
        return None

    def _empty_result_error(self, processed_count: int) -> str | None:
        """判断空结果是否与页面或 checkpoint 事实冲突。"""
        if self.last_unresolved_new_count:
            return (
                f"页面发现 {self.last_unresolved_new_count} 个未处理视频链接，"
                "但标题/描述持续未渲染，已停止以避免误报无新增。"
                "页面源码已保存到 output/debug_scraper_page.html"
            )
        if (
            self.last_expected_total is not None
            and self.last_expected_total > processed_count
        ):
            return (
                f"页面显示 {self.last_expected_total} 个作品，但当前博主 "
                f"checkpoint 仅有 {processed_count} 条，采集结果却为 0。"
                "已停止以避免漏采；页面源码已保存到 "
                "output/debug_scraper_page.html"
            )
        return None

    @staticmethod
    async def _save_debug_page(page: Page) -> None:
        """保存完整诊断页面，不影响原 checkpoint/DB。"""
        try:
            html = await page.content()
            _os.makedirs("output", exist_ok=True)
            with open("output/debug_scraper_page.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

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
            - https://www.iesdouyin.com/share/user/MS4wLjAB...?sec_uid=...
            - https://v.douyin.com/xxxxx/ (短链，调用方应先 resolve_douyin_url)
            - 直接传入纯 sec_uid

        Raises:
            ScraperError: URL 无法识别为有效的抖音用户链接。
        """
        # 短链解析
        user_url = resolve_douyin_url(user_url)
        
        # 尝试多种模式提取 sec_uid
        patterns = [
            r"douyin\.com/user/([A-Za-z0-9_-]+)",
            r"iesdouyin\.com/share/user/([A-Za-z0-9_-]+)",
            r"sec_uid=([A-Za-z0-9_-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, user_url)
            if match:
                return match.group(1)
        
        # 如果整个 URL 看起来就是一个纯 sec_uid（以 MS4w 开头）
        if user_url.startswith("MS4w") and len(user_url) > 30:
            return user_url
        
        raise ScraperError(
            f"无法从 URL 提取用户标识 (sec_uid): {user_url}"
        )

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

    async def _create_context(self) -> BrowserContext:
        """启动浏览器并创建上下文。

        登录态优先级：
            1. douyin_state.json storage_state（推荐，由 login_douyin.py 生成）
            2. DOUYIN_COOKIE 环境变量（回退方案）

        Returns:
            BrowserContext: 已注入登录态的浏览器上下文。
        """
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None:
            self._browser = await self._playwright.chromium.launch(
                headless=self._config.headless,
            )

        browser_args: dict = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # 方案1: 使用 storage_state（弹窗登录保存的文件）
        state_path = Path(self._config.douyin_state_path)
        if state_path.exists():
            try:
                with open(state_path) as f:
                    state = _json.load(f)
                browser_args["storage_state"] = state
                return await self._browser.new_context(**browser_args)
            except Exception:
                pass  # state 文件损坏，回退到方案2

        # 方案2: 使用 Cookie 注入（需先访问域名）
        context = await self._browser.new_context(**browser_args)
        cookies = self._parse_cookie(self._config.douyin_cookie)
        if cookies:
            # 必须先访问域名后 Cookie 才能注入
            page = await context.new_page()
            try:
                await page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            except Exception:
                pass
            await page.close()

            cookie_list = [
                {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
                for name, value in cookies.items()
            ]
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
        else:
            # 兜底：检查页面主体文本
            body_text = await page.text_content("body") or ""
            if "用户不存在" in body_text or "作品不存在" in body_text:
                raise UserNotFoundError(f"博主不存在: {current_url}")

        private_el = await page.query_selector(SELECTORS["private_account"])
        if private_el is not None:
            text = await private_el.text_content() or ""
            # 只有页面确实包含"私密"文字才认定
            if "私密" in text or "private" in text.lower():
                raise AccessDeniedError("该账号为私密账号，无法采集")

        # 无内容不在此处抛出（交由 _scroll_and_extract 判断），仅做日志级检测
        empty_el = await page.query_selector(SELECTORS["no_content"])
        if empty_el is not None:
            # 页面明确无内容标记，可能无视频
            pass

    async def _check_login_state(self, page: Page) -> None:
        """检测抖音登录状态，未登录或登录过期时抛出 NotLoggedInError。

        检测依据：
            1. URL 被重定向到登录页
            2. 页面出现明显未登录特征（「登录」按钮 + 无用户信息）
            3. 页面主体提示需要登录

        Raises:
            NotLoggedInError: 未登录或登录态过期。
        """
        current_url = page.url

        # 1) URL 包含 login → 明确被重定向到登录页
        if "login" in current_url.lower():
            raise NotLoggedInError(
                "抖音未登录（页面被重定向到登录页）。\n"
                "请运行 python login_douyin.py 弹窗登录后重试。"
            )

        # 2) 检查页面是否包含明显的“未登录”特征
        try:
            body_text = (await page.text_content("body")) or ""
        except Exception:
            body_text = ""

        # 页面顶部出现了需要登录的提示
        not_logged_signals = [
            "请先登录", "立即登录", "登录后即可",
        ]
        for signal in not_logged_signals:
            if signal in body_text[:3000]:
                # 二次确认：是否同时缺少用户信息
                has_user_info = any(
                    kw in body_text[:3000] for kw in ["粉丝", "关注", "获赞", "作品"]
                )
                if not has_user_info:
                    raise NotLoggedInError(
                        "抖音未登录或登录态已过期（页面提示需要登录）。\n"
                        "请执行以下任一操作：\n"
                        "  1) python login_douyin.py  弹窗扫码登录（推荐）\n"
                        "  2) 更新 .env 中的 DOUYIN_COOKIE 为有效值"
                    )

        # 3) 如果是用 Cookie 回退方案（而非 storage_state），检测页面是否显示了正常用户信息
        from pathlib import Path as _Path
        using_cookie_fallback = (
            not _Path(self._config.douyin_state_path).exists()
            and bool(self._config.douyin_cookie)
        )
        if using_cookie_fallback:
            # Cookie 回退时，额外检查页面是否真的是已登录状态
            # 抖音已登录用户页面通常包含：关注数/粉丝数/获赞数 等统计
            has_stats = any(
                kw in body_text[:5000] for kw in ["获赞", "粉丝", "关注"]
            )
            if not has_stats:
                print(
                    "\n⚠️  警告：使用 .env DOUYIN_COOKIE 但登录态可能已过期。"
                    "\n   建议运行 python login_douyin.py 重新登录。"
                )
                # 不抛异常——让用户看到警告但允许继续尝试（可能只是页面还没加载完）

    async def _scroll_and_extract(
        self, page: Page, limit: int | None, processed_ids: set[str] | None = None
    ) -> list[VideoInfo]:
        """滚动加载页面并逐批提取视频信息。

        改进策略：
            1. 从页面提取「作品N」作为目标总数，采集到目标才停止。
            2. 滚动到最后一个视频元素触发懒加载（比 scrollHeight 更可靠）。
            3. 无进展时逐渐拉长等待时间（退避策略），而非固定阈值立即放弃。

        Args:
            page:         已加载博主主页的 Playwright Page。
            limit:        最大采集数。
            processed_ids: 已处理视频的 aweme_id 集合（None 表示非增量模式）。

        Returns:
            list[VideoInfo]: 采集到的视频列表。
        """
        videos: list[VideoInfo] = []
        seen_ids: set[str] = set()
        visible_ids: set[str] = set()
        known_seen_ids: set[str] = set()
        unresolved_new_ids: set[str] = set()
        processed_ids = set(processed_ids or set())
        expected_total = await self._extract_expected_total(page)
        interval_min = self._config.request_interval_min
        interval_max = self._config.request_interval_max

        def publish_diagnostics() -> None:
            self.last_expected_total = expected_total
            self.last_visible_video_count = len(visible_ids)
            self.last_known_seen_count = len(known_seen_ids)
            self.last_unresolved_new_count = len(unresolved_new_ids)
            self.last_accounted_total = len(known_seen_ids) + len(videos)

        publish_diagnostics()

        if expected_total:
            print(f"   📊 页面显示作品数: {expected_total}")

        # checkpoint 比页面作品数还多是确定性污染，不需要滚动页面确认。
        if expected_total is not None and len(processed_ids) > expected_total:
            publish_diagnostics()
            return []

        expected_increment = (
            max(expected_total - len(processed_ids), 0)
            if expected_total is not None
            else None
        )

        all_processed_page_count = 0
        max_all_processed_pages = 2
        no_new_count = 0
        # 新作品位于主页顶部：首屏加一次复核即可，避免空转八轮。
        max_no_new_base = 2
        backoff_mul = 1.0

        while True:
            # 提取当前视图中所有视频
            items = await page.query_selector_all(SELECTORS["video_item"])
            new_found = False
            page_all_processed = True

            for item in items:
                href = await item.get_attribute("href")
                if not href:
                    continue

                aweme_id = self._extract_aweme_id(href)
                if not aweme_id or aweme_id in seen_ids:
                    continue
                visible_ids.add(aweme_id)

                # 增量模式：跳过已处理的视频
                if processed_ids and aweme_id in processed_ids:
                    seen_ids.add(aweme_id)
                    known_seen_ids.add(aweme_id)
                    continue

                page_all_processed = False

                full_url = f"https://www.douyin.com{href}" if href.startswith("/") else href
                title, desc = await self._extract_title_and_desc(item)

                # 标题/描述可能晚于链接渲染；此时保留候选，下一轮继续尝试。
                if not title and not desc:
                    unresolved_new_ids.add(aweme_id)
                    continue

                unresolved_new_ids.discard(aweme_id)
                seen_ids.add(aweme_id)
                new_found = True

                # 检测置顶标记
                is_pinned = "置顶" in (await item.inner_text())

                videos.append(VideoInfo(
                    aweme_id=aweme_id,
                    url=full_url,
                    title=title,
                    description=desc,
                    is_pinned=is_pinned,
                ))

                if limit is not None and len(videos) >= limit:
                    publish_diagnostics()
                    print(f"   📊 已采集: {len(videos)} (达到 limit={limit})")
                    return videos[:limit]

            publish_diagnostics()

            # 进度输出
            if new_found:
                if expected_total:
                    print(
                        f"   📊 已采集新增: {len(videos)} "
                        f"(checkpoint {len(processed_ids)} / 页面 {expected_total})"
                    )
                else:
                    print(f"   📊 已采集新增: {len(videos)}")

            # 达到页面与 checkpoint 的预期差额后，再等一轮确认顶部无遗漏。
            expected_gap = bool(
                expected_increment is not None
                and len(videos) < expected_increment
            )
            # 无新视频时退避
            if not new_found:
                no_new_count += 1
                backoff_mul = 1.0 + (no_new_count - 1) * 0.5  # 每次无新 +50% 等待
            else:
                no_new_count = 0
                backoff_mul = 1.0

            if (
                expected_increment is not None
                and len(videos) >= expected_increment
                and no_new_count >= 1
            ):
                print(
                    f"   ✅ 顶部复核完成: 新增 {len(videos)} "
                    f"(预期差额 {expected_increment})"
                )
                break

            # 增量模式：整页全已处理时提前停止
            if (
                processed_ids
                and items
                and page_all_processed
                and not unresolved_new_ids
                and not expected_gap
            ):
                all_processed_page_count += 1
                if all_processed_page_count >= max_all_processed_pages:
                    break
            else:
                all_processed_page_count = 0

            # 两轮无进展就停止，由结果一致性检查决定成功或明确报错。
            effective_max = max_no_new_base

            if no_new_count >= effective_max:
                if expected_gap:
                    print(f"   ⚠️  顶部复核未达到预期差额: "
                          f"新增 {len(videos)} / 预期 {expected_increment}")
                break

            # 滚动——优先滚到最后一个视频元素，触发懒加载
            # 卡住多轮后改用全页滚动（兜底触发深处懒加载）
            if no_new_count >= 3:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(max(3.0 * backoff_mul, interval_max))
            else:
                await self._smart_scroll(page, items, backoff_mul, interval_min, interval_max)

        # 循环结束后的兜底提取：可能有最后一两个视频刚被懒加载渲染
        if (
            expected_increment is not None
            and len(videos) < expected_increment
        ) or unresolved_new_ids:
            before_final = len(videos)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(4.0)
            items_final = await page.query_selector_all(SELECTORS["video_item"])
            for item in items_final:
                href = await item.get_attribute("href")
                if not href:
                    continue
                aweme_id = self._extract_aweme_id(href)
                if not aweme_id or aweme_id in seen_ids:
                    continue
                visible_ids.add(aweme_id)
                if processed_ids and aweme_id in processed_ids:
                    seen_ids.add(aweme_id)
                    known_seen_ids.add(aweme_id)
                    continue
                full_url = f"https://www.douyin.com{href}" if href.startswith("/") else href
                title, desc = await self._extract_title_and_desc(item)
                if not title and not desc:
                    unresolved_new_ids.add(aweme_id)
                    continue
                unresolved_new_ids.discard(aweme_id)
                seen_ids.add(aweme_id)
                is_pinned = "置顶" in (await item.inner_text())
                videos.append(VideoInfo(
                    aweme_id=aweme_id, url=full_url, title=title,
                    description=desc, is_pinned=is_pinned,
                ))
            if len(videos) > before_final:
                print(f"   📊 已采集新增: {len(videos)} (兜底补采 {len(videos) - before_final} 个)")

        publish_diagnostics()
        return videos

    @staticmethod
    async def _extract_expected_total(page: Page) -> int | None:
        """从页面用户信息区域提取「作品 N」中的数字 N。

        优先在 Tab 标签区域查找（更精确），回退到全页面搜索。
        """
        import re as _re

        def parse_total(text: str) -> int | None:
            match = _re.search(r"作品\s*[:：]?\s*(\d[\d,]*)", text)
            if match:
                return int(match.group(1).replace(",", ""))
            return None

        # 广泛 class 选择器可能先命中无关 profile；逐个检查，未找到再回退 body。
        try:
            tab_areas = await page.query_selector_all(
                '[class*="tab"], [class*="profile"], [class*="user-tab"]'
            )
            for tab_area in tab_areas:
                total = parse_total(await tab_area.text_content() or "")
                if total is not None:
                    return total
        except Exception:
            pass

        try:
            return parse_total(await page.text_content("body") or "")
        except Exception:
            return None

    @staticmethod
    async def _extract_title_and_desc(item) -> tuple[str, str]:
        """从视频 DOM 元素提取标题和描述文本。"""
        title = ""
        title_els = await item.query_selector_all(SELECTORS["video_title"])
        best_text = ""
        for t_el in title_els:
            t = (await t_el.inner_text()).strip()
            if len(t) > len(best_text):
                best_text = t
        title = best_text

        desc = ""
        desc_el = await item.query_selector(SELECTORS["video_desc"])
        if desc_el is not None:
            desc = (await desc_el.inner_text()).strip()

        if not title and not desc:
            fallback_candidates: list[str] = []

            # 新版卡片可能把标题放在链接自身的可访问属性中。
            for attr in ("aria-label", "title"):
                try:
                    value = (await item.get_attribute(attr) or "").strip()
                except Exception:
                    value = ""
                if value:
                    fallback_candidates.append(value)

            # 图片 alt、子元素 aria-label/title 也是常见的语义来源。
            try:
                semantic_nodes = await item.query_selector_all(
                    '[aria-label], [title], img[alt]'
                )
            except Exception:
                semantic_nodes = []
            for node in semantic_nodes:
                for attr in ("aria-label", "title", "alt"):
                    try:
                        value = (await node.get_attribute(attr) or "").strip()
                    except Exception:
                        value = ""
                    if value:
                        fallback_candidates.append(value)

            # 最后回退到链接自身文本；过滤纯“置顶”等非标题噪声。
            try:
                own_text = (await item.inner_text()).strip()
            except Exception:
                own_text = ""
            if own_text:
                fallback_candidates.append(own_text)

            noise = {"置顶", "播放", "点赞", "评论", "分享"}
            meaningful = [
                text for text in fallback_candidates
                if text not in noise and not text.startswith("http")
            ]
            if meaningful:
                title = max(meaningful, key=len)
        return title, desc

    @staticmethod
    async def _smart_scroll(
        page: Page, items: list, backoff_mul: float,
        interval_min: float, interval_max: float,
    ) -> None:
        """智能滚动：优先滚到最后一个视频元素触发懒加载，回退到 scrollHeight。"""
        if items:
            try:
                last_item = items[-1]
                await last_item.scroll_into_view_if_needed()
            except Exception:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # 等待渲染 + 退避
        wait = random.uniform(interval_min, interval_max) * backoff_mul
        # 至少等 2 秒（抖音懒加载需要网络请求时间）
        wait = max(wait, 2.0 * backoff_mul)
        await asyncio.sleep(wait)

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
