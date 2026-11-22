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
        words = list(dict.fromkeys(re.findall(r"[\\w-]{3,}", query.lower())))[:12]
        if not words:
            return []
        rows = self.db.execute("SELECT title, content FROM wiki_text").fetchall()
        scored: list[tuple[int, str, str]] = []
        for title, content in rows:
            title_l = title.lower()
            content_l = content.lower()
            title_hits = sum(1 for word in words if word in title_l)
            body_hits = sum(1 for word in words if word in content_l)
            total_hits = title_hits + body_hits
            # Avoid returning a page just because it contains one generic word.
            required = 1 if len(words) == 1 else 2
            if total_hits < required:
                continue
            scored.append((title_hits * 4 + body_hits, title, content))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(title, content) for _, title, content in scored[:limit]]

    async def get_context(self, query: str) -> str:
        """Search live wiki, save matching articles and linked articles, then return grounded excerpts."""
        live_status = "LIVE SEARCH: failed"
        try:
            data = await self._request({
                "action": "query", "list": "search",
                "srsearch": query, "srnamespace": 0, "srlimit": 8,
            })
            hits = data.get("query", {}).get("search", [])
            titles = [hit["title"] for hit in hits if hit.get("title")]
            if titles:
                pages = await self._fetch_titles(titles)
                live_status = f"LIVE SEARCH: succeeded; found {len(titles)} matching page(s)"
            else:
                pages = []
                live_status = "LIVE SEARCH: succeeded; the wiki returned no matching pages"

            # Follow internal article links from the matched pages and save their text too.
            linked_titles: list[str] = []
            for page in pages:
                for link in page.get("links", []):
                    title = link.get("title")
                    if title and title not in titles and title not in linked_titles:
                        linked_titles.append(title)
                    if len(linked_titles) >= 20:
                        break
                if len(linked_titles) >= 20:
                    break
            if linked_titles:
                await self._fetch_titles(linked_titles)
        except Exception as exc:
            live_status = f"LIVE SEARCH ERROR: {type(exc).__name__}: {exc}"
            print(f"[TaigaGPT] INSMP wiki search failed: {live_status}")

        rows = self._local_search(query, 6)
        if not rows:
            return (
                f"{live_status}\\n"
                "NO VERIFIED WIKI EXCERPTS FOUND. Do not answer wiki-specific factual questions "
                "from memory or guess. Tell the user the wiki search returned no usable page text."
            )
        chunks = []
        for title, content in rows:
            chunks.append(f"[WIKI PAGE: {title}]\\n{content[:2600]}")
        return (
            f"{live_status}\\n"
            "The following are the only retrieved wiki excerpts available for this question.\\n\\n"
            + "\\n\\n".join(chunks)[:10500]
        )
