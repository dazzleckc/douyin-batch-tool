"""调试豆包 - 测试不同消息格式"""
import asyncio, os
from dotenv import load_dotenv
load_dotenv()

async def main():
    from playwright.async_api import async_playwright
    
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    
    raw = os.getenv("DOUBAO_COOKIE", "")
    if raw:
        from urllib.parse import unquote
        cookies = []
        for part in raw.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                k = k.strip().lower()
                if k not in ("domain", "path", "expires", "max-age", "secure", "httponly", "samesite"):
                    cookies.append({"name": k, "value": unquote(v), "domain": ".doubao.com", "path": "/"})
        await context.add_cookies(cookies)
    
    page = await context.new_page()
    await page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)
    
    input_sel = 'textarea[placeholder="发消息..."]'
    
    # 测试 1: 只发送 URL
    print("\n=== 测试 1: 只发送 URL ===")
    await page.wait_for_selector(input_sel, timeout=10000)
    url_only = "https://www.douyin.com/video/7609521874258726153"
    await page.fill(input_sel, url_only)
    await page.keyboard.press("Enter")
    await asyncio.sleep(15)
    await page.screenshot(path="debug_test1.png")
    print(f"  body 长度: {(await page.evaluate('() => document.body.innerText.length'))}")
    
    # 清空并测试 2: 简单提示
    print("\n=== 测试 2: '分析视频' + URL ===")
    await page.fill(input_sel, "")
    await asyncio.sleep(1)
    await page.fill(input_sel, f"分析这个抖音视频\n{url_only}")
    await page.keyboard.press("Enter")
    await asyncio.sleep(15)
    await page.screenshot(path="debug_test2.png")
    print(f"  body 长度: {(await page.evaluate('() => document.body.innerText.length'))}")
    
    # 清空并测试 3: 看 AI 是否有"视频分析"功能按钮
    print("\n=== 测试 3: 检查功能按钮 ===")
    await page.fill(input_sel, "")
    await asyncio.sleep(2)
    btns = await page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button, [role="button"], [class*="btn"]'));
        return btns.map(b => b.innerText || b.title || b.getAttribute('aria-label') || '').filter(t => t);
    }""")
    print(f"  可见按钮: {btns}")
    
    await browser.close()
    await pw.stop()

asyncio.run(main())