# Shopi

Async Shopify checker Telegram bot with whitelist-based access control, rotating proxy support, single and mass store checks.

## Features

- Access control with SQLite whitelist
- Admin controls: `/adduser`, `/removeuser`, `/addproxy`, `/stats`
- Rotating proxy support (HTTP/HTTPS/SOCKS5) for every HTTP request
- Single check: `/check <url>`
- Mass check from `.txt` file: `/masscheck`
- Shopify detection with:
  - Live store check
  - Shopify signature detection
  - Product availability via `/products.json?limit=1`
  - Store name and currency extraction

## Project Structure

```text
bot.py
config.py
checker/
  __init__.py
  shopify.py
  proxy_manager.py
database/
  __init__.py
  db.py
proxies.txt
requirements.txt
README.md
```

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set config through environment (recommended):

```bash
export BOT_TOKEN="your_telegram_bot_token"
export ADMIN_ID="123456789"
```

Or edit `config.py` defaults.

3. Run bot:

```bash
python bot.py
```

## Usage

- `/start` - bot info
- `/check <url>` - check one store
- `/masscheck` - upload `.txt` file containing one URL per line
- `/stats` - bot stats

Admin only:

- `/adduser <user_id>`
- `/removeuser <user_id>`
- `/addproxy` (or `/addproxy <proxy1> <proxy2> ...`)

## Proxy Formats

Supported proxy formats:

- `http://user:pass@host:port`
- `https://host:port`
- `socks5://user:pass@host:port`

If scheme is omitted, `http://` is assumed.
