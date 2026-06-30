from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Iterable

import httpx

from ..models import Show


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class Scraper(ABC):
    """Base class for site scrapers."""

    source_id: str = ""        # short id, e.g. "habima"
    source_name: str = ""      # display name, e.g. "תיאטרון הבימה"

    def __init__(self, timeout: float = 30.0):
        self.log = logging.getLogger(f"scraper.{self.source_id}")
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"},
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @abstractmethod
    def fetch_shows(self) -> Iterable[Show]:
        """Yield Show objects. Implement per site."""
        ...

    # -- helpers ---------------------------------------------------------
    def get(self, url: str) -> httpx.Response:
        r = self.client.get(url)
        r.raise_for_status()
        return r
