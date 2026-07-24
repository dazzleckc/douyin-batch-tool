"""登录一次 doubao.com，保存登录态供后续自动使用"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    # 有窗口模式，让你手动登录
    browser = await pw.chromium.launch(headless=False)
    page = await browser.new_page()
    
    print("1. 浏览器已打开，请在窗口中登录 doubao.com")
    print("   可以扫码或手机号登录")
    print("   登录成功后，回到终端按 Enter 继续...")
    
    await page.goto("https://www.doubao.com/chat/")
    input()  # 等待用户按 Enter
    
    # 保存登录态
    state = await page.context.storage_state()
    import json
    with open("doubao_state.json", "w") as f:
        json.dump(state, f)
    print("2. 登录态已保存到 doubao_state.json")
    print("   之后运行工具会自动使用这份登录态")
    
    await browser.close()
    await pw.stop()

asyncio.run(main())
