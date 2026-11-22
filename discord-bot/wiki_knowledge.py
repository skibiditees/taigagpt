"""Text-only INSMP Miraheze wiki collector and searcher."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

API_URL = "https://insmp.miraheze.org/w/api.php"
USER_AGENT = "TaigaGPT/1.0 (Discord bot; wiki text indexing)"
MAX_SYNC_PAGES = int(os.getenv("TAIGA_WIKI_MAX_PAGES", "5000"))


def _api_request(params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({"format": "json", "formatversion": 2, **params})
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def _plain_text(value: str) -> str:
    # The MediaWiki extracts API returns plaintext; normalize whitespace only.
    value = re.sub(r"\r", "", value or "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


class WikiKnowledge:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS wiki_text (
                page_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                fetched REAL NOT NULL
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_wiki_title ON wiki_text(title)")
        self.db.commit()

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(_api_request, params)

    def _save_pages(self, pages: list[dict[str, Any]]) -> None:
        rows = []
        for page in pages:
            if page.get("missing") or page.get("ns", 0) != 0:
                continue
            title = str(page.get("title", "")).strip()
            content = _plain_text(page.get("extract", ""))
            page_id = page.get("pageid")
            if title and content and isinstance(page_id, int):
                rows.append((page_id, title, content, time.time()))
        if rows:
            self.db.executemany("""
                INSERT INTO wiki_text(page_id, title, content, fetched)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    title=excluded.title, content=excluded.content, fetched=excluded.fetched
            """, rows)
            self.db.commit()

    async def _fetch_titles(self, titles: list[str]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for start in range(0, len(titles), 20):
            batch = titles[start:start + 20]
            if not batch:
                continue
            data = await self._request({
                "action": "query",
                "prop": "extracts|links",
                "titles": "|".join(batch),
                "explaintext": 1,
                "exsectionformat": "plain",
                "plnamespace": 0,
                "pllimit": 50,
            })
            result = data.get("query", {}).get("pages", [])
            if isinstance(result, dict):
                result = list(result.values())
            pages.extend(result)
            await asyncio.sleep(0.15)
        self._save_pages(pages)
        return pages

    async def sync_all(self) -> None:
        """Index up to the configured number of main-namespace pages, politely."""
        try:
            titles: list[str] = []
            continuation = None
            while len(titles) < MAX_SYNC_PAGES:
                params: dict[str, Any] = {
                    "action": "query", "list": "allpages",
                    "apnamespace": 0, "aplimit": 50,
                }
                if continuation:
                    params.update(continuation)
                data = await self._request(params)
                batch = data.get("query", {}).get("allpages", [])
                titles.extend(item["title"] for item in batch if item.get("title"))
                continuation = data.get("continue")
                if not continuation or not batch:
                    break
                await asyncio.sleep(0.2)

            titles = titles[:MAX_SYNC_PAGES]
            print(f"[TaigaGPT] INSMP wiki: indexing {len(titles)} pages as text.")
            for start in range(0, len(titles), 20):
                await self._fetch_titles(titles[start:start + 20])
                await asyncio.sleep(0.2)
            print("[TaigaGPT] INSMP wiki text indexing finished.")
        except Exception as exc:
            print(f"[TaigaGPT] INSMP wiki sync failed: {type(exc).__name__}: {exc}")

    def _local_search(self, query: str, limit: int = 6) -> list[tuple[str, str]]:
        words = re.findall(r"[\w-]{3,}", query.lower())[:8]
        if not words:
            return []
        clauses = []
        values: list[str] = []
        for word in words:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(content) LIKE ?)")
            values.extend((f"%{word}%", f"%{word}%"))
        sql = "SELECT title, content FROM wiki_text WHERE " + " OR ".join(clauses)
        sql += " ORDER BY CASE WHEN LOWER(title) LIKE ? THEN 0 ELSE 1 END, fetched DESC LIMIT ?"
        values.extend((f"%{words[0]}%", limit))
        return self.db.execute(sql, values).fetchall()

    async def get_context(self, query: str) -> str:
        """Search the live wiki, save matching pages and their first-level links, then retrieve text."""
        try:
            data = await self._request({
                "action": "query", "list": "search",
                "srsearch": query, "srnamespace": 0, "srlimit": 6,
            })
            hits = data.get("query", {}).get("search", [])
            titles = [hit["title"] for hit in hits if hit.get("title")]
            pages = await self._fetch_titles(titles)

            # Follow internal article links from matching pages and save those pages too.
            linked_titles: list[str] = []
            for page in pages:
                for link in page.get("links", []):
                    title = link.get("title")
                    if title and title not in titles and title not in linked_titles:
                        linked_titles.append(title)
                    if len(linked_titles) >= 12:
                        break
                if len(linked_titles) >= 12:
                    break
            if linked_titles:
                await self._fetch_titles(linked_titles)
        except Exception as exc:
            print(f"[TaigaGPT] INSMP wiki search failed: {type(exc).__name__}: {exc}")

        rows = self._local_search(query, 6)
        if not rows:
            return "No matching INSMP wiki text is saved yet."
        chunks = []
        for title, content in rows:
            chunks.append(f"[{title}]\n{content[:2200]}")
        return "\n\n".join(chunks)[:10000]
