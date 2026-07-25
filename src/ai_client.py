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
import json as _json
import os as _os
import re
from typing import Optional
from urllib.parse import unquote

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from src.models import OutlineResult


class DoubaoError(Exception):
    """豆包客户端通用异常。"""


class ReviewBlockedError(DoubaoError):
    """人审拦截——豆包回复被内容安全触发，需人工处理。"""


class DoubaoClient:
    """通过 Playwright 自动化 doubao.com，贴视频 URL 让豆包生成提纲。

    每个 generate_outline 调用独立 Page，防止跨视频上下文污染。
    """

    DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"
    INPUT_SELECTOR = 'textarea[placeholder="发消息..."]'

    PROMPT_TEMPLATE = (
        "无需查阅其他参考资料，请获取以下抖音视频的内容，并为其生成一个详细的内容大纲。"
        "大纲应包含：核心观点、逻辑结构、关键论据。\n\n"
        "视频链接：{url}"
    )

    # prompt[:50] 锚定后残留的固定碎片
    PROMPT_ECHO_PATTERNS = [
        r"^逻辑结构、关键论据。[ \t]*\n?",
        r"^视频链接：.*?\n",
        r"^\s*\n",
    ]

    UI_NOISE_PATTERNS = [
        r"搜索\s*\d+\s*个关键词[，,]\s*参考\s*\d+\s*篇资料\s*\n?",
    ]

    # 流式回复稳定检测参数
    POLL_INTERVAL = 5
    REQUIRED_STABLE = 2
    REPLY_TIMEOUT = 300

    # DOM 级验证码检测：豆包可能弹出图片验证码/slider/hCaptcha 而不是文本回复
    CAPTCHA_TOKEN_RE = re.compile(
        r"20\d{2}\([01]\d[0-3]\d\)[A-F0-9]{20,}"
    )
    CAPTCHA_TEXT_PATTERNS = [
        "请选择所有符合上文描述的图片",
        "拖拽到下方",
        "拖拽到这里",
        "提交并继续",
        "Verify you are human",
        "Are you a human",
        "Let's verify you are human",
    ]
    CAPTCHA_IFRAME_SELECTOR = (
        'iframe[src*="captcha"], iframe[src*="hcaptcha"], '
        'iframe[src*="arkose"], iframe[src*="geetest"], '
        'iframe[src*="bytedance.com/verifycenter"]'
    )
    CAPTCHA_TIMEOUT = 180       # 人机验证等待上限（秒）
    CAPTCHA_POLL_INTERVAL = 10  # 等待期间轮询间隔（秒）

    def __init__(self, headless: bool = True, cookies: dict[str, str] | None = None):
        self._headless = headless
        self._cookies = cookies or {}
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def _ensure_context(self) -> BrowserContext:
        """延迟创建浏览器上下文（storage_state 优先，Cookie 注入兜底）。"""
        if self._context is not None:
            return self._context

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)

        browser_args = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        state_path = "doubao_state.json"
        if _os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = _json.load(f)
                self._context = await self._browser.new_context(storage_state=state, **browser_args)
                return self._context
            except Exception:
                pass

        self._context = await self._browser.new_context(**browser_args)
        if self._cookies:
            cookie_list = [
                {"name": name, "value": unquote(value), "domain": ".doubao.com", "path": "/"}
                for name, value in self._cookies.items()
            ]
            await self._context.add_cookies(cookie_list)

        return self._context

    @classmethod
    async def _detect_captcha_modal(cls, page: Page) -> Optional[str]:
        """检测页面是否弹出人机验证（图片验证码/滑块/hCaptcha/字节内部验证中心）。

        三维检测：
          1) 验证码 token 正则（如 2026(0728)950228FE...）
          2) 验证码弹窗特征文本
          3) 第三方/字节内部验证 iframe

        Returns:
            验证原因文本，未检测到时返回 None。
        """
        # 维度 1：token 正则（从页面文本搜，避免 iframe 内无法匹配）
        body_text = await page.evaluate("document.body.innerText")
        if cls.CAPTCHA_TOKEN_RE.search(body_text):
            return "检测到验证码 token"

        # 维度 2：特征文本
        html = await page.content()
        for pat in cls.CAPTCHA_TEXT_PATTERNS:
            if pat.casefold() in html.casefold():
                return f"检测到验证码特征文本: {pat}"

        # 维度 3：验证 iframe
        iframe = await page.query_selector(cls.CAPTCHA_IFRAME_SELECTOR)
        if iframe:
            src = await iframe.get_attribute("src") or ""
            return f"检测到验证 iframe: {src[:100]}"

        return None

    @classmethod
    async def _wait_for_captcha_resolution(cls, page: Page, reason: str) -> bool:
        """等待人工完成人机验证。

        Poll 检测验证码弹窗是否消失；超时后返回 False。
        期间每轮打印提示和剩余时间。

        Returns:
            True  — 验证已通过，可继续处理。
            False — 超时，应抛出 ReviewBlockedError 终止流水线。
        """
        remaining = cls.CAPTCHA_TIMEOUT
        print(f"\n  🔐 检测到人机验证: {reason}")
        print(f"     请在浏览器中完成验证。等待 {remaining}s 后自动停止处理...")

        while remaining > 0:
            await asyncio.sleep(cls.CAPTCHA_POLL_INTERVAL)
            remaining -= cls.CAPTCHA_POLL_INTERVAL

            captcha = await cls._detect_captcha_modal(page)
            if captcha is None:
                print(f"     ✅ 验证已通过（耗时 {cls.CAPTCHA_TIMEOUT - remaining}s），继续处理...")
                return True

            # 每 30 秒打印一次剩余时间
            if remaining % 30 == 0 and remaining > 0:
                print(f"     ⏳ 仍在等待验证... 剩余 {remaining}s")

        print(f"     ⏰ 超时 {cls.CAPTCHA_TIMEOUT}s，人机验证未完成，自动停止处理。")
        return False

    @staticmethod
    def _clean_reply(raw: str) -> str:
        """轻量清洗：剥离 prompt 回显 + UI 元信息。"""
        cleaned = raw
        for pattern in DoubaoClient.PROMPT_ECHO_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.DOTALL)
        for pattern in DoubaoClient.UI_NOISE_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"新\s*$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _extract_reply(js_result: str, prompt: str) -> str:
        """从提取的文本中截取 prompt 后的纯回复。"""
        if not js_result:
            return ""
        anchor = prompt[:50]
        idx = js_result.find(anchor)
        if idx < 0:
            return ""
        raw = js_result[idx + len(anchor):].strip()
        return DoubaoClient._clean_reply(raw)

    async def generate_outline(self, title: str, video_url: str, description: str = "") -> OutlineResult:
        """贴视频 URL 到豆包，等待流式回复完整生成后返回提纲。

        每个调用独立 Page，自动处理新对话、流式稳定检测、DOM 层噪音过滤。
        """
        prompt = self.PROMPT_TEMPLATE.format(url=video_url)

        context = await self._ensure_context()
        page = await context.new_page()

        try:
            # 1) 导航 + 新对话
            await page.goto(self.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # 检查登录（CLI 层已前置保障，此处作兜底）
            current_url = page.url
            if "/chat/" not in current_url or "login" in current_url.lower():
                raise DoubaoError("豆包未登录。请运行 python main.py，工具会自动弹窗引导登录。")

            # 检查验证码弹窗（页面加载后即可能出现）
            captcha = await self._detect_captcha_modal(page)
            if captcha:
                raise ReviewBlockedError(captcha)

            # 点击新对话
            for sel in [
                'text=新对话', '[class*="new-chat"]', '[class*="newChat"]',
                'button:has-text("新对话")', '[aria-label="新对话"]',
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn is not None:
                        await btn.click()
                        await asyncio.sleep(2)
                        break
                except Exception:
                    continue

            # 2) 发送
            await page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await page.fill(self.INPUT_SELECTOR, prompt)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")

            # 发送后再次检查验证码弹窗（发送操作本身可能触发验证）
            captcha = await self._detect_captcha_modal(page)
            if captcha:
                resolved = await self._wait_for_captcha_resolution(page, captcha)
                if not resolved:
                    raise ReviewBlockedError(f"人机验证超时（{self.CAPTCHA_TIMEOUT}s）: {captcha}")

            # 3) 等待流式回复稳定
            reply = await self._wait_for_stable_reply(page, prompt)
            await page.close()

            if reply and len(reply) > 50:
                return OutlineResult("", reply, reply, True)
            return OutlineResult("", "", reply or "", False, "豆包返回内容过短")

        except DoubaoError:
            await page.close()
            raise
        except Exception as e:
            await page.close()
            return OutlineResult("", "", str(e), False, f"豆包调用失败: {e}")

    async def _wait_for_stable_reply(self, page: Page, prompt: str) -> str:
        """流式稳定检测：连续 N 轮文本不再增长 → 完成。

        DOM 层过滤：从 message-list 容器提取文本，临时隐藏建议追问。
        """
        max_polls = self.REPLY_TIMEOUT // self.POLL_INTERVAL
        last_reply = ""
        stable_count = 0
        reply = ""

        for i in range(max_polls):
            await asyncio.sleep(self.POLL_INTERVAL)

            # 每轮轮询先检测验证码弹窗（发送后可能延迟弹出）
            captcha = await self._detect_captcha_modal(page)
            if captcha:
                resolved = await self._wait_for_captcha_resolution(page, captcha)
                if not resolved:
                    raise ReviewBlockedError(f"人机验证超时（{self.CAPTCHA_TIMEOUT}s）: {captcha}")
                # 验证已通过，继续本轮轮询（AI 回复可能已开始生成）
                continue

            # 从 message-list 容器提取（临时隐藏建议追问）
            reply = await page.evaluate("""() => {
                const msgList = document.querySelector('[class*="message-list"]');
                if (!msgList) {
                    const vList = document.querySelector('[class*="v_list"]');
                    if (vList) return vList.innerText;
                    return document.body.innerText;
                }
                const items = msgList.querySelectorAll('.suggest-list-item, .suggest-message-list-wrapper-QLdGFg');
                const originals = [];
                items.forEach(el => { originals.push(el.style.display); el.style.display = 'none'; });
                const text = msgList.innerText;
                items.forEach((el, i) => { el.style.display = originals[i]; });
                return text;
            }""")

            if not reply:
                continue

            extracted = self._extract_reply(reply, prompt)
            if not extracted:
                continue

            if extracted == last_reply:
                stable_count += 1
            else:
                stable_count = 0
                last_reply = extracted

            if stable_count >= self.REQUIRED_STABLE and len(extracted) > 50:
                # 额外确认一轮
                await asyncio.sleep(self.POLL_INTERVAL)
                final = await page.evaluate("""() => {
                    const msgList = document.querySelector('[class*="message-list"]');
                    if (!msgList) return '';
                    const items = msgList.querySelectorAll('.suggest-list-item, .suggest-message-list-wrapper-QLdGFg');
                    items.forEach(el => el.style.display = 'none');
                    const text = msgList.innerText;
                    items.forEach(el => el.style.display = '');
                    return text;
                }""")
                final_extracted = self._extract_reply(final, prompt)
                if final_extracted == extracted:
                    return extracted
                stable_count = 0
                last_reply = final_extracted

        # 超时：返回已捕获内容
        if reply:
            extracted = self._extract_reply(reply, prompt)
            return extracted or ""
        return ""

    async def close(self):
        """关闭浏览器资源。"""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
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
