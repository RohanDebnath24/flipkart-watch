"""
Flipkart stock watcher.
Checks one product page periodically, alerts you (Telegram) when it becomes
available, and can optionally click "Add to cart" using your saved login.
You complete payment manually.
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

# Product URL to monitor (defaults to Motorola Signature 512GB)
DEFAULT_URL = "https://www.flipkart.com/motorola-signature-pantone-carbon-512-gb/p/itm5cf2347de817a?pid=MOBHGVJYGVP6G2NY"
URL = os.getenv("FLIPKART_URL", DEFAULT_URL)
CHECK_EVERY = float(os.getenv("CHECK_INTERVAL_SECONDS", "10"))  # Base interval in seconds
HEARTBEAT_HOURS = float(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6"))  # Health status report interval in hours
REMINDER_INTERVAL_SECONDS = float(os.getenv("REMINDER_INTERVAL_SECONDS", "30"))  # Repeating alert interval when in stock
AUTO_ADD_TO_CART = os.getenv("AUTO_ADD_TO_CART", "true").lower() in ("true", "1", "yes")
PROFILE_DIR = "./flipkart_profile"   # Keeps your login session between runs

# Telegram configuration (read from environment variables with defaults)
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT = os.getenv("TG_CHAT")  # Numeric chat ID for Telegram notifications

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

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
        print("[Notice] TG_TOKEN or TG_CHAT not configured. Skipping Telegram message.", flush=True)
        return

    bot_id = TG_TOKEN.split(":")[0] if ":" in TG_TOKEN else ""
    if TG_CHAT == bot_id:
        print(f"[Warning] TG_CHAT ({TG_CHAT}) is set to the Bot ID. Telegram requires your personal User Chat ID.", flush=True)
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Telegram Error] HTTP {resp.status_code}: {resp.text}", flush=True)
    except requests.RequestException as e:
        print("[Telegram Exception]:", e, flush=True)


def check_stock_fast(url: str) -> tuple[bool, str]:
    """
    Lightning-fast HTTP stock check (takes ~0.5 seconds).
    Returns (is_available, status_message).
    """
    if not url:
        return False, "Error: No product URL specified."

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return False, f"HTTP Status {resp.status_code}"

        html = resp.text
        
        # Primary buy/cart action indicators for the target product
        has_buy_action = bool(
            re.search(r'"productActionButtonType"\s*:\s*"(?:BUY_NOW|ADD_TO_CART)"', html, re.I) or
            re.search(r'"clickAction"\s*:\s*"(?:BUY_NOW|ADD_TO_CART)"', html, re.I) or
            re.search(r'>(?:Buy\s*now|Add\s*to\s*cart)<', html, re.I) or
            re.search(r'button[^>]*>(?:[^<]*\s*)?(?:buy now|add to cart)', html, re.I)
        )

        # Check if primary product action is replaced by NOTIFY_ME (out of stock indicator)
        has_notify_me = bool(
            re.search(r'"productActionButtonType"\s*:\s*"NOTIFY_ME"', html, re.I) or
            re.search(r'>(?:Notify\s*Me)<', html, re.I)
        )

        if has_buy_action and not has_notify_me:
            return True, "AVAILABLE [IN STOCK]"
        elif has_notify_me:
            return False, "NOT AVAILABLE [OUT OF STOCK] (Notify Me active)"

        return False, "NOT AVAILABLE [OUT OF STOCK]"
    except Exception as e:
        return False, f"Check error: {e}"


def add_to_cart_playwright(url: str) -> bool:
    """Launches Playwright headless context to execute Add to Cart."""
    if not url:
        return False
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=True,
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
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            btn = page.locator("button, a, div, span").filter(has_text=re.compile(r"^(?:add to cart|buy now)$", re.I)).last
            if btn.is_visible(timeout=5000):
                try:
                    btn.click(force=True, timeout=5000)
                except Exception:
                    btn.evaluate("el => el.click()")
                page.wait_for_timeout(2000)
                print("Successfully clicked Add to Cart / Buy Now!", flush=True)
                ctx.close()
                return True
            ctx.close()
    except Exception as e:
        print(f"Add to cart error: {e}", flush=True)
    return False


def main() -> None:
    login_mode = "--login" in sys.argv
    print(f"Starting Flipkart Stock Watcher [Mode: {'Login' if login_mode else 'Watcher'}]...", flush=True)
    
    # Start health check server if PORT environment variable is set (e.g. Render Web Service)
    port_env = os.getenv("PORT")
    if port_env:
        try:
            port = int(port_env)
            t = threading.Thread(target=start_health_server, args=(port,), daemon=True)
            t.start()
        except Exception as e:
            print(f"[HealthCheck] Failed to start HTTP server: {e}", flush=True)

    if login_mode:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print("Navigating to Flipkart for login...", flush=True)
            page.goto("https://www.flipkart.com", wait_until="domcontentloaded")
            print("\n[LOGIN MODE] Please log in to Flipkart in the opened browser window.", flush=True)
            print("[LOGIN MODE] Close the browser window when you are done to save your login session.\n", flush=True)
            while True:
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
            print("Login session saved to profile directory.", flush=True)
            return

    was_available = False
    last_heartbeat = time.time()
    last_in_stock_reminder = 0.0
    print(f"Monitoring product every {CHECK_EVERY}s: {URL}", flush=True)
    print("Press Ctrl+C to stop watcher.\n", flush=True)

    # Send startup confirmation notification to Telegram
    notify("🟢 Flipkart Stock Watcher connected successfully! 24/7 Monitoring Active.")

    while True:
        try:
            available, status_details = check_stock_fast(URL)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] Status: {status_details}", flush=True)

            LATEST_STATUS.update({
                "status": "running",
                "last_check": timestamp,
                "available": available,
                "total_checks": LATEST_STATUS["total_checks"] + 1,
            })

            current_time = time.time()

            # Send periodic Telegram heartbeat status update every HEARTBEAT_HOURS (default 6 hours)
            if (current_time - last_heartbeat) >= (HEARTBEAT_HOURS * 3600):
                hb_msg = (
                    f"💚 Flipkart Watcher {HEARTBEAT_HOURS:.0f}-Hour Health Check\n"
                    f"• Status: 🟢 Monitoring Active\n"
                    f"• Checks Completed: {LATEST_STATUS['total_checks']}\n"
                    f"• Error Count: {LATEST_STATUS['error_count']}\n"
                    f"• Product: {URL}"
                )
                notify(hb_msg)
                last_heartbeat = current_time

            # Handle stock notifications & repeating reminders while in stock
            if available:
                if not was_available:
                    msg = f"🚀 IN STOCK on Flipkart!\nURL: {URL}"
                    if AUTO_ADD_TO_CART:
                        try:
                            if add_to_cart_playwright(URL):
                                msg += "\n🛒 Added to cart automatically! Complete your order now."
                            else:
                                msg += "\n⚠️ Auto add-to-cart attempt finished (manual order recommended)."
                        except Exception as cart_err:
                            print(f"[AutoCart Exception]: {cart_err}", flush=True)
                    notify(msg)
                    last_in_stock_reminder = current_time
                elif (current_time - last_in_stock_reminder) >= REMINDER_INTERVAL_SECONDS:
                    reminder_msg = (
                        f"🚨 URGENT REMINDER: Product is STILL IN STOCK on Flipkart!\n"
                        f"Have you completed your order yet?\n"
                        f"URL: {URL}"
                    )
                    notify(reminder_msg)
                    last_in_stock_reminder = current_time
            elif was_available and not available:
                notify(f"ℹ️ Product went back OUT OF STOCK on Flipkart.\nURL: {URL}")

            was_available = available

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Check error: {e}", flush=True)
            LATEST_STATUS["error_count"] += 1

        sleep_time = CHECK_EVERY + random.uniform(-1.0, 1.0)
        time.sleep(max(2.0, sleep_time))


if __name__ == "__main__":
    main()
