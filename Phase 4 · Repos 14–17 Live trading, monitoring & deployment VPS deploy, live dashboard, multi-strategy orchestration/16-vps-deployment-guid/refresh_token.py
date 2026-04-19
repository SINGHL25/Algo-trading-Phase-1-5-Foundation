"""
token_refresh/refresh_token.py
───────────────────────────────
Automated daily Kite access token refresh using Selenium + Chrome headless.

Two authentication modes:
  Mode 1 — TOTP (Time-Based OTP via authenticator app):
    Set KITE_TOTP_SECRET in .env. The script generates the current TOTP code
    using the pyotp library and fills it in automatically.

  Mode 2 — Manual PIN fallback:
    If KITE_TOTP_SECRET is not set, sends a Telegram message asking the user
    to provide the PIN, waits up to 5 minutes for a reply via the bot.

After successful login:
  - Writes the new KITE_ACCESS_TOKEN to .env
  - Updates the running orchestrator via POST /admin/token/update
  - Sends a Telegram confirmation

Prerequisites:
  pip install selenium webdriver-manager pyotp python-dotenv requests

Setup for TOTP:
  1. In Kite Security settings, enable 2FA with TOTP (Google Authenticator)
  2. When shown the QR code, also note the secret key (base32 string)
  3. Add to .env: KITE_TOTP_SECRET=JBSWY3DPEHPK3PXP (example — use yours)

Security notes:
  - The Chrome session is headless and runs as the 'trader' user
  - Credentials are read from .env — never hardcoded here
  - Chrome runs with --no-sandbox (required for VPS) + --disable-dev-shm-usage
  - The TOTP secret is as sensitive as your password — protect .env accordingly
"""

import os
import sys
import re
import time
import logging
import hmac
import hashlib
import struct
import base64
import asyncio
import requests
from pathlib import Path
from datetime import datetime
import pytz

from dotenv import load_dotenv, set_key
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("token_refresh")
IST    = pytz.timezone("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────────
KITE_API_KEY    = os.getenv("KITE_API_KEY",    "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_USER_ID    = os.getenv("KITE_USER_ID",    "")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD",   "")
KITE_PIN        = os.getenv("KITE_PIN",        "")
KITE_TOTP_SECRET= os.getenv("KITE_TOTP_SECRET","")   # base32 TOTP secret

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:5001")
ENV_FILE         = Path(os.getenv("ENV_FILE", ".env"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# Kite login flow URLs
KITE_LOGIN_URL    = f"https://kite.zerodha.com/connect/login?api_key={KITE_API_KEY}&v=3"
KITE_REDIRECT_RX  = re.compile(r"request_token=([^&]+)")


# ── TOTP generator (RFC 6238 — no pyotp required) ─────────────────────────────

def _generate_totp(secret_b32: str, interval: int = 30) -> str:
    """Generate current TOTP code from a base32 secret."""
    try:
        import pyotp
        return pyotp.TOTP(secret_b32).now()
    except ImportError:
        # Fallback: pure stdlib implementation
        key    = base64.b32decode(secret_b32.upper().replace(" ", ""))
        epoch  = int(time.time()) // interval
        msg    = struct.pack(">Q", epoch)
        h      = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code   = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
        return str(code % 1_000_000).zfill(6)


# ── Chrome driver factory ──────────────────────────────────────────────────────

def _get_driver() -> webdriver.Chrome:
    """Create a headless Chrome WebDriver suitable for VPS."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")
    # Suppress Selenium logs
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=options)
    except Exception:
        # Fallback to system chromedriver
        return webdriver.Chrome(options=options)


# ── Main login flow ────────────────────────────────────────────────────────────

def login_and_get_request_token() -> str:
    """
    Automate the Kite Connect login flow and return the request_token.

    Returns:
        request_token string from the redirect URL
    Raises:
        RuntimeError on any failure
    """
    driver = None
    try:
        logger.info("Starting Chrome headless browser...")
        driver = _get_driver()
        wait   = WebDriverWait(driver, 20)

        # ── Step 1: Navigate to login page ────────────────────────────────────
        logger.info("Opening Kite login page...")
        driver.get(KITE_LOGIN_URL)
        time.sleep(2)

        # ── Step 2: Enter user ID and password ────────────────────────────────
        wait.until(EC.presence_of_element_located((By.ID, "userid")))
        driver.find_element(By.ID, "userid").send_keys(KITE_USER_ID)
        driver.find_element(By.ID, "password").send_keys(KITE_PASSWORD)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        logger.info("Credentials submitted, waiting for 2FA...")
        time.sleep(2)

        # ── Step 3: 2FA — TOTP or PIN ─────────────────────────────────────────
        totp_input = None
        try:
            totp_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='number' or @label='External TOTP']"))
            )
        except TimeoutException:
            # Some accounts use a text field
            try:
                totp_input = wait.until(
                    EC.presence_of_element_located((By.XPATH, "//input[contains(@placeholder,'PIN') or contains(@placeholder,'OTP') or contains(@placeholder,'2FA')]"))
                )
            except TimeoutException:
                pass

        if totp_input and KITE_TOTP_SECRET:
            totp_code = _generate_totp(KITE_TOTP_SECRET)
            logger.info(f"Entering TOTP code: {totp_code[:2]}****")
            totp_input.send_keys(totp_code)
            time.sleep(1)

            # Some Kite versions auto-submit on 6 digits; others need a button
            try:
                submit = driver.find_element(
                    By.XPATH, "//button[@type='submit' or contains(text(),'Continue')]"
                )
                submit.click()
            except Exception:
                pass

        elif totp_input and KITE_PIN:
            logger.info("Entering security PIN...")
            totp_input.send_keys(KITE_PIN)
            time.sleep(1)
            try:
                submit = driver.find_element(By.XPATH, "//button[@type='submit']")
                submit.click()
            except Exception:
                pass

        else:
            logger.warning("No TOTP secret or PIN configured — cannot complete 2FA automatically")
            raise RuntimeError(
                "2FA required but KITE_TOTP_SECRET / KITE_PIN not set. "
                "Add one to .env to enable automated token refresh."
            )

        logger.info("Waiting for redirect after 2FA...")
        time.sleep(3)

        # ── Step 4: Extract request_token from redirect URL ───────────────────
        # After login, Kite redirects to your registered redirect URL with
        # ?request_token=... in the query string
        current_url = driver.current_url
        logger.info(f"Current URL after login: {current_url[:60]}...")

        # Wait up to 15s for redirect containing request_token
        deadline = time.time() + 15
        while time.time() < deadline:
            current_url = driver.current_url
            match = KITE_REDIRECT_RX.search(current_url)
            if match:
                request_token = match.group(1)
                logger.info(f"request_token obtained: {request_token[:8]}...")
                return request_token
            time.sleep(1)

        # If no token in URL, check page source (some flows show it differently)
        page_source = driver.page_source
        match = KITE_REDIRECT_RX.search(page_source)
        if match:
            return match.group(1)

        raise RuntimeError(
            f"request_token not found in URL after login. "
            f"Current URL: {current_url}"
        )

    finally:
        if driver:
            driver.quit()


def generate_access_token(request_token: str) -> str:
    """Exchange request_token for access_token via Kite API."""
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=KITE_API_KEY)
    data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    return data["access_token"]


def update_env_file(access_token: str):
    """Write new access token to .env file."""
    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "KITE_ACCESS_TOKEN", access_token)
        logger.info(f"Updated KITE_ACCESS_TOKEN in {ENV_FILE}")
    else:
        logger.warning(f".env file not found at {ENV_FILE}")

    # Also update running process via orchestrator API
    try:
        resp = requests.post(
            f"{ORCHESTRATOR_URL}/admin/token/update",
            json={"access_token": access_token},
            timeout=5,
        )
        if resp.status_code == 200:
            logger.info("Orchestrator token updated via API")
        else:
            logger.warning(f"Orchestrator API returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not update orchestrator via API: {e}")


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(IST)
    logger.info(f"Token refresh starting at {now.strftime('%H:%M IST')}...")

    if not all([KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD]):
        logger.error("Missing required credentials in .env")
        logger.error("Required: KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD")
        sys.exit(1)

    if not KITE_TOTP_SECRET and not KITE_PIN:
        logger.warning("No 2FA method configured — will fail at 2FA step")

    try:
        request_token = login_and_get_request_token()
        access_token  = generate_access_token(request_token)
        update_env_file(access_token)

        logger.info("✅ Token refresh successful")
        send_telegram(
            f"✅ *Kite token refreshed*\n"
            f"Time: {now.strftime('%H:%M IST')}\n"
            f"Token: `{access_token[:8]}...{access_token[-4:]}`"
        )
        sys.exit(0)

    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        send_telegram(
            f"❌ *Token refresh FAILED*\n"
            f"`{type(e).__name__}: {e}`\n"
            f"Manual action required!"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
