"""
Standalone Flipkart login helper script.
Opens Flipkart in a Chromium browser window so you can log in.
Saves session cookies and profile data into ./flipkart_profile/ automatically.
"""

import os
import time
from playwright.sync_api import sync_playwright

PROFILE_DIR = "./flipkart_profile"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def login():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print("\n=======================================================", flush=True)
    print(" Opening Flipkart Login Window...", flush=True)
    print(" Please log in to Flipkart in the browser window that pops up.", flush=True)
    print(" Once logged in, close the browser window to finish.", flush=True)
    print("=======================================================\n", flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.flipkart.com", wait_until="domcontentloaded")
        
        print("Waiting for browser to be closed or login to complete...", flush=True)
        
        # Wait up to 5 minutes or until browser page is closed
        start_time = time.time()
        while time.time() - start_time < 300:
            try:
                if page.is_closed():
                    break
            except Exception:
                break
            time.sleep(1)

        try:
            ctx.close()
        except Exception:
            pass

    print("\n🟢 Flipkart session successfully saved to ./flipkart_profile/", flush=True)

if __name__ == "__main__":
    login()
