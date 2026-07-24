"""豆包 AI 客户端：通过 Playwright 自动化 doubao.com，贴视频 URL 让豆包生成提纲。

用法：
    from src.ai_client import DoubaoClient

    doubao = DoubaoClient(headless=True)
    result = await doubao.generate_outline("视频标题", "https://...", "描述")
    if result.success:
        print(result.outline_markdown)
    await doubao.close()
"""

import asyncio
import re
from typing import Optional
from playwright.async_api import async_playwright, Page, Browser

from src.models import OutlineResult


class DoubaoError(Exception):
    """豆包客户端通用异常。"""


class DoubaoClient:
    """通过 Playwright 自动化 doubao.com，贴视频 URL 让豆包生成提纲。"""

    DOUBAO_URL = "https://www.douyin.com"  # 先访问抖音保持 Cookie 域一致
    DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"

    def __init__(self, headless: bool = True, cookies: dict[str, str] | None = None):
        self._headless = headless
        self._cookies = cookies or {}
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        
        # 优先使用 storageState（比 Cookie 注入更可靠）
        import os as _os, json as _json
        browser_args = {}
        state_path = "doubao_state.json"
        if _os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = _json.load(f)
                context = await self._browser.new_context(
                    storage_state=state,
                    viewport={"width": 1280, "height": 900},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                )
            except Exception:
                context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                )
        else:
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            # 注入豆包 Cookie（如果有）
            if self._cookies:
                from urllib.parse import unquote
                cookie_list = [
                    {"name": name, "value": unquote(value), "domain": ".doubao.com", "path": "/"}
                    for name, value in self._cookies.items()
                ]
                await context.add_cookies(cookie_list)
        
        self._page = await context.new_page()
        await self._page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        
        # 如果有 Cookie 但没有 storageState，注入后刷新
        if self._cookies and not _os.path.exists(state_path):
            await self._page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        # REV-A-011: 检测 doubao.com 登录状态
        current_url = self._page.url
        if "/chat/" not in current_url or "login" in current_url.lower():
            raise DoubaoError("请先在浏览器中登录 doubao.com")

        return self._page

    async def generate_outline(self, title: str, video_url: str, description: str = "") -> OutlineResult:
        """贴视频 URL 到豆包，等待回复，提取提纲（增量提取法）。"""
        prompt = (
            f"请分析这个视频的内容，并生成一份结构化的文字提纲"
            f"（含两级层级，一级要点和二级子要点），以 Markdown 格式输出。\n"
            f"视频链接：{video_url}\n标题：{title}"
        )

        input_selector = 'textarea[placeholder="发消息..."]'

        for retry in range(3):
            try:
                page = await self._ensure_page()

                # 填入 prompt
                await page.wait_for_selector(input_selector, timeout=10000)
                await page.fill(input_selector, prompt)
                await asyncio.sleep(1)

                # 记录发送前页面文本
                try:
                    before = await page.evaluate("() => document.body.innerText")
                except Exception:
                    before = ""

                # 发送
                await page.keyboard.press("Enter")

                # 轮询等待 AI 回复（最长 120s）
                response_text = ""
                for _ in range(60):  # 60 × 2s
                    await asyncio.sleep(2)
                    try:
                        now = await page.evaluate("() => document.body.innerText")
                    except Exception:
                        continue
                    if now and len(now) > len(before) + 100:
                        # 提取增量
                        idx = now.find(prompt)
                        new = now[idx + len(prompt):].strip() if idx >= 0 else now[len(before):].strip()
                        if len(new) > 100:
                            # 再等 5 秒确保回复完整
                            await asyncio.sleep(5)
                            try:
                                now2 = await page.evaluate("() => document.body.innerText")
                                idx2 = now2.find(prompt)
                                response_text = now2[idx2 + len(prompt):].strip() if idx2 >= 0 else new
                            except Exception:
                                response_text = new
                            break

                # 清理输入框
                try:
                    await page.fill(input_selector, "")
                except Exception:
                    pass

                if response_text and len(response_text.strip()) > 50:
                    return OutlineResult("", response_text.strip(), response_text.strip(), True)

                if retry < 2:
                    await page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    continue
                return OutlineResult("", "", response_text, False, "豆包返回内容过短（已重试 2 次）")

            except DoubaoError:
                raise
            except Exception as e:
                return OutlineResult("", "", str(e), False, f"豆包调用失败: {e}")

    async def close(self):
        """关闭浏览器资源。"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
