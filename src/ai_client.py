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

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        self._page = await context.new_page()
        await self._page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)

        # REV-A-011: 检测 doubao.com 登录状态
        current_url = self._page.url
        if "/chat/" not in current_url or "login" in current_url.lower():
            raise DoubaoError("请先在浏览器中登录 doubao.com")

        return self._page

    async def generate_outline(self, title: str, video_url: str, description: str = "") -> OutlineResult:
        """贴视频 URL 到豆包，等待回复，提取提纲。

        Args:
            title: 视频标题。
            video_url: 视频链接 URL。
            description: 视频描述（可选）。

        Returns:
            OutlineResult: 包含提纲 Markdown 和原始响应。失败时 success=False。
        """
        prompt = (
            f"请分析这个视频的内容，并生成一份结构化的文字提纲"
            f"（含两级层级，一级要点和二级子要点），以 Markdown 格式输出。\n"
            f"视频链接：{video_url}\n标题：{title}"
        )

        input_selector = 'textarea[placeholder*="输入"], [contenteditable="true"]'
        send_selector = 'button[type="submit"], .send-btn'
        response_selector = '.message-content, .markdown-body, .ds-markdown'

        for retry in range(3):  # AC-008: 最多重试 2 次（共 3 次尝试）
            try:
                page = await self._ensure_page()

                # 定位输入框并输入
                await page.wait_for_selector(input_selector, timeout=10000)
                await page.fill(input_selector, prompt)
                await asyncio.sleep(1)

                # 点击发送按钮或按 Enter
                send_btn = await page.query_selector(send_selector)
                if send_btn:
                    await send_btn.click()
                else:
                    await page.keyboard.press("Enter")

                # 等待豆包回复——最长等待 120 秒（视频分析需要时间）
                await asyncio.sleep(5)  # 先等一会儿让豆包开始处理
                try:
                    await page.wait_for_selector(response_selector, timeout=120000)
                    await asyncio.sleep(3)  # 等完整内容渲染
                    response_text = await page.text_content(response_selector) or ""
                except Exception:
                    # 等待超时，尝试获取任意回复内容
                    await asyncio.sleep(10)
                    response_text = await page.text_content("body") or ""

                # 清理输入框准备下一次
                try:
                    await page.fill(input_selector, "")
                except Exception:
                    pass  # 页面可能已变化，清理失败不影响结果

                if response_text and len(response_text.strip()) > 50:
                    return OutlineResult(
                        aweme_id="",
                        outline_markdown=response_text.strip(),
                        raw_response=response_text.strip(),
                        success=True
                    )

                # 响应过短：如果不是最后一次尝试则重试
                if retry < 2:
                    # 重新导航到 /chat/ 准备下一次尝试
                    await page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)  # 等页面稳定
                    continue
                else:
                    # 最后一次尝试也过短，返回失败
                    return OutlineResult(
                        aweme_id="",
                        outline_markdown="",
                        raw_response=response_text,
                        success=False,
                        error_message="豆包返回内容过短或为空（已重试 2 次）"
                    )

            except DoubaoError:
                # 登录失败等明确错误：不重试，直接向上抛出
                raise
            except Exception as e:
                # 页面崩溃等非重试异常：不重试，直接返回失败
                return OutlineResult(
                    aweme_id="",
                    outline_markdown="",
                    raw_response=str(e),
                    success=False,
                    error_message=f"豆包调用失败: {str(e)}"
                )

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
