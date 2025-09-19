from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=200)
    page = browser.new_page()
    page.goto("https://example.com", timeout=30000)
    print("Title:", page.title())
    input("Chromium is open. Press ENTER here to close it...")
    browser.close()
