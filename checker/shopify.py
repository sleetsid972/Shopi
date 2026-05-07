import asyncio
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from .proxy_manager import ProxyManager


@dataclass
class ShopifyCheckResult:
    input_url: str
    normalized_url: str
    live: bool
    shopify_confirmed: bool
    has_products: bool
    product_count: int
    store_name: str
    currency: str
    status: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize_url(url: str) -> str:
    candidate = url.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.netloc:
        raise ValueError("Invalid URL")

    return f"{parsed.scheme}://{parsed.netloc}"


async def _http_get(url: str, proxy: Optional[str], timeout: int) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Safari/537.36",
        "Accept": "*/*",
    }

    if proxy and proxy.startswith("socks5://"):
        connector = ProxyConnector.from_url(proxy)
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            response = await asyncio.wait_for(
                session.get(url, allow_redirects=True), timeout=timeout
            )
            async with response:
                text = await asyncio.wait_for(response.text(errors="ignore"), timeout=timeout)
                return {
                    "status": response.status,
                    "url": str(response.url),
                    "headers": dict(response.headers),
                    "text": text,
                }

    async with aiohttp.ClientSession(headers=headers) as session:
        request_kwargs: Dict[str, Any] = {"allow_redirects": True}
        if proxy:
            request_kwargs["proxy"] = proxy

        response = await asyncio.wait_for(session.get(url, **request_kwargs), timeout=timeout)
        async with response:
            text = await asyncio.wait_for(response.text(errors="ignore"), timeout=timeout)
            return {
                "status": response.status,
                "url": str(response.url),
                "headers": dict(response.headers),
                "text": text,
            }


async def _fetch_with_rotating_proxy(
    url: str, proxy_manager: ProxyManager, timeout: int
) -> Dict[str, Any]:
    attempts = max(proxy_manager.count, 1)
    last_error: Optional[Exception] = None

    for _ in range(attempts):
        proxy = proxy_manager.get_next_proxy()
        try:
            response = await _http_get(url, proxy, timeout)
            response["proxy"] = proxy
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise RuntimeError("Request failed")


def _extract_title(html: str) -> str:
    lower = html.lower()
    start = lower.find("<title>")
    end = lower.find("</title>")
    if start == -1 or end == -1 or end <= start + 7:
        return ""
    return html[start + 7 : end].strip()


async def check_shopify_store(
    url: str, proxy_manager: ProxyManager, timeout: int = 20
) -> Dict[str, Any]:
    normalized_url = _normalize_url(url)
    home_url = normalized_url
    products_url = urljoin(normalized_url, "/products.json?limit=1")
    meta_url = urljoin(normalized_url, "/meta.json")

    result = ShopifyCheckResult(
        input_url=url,
        normalized_url=normalized_url,
        live=False,
        shopify_confirmed=False,
        has_products=False,
        product_count=0,
        store_name="Unknown",
        currency="Unknown",
        status="Dead",
        reason="Store did not respond",
    )

    try:
        home_response = await _fetch_with_rotating_proxy(home_url, proxy_manager, timeout)
    except Exception as exc:  # noqa: BLE001
        result.reason = f"Request failed: {exc}"
        return result.to_dict()

    home_html = home_response["text"]
    home_lower = home_html.lower()
    title = _extract_title(home_html)
    current_url = home_response["url"].lower()

    password_keywords = ["enter using password", "password", "storefront password"]
    unavailable_keywords = [
        "shop is unavailable",
        "store unavailable",
        "currently unavailable",
        "temporarily unavailable",
    ]

    if home_response["status"] == 200:
        password_protected = "/password" in current_url or any(
            keyword in home_lower for keyword in password_keywords
        )
        unavailable = any(keyword in home_lower for keyword in unavailable_keywords)
        if not password_protected and not unavailable:
            result.live = True
            result.reason = "Store is live"
        elif password_protected:
            result.reason = "Store is password protected"
        else:
            result.reason = "Store appears unavailable"
    else:
        result.reason = f"Home page status: {home_response['status']}"

    header_keys = {key.lower() for key in home_response["headers"].keys()}
    shopify_signals = [
        "cdn.shopify.com" in home_lower,
        "shopify.shop" in home_lower,
        "shopify-payment-button" in home_lower,
        any("x-shopify" in header for header in header_keys),
        "shopify" in title.lower(),
    ]

    products_data: Dict[str, Any] = {}
    try:
        products_response = await _fetch_with_rotating_proxy(products_url, proxy_manager, timeout)
        products_data = json.loads(products_response["text"])
        products = products_data.get("products", []) if isinstance(products_data, dict) else []
        result.product_count = len(products)
        if result.product_count > 0:
            result.has_products = True
        if isinstance(products_data, dict) and "products" in products_data:
            shopify_signals.append(True)
    except Exception:  # noqa: BLE001
        products_data = {}

    try:
        meta_response = await _fetch_with_rotating_proxy(meta_url, proxy_manager, timeout)
        meta_json = json.loads(meta_response["text"])
        if isinstance(meta_json, dict):
            result.store_name = str(meta_json.get("name") or result.store_name)
            result.currency = str(meta_json.get("currency") or result.currency)
    except Exception:  # noqa: BLE001
        pass

    if result.store_name == "Unknown" and title:
        result.store_name = title

    result.shopify_confirmed = any(shopify_signals)

    if result.live and result.shopify_confirmed and result.has_products:
        result.status = "Valid Store"
        result.reason = "Live Shopify store with products"
    elif not result.shopify_confirmed:
        result.status = "Not Shopify"
        if result.reason == "Store is live":
            result.reason = "Shopify signatures not found"
    else:
        result.status = "Dead"

    return result.to_dict()
