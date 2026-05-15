# Shopi Hybrid API v3.0

Fast Shopify checkout checker: curl_cffi primary + Selenium CAPTCHA fallback.

## Architecture

| Layer | Technology | Speed | Notes |
|---|---|---|---|
| Primary | curl_cffi + Chrome TLS fingerprint | 3-10 s/card | ~95% of cards |
| CAPTCHA fallback | Selenium / Chrome pool | 20-45 s | Activated only when needed |

### Key improvements in v3.0

1. **Cookie injection** — Selenium CAPTCHA-bypass cookies are cached per-site
   (1-hour TTL). Subsequent HTTP checks inject those cookies into the curl_cffi
   session, skipping CAPTCHA without another browser launch.
2. **Robust form filling** — four-strategy fallback per field:
   `name=` / `autocomplete=` / CSS class / placeholder text.
   Payment iframe uses `WebDriverWait(frame_to_be_available_and_switch_to_it)`.
   Shipping retries with a fresh address if validation errors appear.
3. **Proxy auth guard** — Chrome `--proxy-server` does NOT support `user:pass`.
   If the proxy has credentials Selenium is skipped and the HTTP result is returned.
4. **Quart / ASGI** — replaced Flask + `asyncio.run()` per request with Quart async
   routes served by uvicorn. All requests share one event loop per process so the
   shopifyapi semaphore works correctly.
5. **Browser pool semaphore** — `asyncio.Semaphore(BROWSER_POOL_SIZE)` ensures no
   more than N browser checks run concurrently per worker.

---

## Requirements

- Ubuntu 22.04+ (or any Linux with Chrome available)
- Python 3.10+
- Google Chrome / Chromium

## Installation

```bash
# 1. Install Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt-get install -f -y

# 2. Install Python dependencies
pip install -r requirements.txt
```

## Running

```bash
# Production (recommended)
python run.py

# Or with gunicorn
gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:5000 api:app

# Development
python api.py
```

## API

### GET /shopify

Parameters:

| Name | Required | Example |
|---|---|---|
| site | Yes | https://example.myshopify.com |
| cc | Yes | 4111111111111111|01|2026|123 |
| proxy | No | host:port or http://host:port |

Response JSON:

```json
{
  "Gateway":  "Shopify Payments",
  "Price":    19.99,
  "Response": "INSUFFICIENT_FUNDS",
  "Status":   true,
  "cc":       "4111111111111111|01|2026|123"
}
```

`Status=true` = card live. `challenge_url` included only when `Response=3DS_REQUIRED`.

### Response codes

| Code | Meaning |
|---|---|
| ORDER_PLACED | Card charged — live |
| INSUFFICIENT_FUNDS | Card approved, insufficient funds — live |
| 3DS_REQUIRED | 3D-Secure challenge (see challenge_url) — live |
| CARD_DECLINED | Card declined |
| CAPTCHA_REQUIRED | CAPTCHA not resolved (Selenium disabled or proxy auth) |
| ERROR_INVALID_CC | Bad cc= format |
| SERVER_ERROR | Internal error |

### GET /health

Returns engine status, pool info, cookie cache size.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| PORT | 5000 | Listen port |
| HOST | 0.0.0.0 | Listen address |
| WORKERS | 4 | uvicorn workers (run.py) |
| BROWSER_POOL_SIZE | 2 | Chrome instances per worker |
| BROWSER_CHECKOUT_TIMEOUT | 90 | Selenium timeout (s) |
| COOKIE_CACHE_TTL | 3600 | Per-site cookie TTL (s) |
| PREWARM_BROWSERS | 1 | Pre-warm Chrome on startup (1/0) |

## Telegram bot

No changes needed. The bot continues to call `GET /shopify` and receives the same
JSON fields as before.

## Troubleshooting

**Chrome crashes on VPS / Docker** — already handled: `--no-sandbox` and
`--disable-dev-shm-usage` are included in `_create_driver`.

**undetected_chromedriver version mismatch:**
```
pip install --upgrade undetected-chromedriver
```

**Port already in use:**
```
PORT=5001 python run.py
```
