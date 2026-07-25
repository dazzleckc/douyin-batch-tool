"""自动登录模块：检测登录态，缺失时弹窗让用户登录。

用法：
    from src.login import ensure_douyin_login
    await ensure_douyin_login(config)  # 必要时弹出浏览器
"""

import asyncio
import json
import sys
from pathlib import Path

from src.config import Config


async def ensure_douyin_login(config: Config) -> bool:
    """确保抖音登录态存在。缺失时自动弹出浏览器让用户登录。

    Args:
        config: 应用运行时配置。

    Returns:
        bool: True 表示登录态已就绪（原来就有，或用户刚登录成功）。
    """
    state_path = Path(config.douyin_state_path)

    # 已有有效的登录态文件，直接返回
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            if state.get("cookies"):
                return True
        except Exception:
            pass  # 文件损坏，重新登录

    # 需要登录
    print()
    print("=" * 60)
    print("  抖音未登录 —— 即将弹出浏览器窗口")
    print("=" * 60)
    print()
    print("请在浏览器窗口中完成抖音登录（扫码或手机号）。")
    print("登录成功后回到终端按 Enter 继续。")
    print()

    input(">>> 按 Enter 打开浏览器...")

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        try:
            await page.goto(
                "https://www.douyin.com/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            input("\n>>> 登录完成后按 Enter 保存登录态...")

            # 快速验证
            await asyncio.sleep(2)
            try:
                body = await page.evaluate("() => document.body.innerText")
                if "登录" in body[:800] and "我的" not in body[:800]:
                    print("\n⚠️  页面可能仍显示未登录状态！")
                    cont = input("确实要保存吗？[y/N] ")
                    if cont.lower() != "y":
                        return False
            except Exception:
                pass

            # 保存
            state = await page.context.storage_state()
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

            cookie_count = len(state.get("cookies", []))
            print(f"\n✅ 登录态已保存 ({cookie_count} 个 cookies)")
            return True

        finally:
            await browser.close()

    finally:
        await pw.stop()


async def ensure_doubao_login(config: Config) -> bool:
    """确保豆包登录态存在。缺失时弹窗让用户登录。"""
    state_path = Path(config.doubao_state_path)

    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            if state.get("cookies"):
                return True
        except Exception:
            pass

    print()
    print("=" * 60)
    print("  豆包未登录 —— 即将弹出浏览器窗口")
    print("=" * 60)
    print("请在浏览器中完成豆包登录（扫码或手机号），完成后回终端按 Enter。")

    input(">>> 按 Enter 打开浏览器...")

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        try:
            await page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
            input("\n>>> 登录完成后按 Enter 保存登录态...")
            await asyncio.sleep(2)

            body = await page.evaluate("() => document.body.innerText")
            if "登录" in body[:800] and "有什么我能帮你的吗" not in body[:800]:
                print("\n⚠️  页面可能仍显示未登录状态！")
                if input("确实要保存吗？[y/N] ").lower() != "y":
                    return False

            state = await page.context.storage_state()
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            print(f"\n✅ 豆包登录态已保存 ({len(state.get('cookies', []))} cookies)")
            return True
        finally:
            await browser.close()
    finally:
        await pw.stop()
