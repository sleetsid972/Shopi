from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse


class ProxyManager:
    def __init__(self, proxy_file: str | Path) -> None:
        self.proxy_file = Path(proxy_file)
        self._proxies: List[str] = []
        self._index = -1
        self._last_proxy: Optional[str] = None
        self.load_from_file()

    @property
    def count(self) -> int:
        return len(self._proxies)

    def load_from_file(self) -> None:
        if not self.proxy_file.exists():
            self.proxy_file.touch()
            return

        lines = [line.strip() for line in self.proxy_file.read_text().splitlines()]
        self._proxies = []
        for line in lines:
            normalized = self._normalize_proxy(line)
            if normalized and normalized not in self._proxies:
                self._proxies.append(normalized)

    def save_to_file(self) -> None:
        self.proxy_file.write_text("\n".join(self._proxies) + ("\n" if self._proxies else ""))

    def add_proxies(self, proxies: Iterable[str]) -> int:
        added = 0
        for raw_proxy in proxies:
            normalized = self._normalize_proxy(raw_proxy)
            if normalized and normalized not in self._proxies:
                self._proxies.append(normalized)
                added += 1
        if added:
            self.save_to_file()
        return added

    def get_next_proxy(self) -> Optional[str]:
        if not self._proxies:
            return None
        if len(self._proxies) == 1:
            self._last_proxy = self._proxies[0]
            return self._proxies[0]

        for _ in range(len(self._proxies)):
            self._index = (self._index + 1) % len(self._proxies)
            candidate = self._proxies[self._index]
            if candidate != self._last_proxy:
                self._last_proxy = candidate
                return candidate

        self._last_proxy = self._proxies[self._index]
        return self._last_proxy

    @staticmethod
    def _normalize_proxy(proxy: str) -> Optional[str]:
        value = proxy.strip()
        if not value:
            return None

        if "://" not in value:
            value = f"http://{value}"

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "socks5"}:
            return None

        if not parsed.netloc:
            return None

        return value
