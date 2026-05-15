#!/usr/bin/env python3
"""
run.py — Production launcher for Shopi Hybrid API v3.0

Usage
=====
  # Recommended: uvicorn with multiple workers
  python run.py

  # Or directly:
  uvicorn api:app --host 0.0.0.0 --port 5000 --workers 4

  # Or with gunicorn (uvicorn worker class):
  gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:5000 api:app

Environment variables
=====================
  PORT                 Listen port (default 5000)
  HOST                 Listen host (default 0.0.0.0)
  WORKERS              Number of uvicorn workers (default 4)
  BROWSER_POOL_SIZE    Chrome instances per worker (default 2)
  BROWSER_CHECKOUT_TIMEOUT  Selenium timeout in seconds (default 90)
  COOKIE_CACHE_TTL     Per-site cookie cache TTL in seconds (default 3600)
  PREWARM_BROWSERS     Pre-warm browser pool on startup: 1/0 (default 1)
"""
import os
import sys

HOST    = os.environ.get("HOST",    "0.0.0.0")
PORT    = int(os.environ.get("PORT", "5000"))
WORKERS = int(os.environ.get("WORKERS", "4"))

if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn[standard]")
        sys.exit(1)

    print("Starting Shopi Hybrid API v3.0")
    print("  Host    :", HOST)
    print("  Port    :", PORT)
    print("  Workers :", WORKERS)
    print("  Quart   : async event-loop per worker")
    print()

    # Single-process mode keeps one browser pool and one event loop.
    # Multi-process (workers > 1) gives each process its own pool.
    if WORKERS == 1:
        uvicorn.run("api:app", host=HOST, port=PORT, log_level="info")
    else:
        uvicorn.run(
            "api:app",
            host=HOST,
            port=PORT,
            workers=WORKERS,
            log_level="info",
        )
