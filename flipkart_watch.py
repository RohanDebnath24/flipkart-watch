"""
Flipkart stock watcher.
Checks one product page periodically, alerts you (Telegram) when it becomes
available, and can optionally click "Add to cart" using your saved login.
You complete payment manually.

Setup:
  pip install playwright requests
  playwright install chromium
  export TG_TOKEN="123456:ABC..."   # from @BotFather
  export TG_CHAT="your_chat_id"     # from @userinfobot
  python flipkart_watch.py --login  # one time: log in to Flipkart, then Ctrl+C
  python flipkart_watch.py          # start watching
"""

import os
import re
import sys
import time
import random
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from playwright.sync_api import sync_playwright

# Ensure stdout/stderr handle UTF-8 safely on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Product URL to monitor
URL = os.getenv(
    "FLIPKART_URL")
CHECK_EVERY = float(os.getenv("CHECK_INTERVAL_SECONDS", "10"))  # Base interval in seconds
AUTO_ADD_TO_CART = os.getenv("AUTO_ADD_TO_CART", "true").lower() in ("true", "1", "yes")
PROFILE_DIR = "./flipkart_profile"   # Keeps your login session between runs

# Telegram configuration (read from environment variables with defaults)
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT = os.getenv("TG_CHAT")  # Numeric chat ID for Telegram notifications

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# Regex patterns for matching action buttons and stock status
BUY_OR_ADD_REGEX = re.compile(r"^\s*(buy now|add to cart)\s*$", re.I)
SOLD_OUT_REGEX = re.compile(r"sold out|currently unavailable|out of stock|coming soon|notify me", re.I)

# Health status state dictionary for HTTP health checks on Render
LATEST_STATUS = {
    "status": "initializing",
    "last_check": None,
    "available": False,
    "total_checks": 0,
    "error_count": 0,
}


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(LATEST_STATUS).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # Quiet HTTP server logs


def start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[HealthCheck] Server running on port {port}", flush=True)
    server.serve_forever()



def notify(msg: str) -> None:
    print(msg, flush=True)
    if not TG_TOKEN or not TG_CHAT:
        print("[Notice] TG_TOKEN or TG_CHAT not configured. Skipping Telegram message.")
        return

    # Check if TG_CHAT was accidentally set to the Bot ID prefix
    bot_id = TG_TOKEN.split(":")[0] if ":" in TG_TOKEN else ""
    if TG_CHAT == bot_id:
        print(f"[Warning] TG_CHAT ({TG_CHAT}) is set to the Bot ID. Telegram requires your personal User Chat ID (get it from @userinfobot).")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Telegram Error] HTTP {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print("[Telegram Exception]:", e)


def dismiss_popups(page) -> None:
    """Closes any login prompt or banner popups on Flipkart if present."""
    try:
        close_selectors = [
            "button._2KpZ6l._2doB4z",
            "button._2doB4z",
            "span._30Jegg",
            "button:has-text('✕')",
        ]
        for sel in close_selectors:
            for btn in page.locator(sel).all():
                if btn.is_visible():
                    btn.click(timeout=1000)
                    page.wait_for_timeout(300)
    except Exception:
        pass


def is_available(page) -> bool:
    """
    Determines if the current product variant on page is available to buy.
    Avoids false positives from unselected variant stock messages.
    """
    dismiss_popups(page)

    # 1. Look for visible "BUY NOW" or "ADD TO CART" buttons/elements
    buy_locators = page.locator("button, a, div, li").filter(has_text=BUY_OR_ADD_REGEX).all()
    visible_buy_buttons = [el for el in buy_locators if el.is_visible()]
    if len(visible_buy_buttons) > 0:
        return True

    # Generic check for visible text matching buy or cart
    generic_buy = [el for el in page.locator("text=/buy now|add to cart/i").all() if el.is_visible()]
    if len(generic_buy) > 0:
        return True

    # 2. Check for explicit Out of Stock indicators in visible main section
    oos_locators = [el for el in page.locator("div, span, button").filter(has_text=SOLD_OUT_REGEX).all() if el.is_visible()]
    if len(oos_locators) > 0:
        return False

    return False


def add_to_cart(page) -> bool:
    """Attempts to click the Add to Cart button."""
    dismiss_popups(page)
    try:
        # Search for explicit "ADD TO CART" button
        btn = page.locator("button, a, div, li").filter(has_text=re.compile(r"add to cart", re.I)).first
        if not btn.is_visible():
            btn = page.locator("text=/add to cart/i").first

        if btn.is_visible():
            btn.click(timeout=5000)
            page.wait_for_timeout(2000)
            print("Successfully clicked Add to Cart!")
            return True
        else:
            print("Add to Cart button was not visible.")
            return False
    except Exception as e:
        print("Add to cart error:", e)
        return False


def main() -> None:
    login_mode = "--login" in sys.argv
    print(f"Starting Flipkart Stock Watcher [Mode: {'Login' if login_mode else 'Watcher'}]...")
    
    # Start health check server if PORT environment variable is set (e.g. Render Web Service)
    port_env = os.getenv("PORT")
    if port_env:
        try:
            port = int(port_env)
            t = threading.Thread(target=start_health_server, args=(port,), daemon=True)
            t.start()
        except Exception as e:
            print(f"[HealthCheck] Failed to start HTTP server: {e}")

    os.makedirs(PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:
        # Launch Chromium persistent context with stealth settings to bypass bot detection
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=not login_mode,
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if login_mode:
            print("Navigating to Flipkart for login...")
            page.goto("https://www.flipkart.com", wait_until="domcontentloaded")
            input("\n-> Log in to Flipkart in the opened browser window, then press Enter here to save session...")
            ctx.close()
            print("Login session saved to profile directory.")
            return

        was_available = False
        print(f"Monitoring product every {CHECK_EVERY}s: {URL}")
        print("Press Ctrl+C to stop watcher.\n")

        while True:
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)

                available = is_available(page)
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                status_str = "AVAILABLE [IN STOCK]" if available else "NOT AVAILABLE [OUT OF STOCK]"
                print(f"[{timestamp}] Status: {status_str}", flush=True)

                LATEST_STATUS.update({
                    "status": "running",
                    "last_check": timestamp,
                    "available": available,
                    "total_checks": LATEST_STATUS["total_checks"] + 1,
                })

                if available and not was_available:
                    msg = f"🚀 IN STOCK on Flipkart!\nURL: {URL}"
                    if AUTO_ADD_TO_CART and add_to_cart(page):
                        msg += "\n🛒 Added to cart automatically! Complete your order now."
                    notify(msg)

                was_available = available

            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] Check error: {e}", flush=True)
                LATEST_STATUS["error_count"] += 1

            sleep_time = CHECK_EVERY + random.uniform(-1.5, 1.5)
            time.sleep(max(2.0, sleep_time))



if __name__ == "__main__":
    main()
