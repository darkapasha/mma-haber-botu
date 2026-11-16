"""RSS haber toplama, kategori tespiti ve haberin tam metnini çekme."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
from bs4 import BeautifulSoup

from config import get_settings
from models import NewsItem
from services import http
from services.cache import TTLCache

log = logging.getLogger(__name__)
_cache = TTLCache()
_failing: set[str] = set()

FULL_TEXT_MIN = 800          # RSS'teki metin bundan kısaysa haberin sayfası açılır
FULL_TEXT_MAX = 12000        # çeviri modeline gönderilecek en fazla karakter


@dataclass(frozen=True)
class AlertRule:
    key: str
    emoji: str
    label: str
    pattern: re.Pattern


def _rx(*parts: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


# Sıra önemli: ilk eşleşen kategori başlığı belirler
ALERT_RULES: dict[str, tuple[AlertRule, ...]] = {
    "mma": (
        AlertRule("weight", "⚖️", "TARTI", _rx(
            r"miss(?:es|ed)?\s+weight", r"weigh[- ]?ins?", r"weight\s+cut", r"overweight",
            r"catchweight", r"on weight", r"heavy\s+for")),
        AlertRule("cancel", "🚨", "İPTAL", _rx(
            r"cancel(?:l)?ed", r"cancel(?:l)?ation", r"scrapped", r"called off", r"off the card",
            r"falls? (?:through|apart)", r"nixed")),
        AlertRule("injury", "🚑", "SAKATLIK", _rx(
            r"injur(?:y|ed|ies)", r"pulls? out", r"pulled out", r"withdraw(?:s|n|al)?", r"forced out",
            r"out of (?:the )?(?:fight|bout)")),
        AlertRule("replacement", "🔁", "DEĞİŞİKLİK", _rx(
            r"replacement", r"replaces?", r"steps? in", r"short[- ]notice", r"new opponent")),
    ),
    "f1": (
        AlertRule("penalty", "⚠️", "CEZA", _rx(
            r"penalt(?:y|ies)", r"grid drop", r"disqualif(?:ied|ication)", r"stewards?",
            r"investigat(?:ion|ed)", r"reprimand", r"fined?")),
        AlertRule("crash", "💥", "KAZA", _rx(
            r"crash(?:es|ed)?", r"collision", r"red flag", r"shunt")),
        AlertRule("injury", "🚑", "SAKATLIK", _rx(
            r"injur(?:y|ed|ies)", r"hospital", r"ruled out", r"miss(?:es)? (?:the )?(?:race|grand prix)")),
        AlertRule("transfer", "🔁", "TRANSFER", _rx(
            r"signs?", r"signed", r"contract", r"joins?", r"seat", r"replaces?", r"line-?up",
            r"leaves?", r"departure", r"extension")),
    ),
}


def classify(item: NewsItem) -> AlertRule | None:
    haystack = f"{item.title} {item.summary[:280]}"
    return next((r for r in ALERT_RULES.get(item.sport, ()) if r.pattern.search(haystack)), None)


def title_key(title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())[:120]
    return hashlib.sha1(norm.encode()).hexdigest()


def _clean(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def _published(entry) -> datetime | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc) if st else None


async def fetch_feed(sport: str, name: str, url: str) -> list[NewsItem]:
    resp = await http.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, "
                                                  "application/xml;q=0.9, */*;q=0.8"})
    if resp.status_code != 200:
        raise RuntimeError(f"{name} RSS HTTP {resp.status_code}")
    parsed = await asyncio.to_thread(feedparser.parse, resp.content)
    items = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = _clean(e.get("title") or "")
        if not title or not link:
            continue
        content = (e.get("content") or [{}])[0].get("value", "")
        items.append(NewsItem(
            id=e.get("id") or link,
            title=title,
            link=link,
            source=name,
            published=_published(e),
            summary=_clean(e.get("summary") or e.get("description") or ""),
            sport=sport,
            content=_clean(content) if content else "",
        ))
    return items


async def fetch_all(use_cache: bool = True, sport: str | None = None) -> list[NewsItem]:
    async def load() -> list[NewsItem]:
        feeds = get_settings().feeds
        results = await asyncio.gather(*(fetch_feed(sp, n, u) for sp, n, u in feeds),
                                       return_exceptions=True)
        items: list[NewsItem] = []
        for (_, name, _), res in zip(feeds, results):
            if isinstance(res, Exception):
                # Sürekli bozuk bir kaynak log'u doldurmasın: yalnızca ilk hatada uyar
                level = logging.DEBUG if name in _failing else logging.WARNING
                log.log(level, "RSS okunamadı (%s): %s", name, res)
                _failing.add(name)
            else:
                _failing.discard(name)
                items.extend(res)
        items.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items

    items = await load() if not use_cache else await _cache.get_or_set("all", 300, load)
    return [i for i in items if sport is None or i.sport == sport]


async def recent(hours: int = 24, limit: int = 12, sport: str | None = None) -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = [i for i in await fetch_all(sport=sport) if i.published and i.published >= cutoff]
    # Başlığı aynı olan haberleri tekilleştir
    seen, out = set(), []
    for i in items:
        k = title_key(i.title)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out[:limit]


def _extract(html: str) -> str:
    import trafilatura                   # ağır import; yalnızca gerektiğinde
    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""


async def full_text(item: NewsItem) -> str:
    """Haberin tam metni: RSS'te varsa oradan, yoksa haber sayfasından; olmazsa RSS özeti."""
    if len(item.content) >= FULL_TEXT_MIN:
        return item.content[:FULL_TEXT_MAX]
    try:
        resp = await http.get(item.link)
        if resp.status_code == 200:
            text = await asyncio.to_thread(_extract, resp.text)
            if len(text) > len(item.summary):
                return text[:FULL_TEXT_MAX]
        else:
            log.info("Haber sayfası açılamadı (HTTP %s): %s", resp.status_code, item.link)
    except Exception as exc:
        log.info("Haber sayfası okunamadı: %s (%s)", item.link, exc)
    return item.content or item.summary
