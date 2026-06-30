"""
Local image cache.

Some venues (Lessin, possibly others) block hotlinking — the image URL is
valid but only serves bytes when the Referer matches their own domain. The
browser can't fake that. The fix is to download images at scrape time, with
a same-origin Referer, and serve them locally from `output/images/`.

Usage:
  from .image_cache import LocalImageCache
  cache = LocalImageCache(output_dir="output/images", db_path="data/shows.db")
  local_path = cache.fetch(remote_url)   # returns "images/<hash>.jpg" or "" on failure
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx


log = logging.getLogger("image_cache")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CACHE_DDL = """
CREATE TABLE IF NOT EXISTS image_cache (
    remote_url TEXT PRIMARY KEY,
    local_filename TEXT,
    fetched_on TEXT NOT NULL,
    status TEXT NOT NULL,        -- 'ok' / 'failed'
    content_type TEXT
);
"""


class LocalImageCache:
    def __init__(
        self,
        output_dir: str | Path = "output/images",
        db_path: str | Path = "data/shows.db",
        timeout: float = 15.0,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(CACHE_DDL)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def fetch(self, remote_url: str) -> str:
        """Return relative local path (e.g. 'images/abc.jpg') or ''."""
        if not remote_url:
            return ""

        cur = self.conn.execute(
            "SELECT local_filename, status FROM image_cache WHERE remote_url = ?",
            (remote_url,),
        )
        row = cur.fetchone()
        if row:
            if row["status"] != "ok":
                return ""
            local = self.output_dir / row["local_filename"]
            if local.exists():
                return f"images/{row['local_filename']}"
            # Cache row exists but file is gone — fall through to re-download

        filename = self._filename_for(remote_url)
        local = self.output_dir / filename

        # Same-origin Referer is the trick that beats hotlink blockers (Lessin etc.)
        parts = urlsplit(remote_url)
        referer = f"{parts.scheme}://{parts.netloc}/"
        try:
            r = self.client.get(remote_url, headers={"Referer": referer})
            if r.status_code != 200:
                self._save(remote_url, "", "failed", r.headers.get("content-type", ""))
                log.info("image %d for %s", r.status_code, remote_url[:80])
                return ""
            ctype = r.headers.get("content-type", "").lower()
            if "image" not in ctype:
                self._save(remote_url, "", "failed", ctype)
                log.info("not-image for %s (%s)", remote_url[:80], ctype)
                return ""
            local.write_bytes(r.content)
            self._save(remote_url, filename, "ok", ctype)
            return f"images/{filename}"
        except Exception as e:
            log.warning("image fetch failed for %s: %s", remote_url[:80], e)
            self._save(remote_url, "", "failed", "")
            return ""

    def _save(self, remote_url: str, filename: str, status: str, ctype: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO image_cache (remote_url, local_filename, fetched_on, status, content_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (remote_url, filename, date.today().isoformat(), status, ctype),
        )
        self.conn.commit()

    @staticmethod
    def _filename_for(remote_url: str) -> str:
        h = hashlib.sha1(remote_url.encode("utf-8")).hexdigest()[:16]
        # Try to preserve a sensible extension
        path = urlsplit(remote_url).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            if path.endswith(ext):
                return f"{h}{ext}"
        return f"{h}.jpg"  # default
