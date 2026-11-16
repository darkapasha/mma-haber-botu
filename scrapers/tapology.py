"""Tapology.com kazıyıcı.

Sayfalar services.http (curl_cffi, Chrome TLS parmak izi) ile çekilip BeautifulSoup ile ayrıştırılır.
Tapology'ye nazik olmak için istekler sıraya alınır ve sonuçlar önbelleğe yazılır.

HTML yapısı zaman zaman değiştiği için ayrıştırıcı belirli CSS sınıflarına bağlı değildir:
dövüşçü linkleri (/fightcenter/fighters/...) ve metin kalıpları üzerinden çalışır.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import quote_plus, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from config import Org, get_settings
from countries import country_flag
from models import Bout, Event, EventRef, Fighter, normalize_segment
from services import http
from services.cache import TTLCache

log = logging.getLogger(__name__)

BASE = "https://www.tapology.com"
EVENT_HREF_RE = re.compile(r"/fightcenter/events/(\d+)")
FIGHTER_SEL = 'a[href*="/fightcenter/fighters/"]'
MIN_INTERVAL = 2.5            # iki Tapology isteği arasında en az bekleme (sn)
LISTING_TTL = 30 * 60
EVENT_TTL = 15 * 60
BLOCK_COOLDOWN = 3 * 3600     # engellenince Tapology'ye bu süre boyunca hiç istek atılmaz (sn)


class ScrapeError(Exception):
    pass


class NotFound(ScrapeError):
    pass


class Blocked(ScrapeError):
    """Tapology erişimi engelledi (403 / Cloudflare); bekleme süresi dolana kadar istek atılmaz."""


@dataclass
class ListingItem:
    url: str
    name: str
    date: date | None

    @property
    def event_id(self) -> str | None:
        m = EVENT_HREF_RE.search(self.url)
        return m[1] if m else None


_cache = TTLCache()
_throttle = asyncio.Lock()
_last_request = 0.0
_blocked_until = 0.0          # time.monotonic() değeri; 0 = engel yok


# ---------------------------------------------------------------------------
# Getirme katmanı
# ---------------------------------------------------------------------------
def _is_challenge(html: str) -> bool:
    head = html[:6000].lower()
    return ("just a moment" in head or "cf-browser-verification" in head
            or "challenge-platform" in head or "attention required" in head)


def _block(reason: str) -> Blocked:
    """Engeli kaydeder ve (yalnızca engel ilk başladığında) uyarı yazar."""
    global _blocked_until
    if time.monotonic() >= _blocked_until:
        log.warning("%s — Tapology'ye %d saat istek atılmayacak", reason, BLOCK_COOLDOWN // 3600)
    _blocked_until = time.monotonic() + BLOCK_COOLDOWN
    return Blocked(reason)


async def _fetch_html(url: str) -> str:
    global _last_request
    if time.monotonic() < _blocked_until:
        mins = int((_blocked_until - time.monotonic()) // 60) + 1
        raise Blocked(f"Tapology şu an erişimi engelliyor, yaklaşık {mins} dk sonra tekrar denenecek")
    async with _throttle:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            try:
                resp = await http.get(url, retries=1)
            except http.NetworkError as exc:
                raise ScrapeError(f"Tapology'ye bağlanılamadı: {exc}") from exc
        finally:
            _last_request = time.monotonic()

    if resp.status_code == 404:
        raise NotFound(url)
    if resp.status_code in (403, 429):
        raise _block(f"Tapology erişimi engellendi (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise ScrapeError(f"Tapology HTTP {resp.status_code}: {url}")
    if _is_challenge(resp.text):
        # curl_cffi JavaScript çalıştıramaz; JS doğrulama sayfası gelirse geçilemez
        raise _block("Tapology Cloudflare JS doğrulaması istedi")
    return resp.text


# ---------------------------------------------------------------------------
# Tarih / saat ayrıştırma
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_TZ_ABBR = {
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "GMT": "UTC", "UTC": "UTC", "BST": "Europe/London", "CET": "Europe/Paris",
    "CEST": "Europe/Paris", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "JST": "Asia/Tokyo", "SGT": "Asia/Singapore", "ICT": "Asia/Bangkok", "TRT": "Europe/Istanbul",
}
TIME_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?\s?[Mm]\.?(?:\s*(" + "|".join(_TZ_ABBR) + r")\b)?")

_DATE_PATTERNS = [
    # Tapology ABD biçimi: 09.27.2025
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"), lambda m: date(int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I),
     lambda m: date(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(\d{4})\b", re.I),
     lambda m: date(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))),
]


def parse_date_text(text: str) -> tuple[date, int] | None:
    """Metindeki ilk tarihi ve bittiği konumu döndürür."""
    best: tuple[date, int, int] | None = None
    for rx, build in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                d = build(m)
            except (ValueError, KeyError):
                continue
            if best is None or m.start() < best[1]:
                best = (d, m.start(), m.end())
            break
    return (best[0], best[2]) if best else None


def _time_from_match(m: re.Match, default_tz: ZoneInfo) -> tuple[dtime, ZoneInfo]:
    hour, minute, ampm, abbr = int(m[1]) % 12, int(m[2]), m[3].lower(), m[4]
    if ampm == "p":
        hour += 12
    tz = ZoneInfo(_TZ_ABBR[abbr.upper()]) if abbr else default_tz
    return dtime(hour, minute), tz


def _combine(d: date, t: dtime, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Promosyon sayfası
# ---------------------------------------------------------------------------
def _parse_listing(html: str) -> list[ListingItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[ListingItem] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/fightcenter/events/"]'):
        href = a.get("href", "").split("?")[0].split("#")[0]
        if not EVENT_HREF_RE.search(href):
            continue
        url = urljoin(BASE, href)
        name = a.get_text(" ", strip=True)
        if not name or url in seen:
            continue
        seen.add(url)

        # Tarihi, yalnızca bu etkinliği içeren en yakın kapsayıcıdan oku
        found: date | None = None
        node: Tag | None = a
        for _ in range(5):
            node = node.parent if node else None
            if node is None:
                break
            links = {x.get("href", "").split("?")[0] for x in node.select('a[href*="/fightcenter/events/"]')
                     if EVENT_HREF_RE.search(x.get("href", ""))}
            if len(links) > 1:
                break
            parsed = parse_date_text(node.get_text(" ", strip=True))
            if parsed:
                found = parsed[0]
                break
        items.append(ListingItem(url=url, name=name, date=found))
    return items


async def _search_promotion(term: str) -> str:
    html = await _fetch_html(f"{BASE}/search?term={quote_plus(term)}&mainSearchFilter=orgs")
    soup = BeautifulSoup(html, "html.parser")
    links = [(urljoin(BASE, a["href"].split("?")[0]), a.get_text(" ", strip=True))
             for a in soup.select('a[href*="/fightcenter/promotions/"]') if a.get("href")]
    links = [(u, t) for u, t in links if re.search(r"/promotions/\d+", u)]
    if not links:
        raise ScrapeError(f"Tapology'de '{term}' promosyonu bulunamadı")
    t = term.lower()
    best = next((u for u, text in links if t in text.lower()), links[0][0])
    log.info("Tapology '%s' promosyonu çözüldü: %s (.env'de TAPOLOGY_URL_* ile sabitleyebilirsiniz)",
             term, best)
    return best


async def _promotion_url(org: Org, force_search: bool = False) -> str:
    if org.tapology_url and not force_search:
        return org.tapology_url
    return await _cache.get_or_set(f"promo:{org.key}", 24 * 3600,
                                   lambda: _search_promotion(org.tapology_search))


async def _listing(org: Org) -> list[ListingItem]:
    async def load() -> list[ListingItem]:
        url = await _promotion_url(org)
        try:
            html = await _fetch_html(url)
        except NotFound:
            log.warning("%s Tapology adresi 404 verdi, arama ile bulunuyor", org.name)
            html = await _fetch_html(await _promotion_url(org, force_search=True))
        items = _parse_listing(html)
        if not items:
            raise ScrapeError(f"{org.name} için Tapology'de etkinlik listesi okunamadı")
        return items

    return await _cache.get_or_set(f"listing:{org.key}", LISTING_TTL, load)


# ---------------------------------------------------------------------------
# Etkinlik sayfası
# ---------------------------------------------------------------------------
_WEIGHT_BY_LBS = {
    105: "Atomweight", 115: "Strawweight", 125: "Flyweight", 135: "Bantamweight",
    145: "Featherweight", 155: "Lightweight", 170: "Welterweight", 185: "Middleweight",
    205: "Light Heavyweight", 225: "Cruiserweight", 265: "Heavyweight",
}
_WEIGHT_NAME_RE = re.compile(
    r"\b(Atomweight|Strawweight|Flyweight|Bantamweight|Featherweight|Lightweight|Welterweight|"
    r"Middleweight|Light Heavyweight|Cruiserweight|Heavyweight|Catchweight|Openweight)\b", re.I)
_METHOD_RE = re.compile(
    r"\b(KO/TKO|TKO|KO|Submission|Decision|Draw|No Contest|DQ|Disqualification)\b", re.I)
_DEC_TYPE_RE = re.compile(r"\b(Unanimous|Split|Majority)\b", re.I)
# "R2", "Round 2", "Round 2 of 3", "Rd. 2"
_ROUND_RE = re.compile(r"\b(?:R|Rd\.?|Round)\s*(\d)(?:\s*(?:of|/)\s*\d)?\b", re.I)
# Raunt süresi "3:14" (etkinlik saati olan "10:00 PM" hariç)
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?![\d:])(?!\s*[AaPp]\.?\s?[Mm]\b)")
_SEGMENT_RE = re.compile(r"^\s*(Main Card|Prelims?|Preliminary Card|Early Prelims?)\b", re.I)
_BOUT_HINT_RE = re.compile(r"\b\d{1,3}-\d{1,3}(?:-\d{1,3})?\b|lbs|\bvs\.?\b|" + _METHOD_RE.pattern, re.I)
# Bitiriş detayı olamayacak parçalar
_DETAIL_STOP_RE = re.compile(
    r"^(?:R|Rd\.?|Round)\s*\d|^\d|lbs|^(?:win|loss|winner|draw|nc|w|l|vs\.?|def\.?)$|weight\b|"
    r"^(?:main card|prelims?|early prelims?|title|bout|fight)$", re.I)


def _fighter_links(el: Tag) -> list[tuple[str, str, Tag]]:
    found: dict[str, tuple[str, Tag]] = {}
    for a in el.select(FIGHTER_SEL):
        href = a.get("href", "").split("?")[0]
        name = a.get_text(" ", strip=True)
        if href and name and href not in found:
            found[href] = (name, a)
    return [(h, n, a) for h, (n, a) in found.items()]


def _bout_nodes(soup: BeautifulSoup) -> list[Tag]:
    tagged = soup.select("[data-bout-id]")
    if tagged:
        return [n for n in tagged if not n.find_parent(attrs={"data-bout-id": True})]

    # Genel yöntem: tam olarak iki farklı dövüşçü linki içeren en içteki kapsayıcı
    containers: dict[int, Tag] = {}
    hrefs: dict[int, set[str]] = {}
    for a in soup.select(FIGHTER_SEL):
        if not a.get_text(strip=True):
            continue
        href = a.get("href", "").split("?")[0]
        for p in a.parents:
            if p.name in ("li", "div", "tr", "section", "article"):
                containers[id(p)] = p
                hrefs.setdefault(id(p), set()).add(href)
    cands = {k for k, v in hrefs.items() if len(v) == 2}
    has_inner: set[int] = set()
    for k in cands:
        for p in containers[k].parents:
            if id(p) in cands:
                has_inner.add(id(p))
    nodes = [containers[k] for k in cands if k not in has_inner]
    nodes = [n for n in nodes if _BOUT_HINT_RE.search(n.get_text(" ", strip=True))]
    # Belge sırasını koru
    order = {id(el): i for i, el in enumerate(soup.find_all(True))}
    return sorted(nodes, key=lambda n: order.get(id(n), 0))


def _winner(tokens: list[str], names: list[str]) -> int | None:
    name_idx = [next((i for i, t in enumerate(tokens) if t == n), None) for n in names]
    if None in name_idx:
        return None
    win_idx = [i for i, t in enumerate(tokens) if t.lower() in {"win", "winner", "w"}]
    loss_idx = [i for i, t in enumerate(tokens) if t.lower() in {"loss", "l"}]
    if win_idx:
        w = win_idx[0]
        return min(range(2), key=lambda k: abs(name_idx[k] - w))
    if loss_idx:
        lo = loss_idx[0]
        return 1 - min(range(2), key=lambda k: abs(name_idx[k] - lo))
    joined = " ".join(tokens)
    for k in range(2):
        if re.search(re.escape(names[k]) + r"\s+(?:def\.|defeats|defeated)", joined, re.I):
            return k
    return None


def _clean_detail(text: str, names: list[str]) -> str | None:
    d = text.strip(" ,·-–—:()|")
    if (not d or len(d) > 40 or not re.match(r"[A-Za-z]", d) or d in names
            or _DETAIL_STOP_RE.search(d) or _METHOD_RE.fullmatch(d)):
        return None
    return d


def _method(tokens: list[str], names: list[str]) -> tuple[str | None, str | None, int]:
    """Bitiriş yöntemi, detayı ve yöntemin geçtiği token sırası.

    Tapology biçimleri: "KO/TKO | Punches", "Submission, Rear-Naked Choke", "Decision · Unanimous".
    """
    for i, tok in enumerate(tokens):
        m = _METHOD_RE.search(tok)
        if not m:
            continue
        method = m[1]
        low = method.lower()
        if low in {"decision", "draw"}:
            dm = _DEC_TYPE_RE.search(" ".join(tokens[i:i + 3]))
            return ("Decision" if low == "decision" else "Draw"), (dm[1].title() if dm else None), i
        detail = _clean_detail(tok[m.end():], names)
        if detail is None and i + 1 < len(tokens):
            detail = _clean_detail(tokens[i + 1], names)
        return method, detail, i
    return None, None, -1


def _round_time(tokens: list[str], start: int) -> tuple[int | None, str | None]:
    # Önce yöntemden sonraki kısma bak (kayıtlar / saatlerle karışmasın), sonra tümüne
    for part in (" | ".join(tokens[start:]), " | ".join(tokens)):
        rm = _ROUND_RE.search(part)
        cm = _CLOCK_RE.search(part)
        if rm or cm:
            return (int(rm[1]) if rm else None), (cm[1] if cm else None)
    return None, None


def _parse_bout(node: Tag) -> Bout | None:
    links = _fighter_links(node)[:2]
    if len(links) < 2:
        return None
    text = node.get_text(" | ", strip=True)
    tokens = [t.strip() for t in text.split("|") if t.strip()]
    names = [n for _, n, _ in links]

    flags = [country_flag(img.get("alt") or img.get("title") or img.get("src", ""))
             for img in node.select('img[src*="flag" i], img[alt*="flag" i], img[class*="flag" i]')]
    flags = [f for f in flags if f] + ["", ""]

    weight = None
    m = re.search(r"\b(\d{3})(?:\.\d)?\s*(?:lbs?|pounds)\b", text, re.I)
    if m:
        weight = _WEIGHT_BY_LBS.get(int(m[1]), f"Catchweight ({m[1]} lbs)")
    if (wm := _WEIGHT_NAME_RE.search(text)) and (weight is None or weight.startswith("Catch")):
        weight = wm[1].title()

    method, detail, idx = _method(tokens, names)
    rnd, rtime = _round_time(tokens, idx) if method else (None, None)

    seg_marker = node.find_previous(string=_SEGMENT_RE)
    records = [re.search(r"\b\d{1,3}-\d{1,3}(?:-\d{1,3})?\b", a.parent.get_text(" ", strip=True))
               if a.parent else None for _, _, a in links]

    return Bout(
        fighter_a=Fighter(names[0], flags[0], records[0][0] if records[0] else None),
        fighter_b=Fighter(names[1], flags[1], records[1][0] if records[1] else None),
        weight_class=weight,
        segment=(normalize_segment(str(seg_marker)) if seg_marker else None) or "main",
        title_fight=bool(re.search(r"\btitle\b|\bchampionship bout\b", text, re.I)),
        winner=_winner(tokens, names) if method else None,
        method=method,
        method_detail=detail,
        round=rnd,
        time=rtime,
    )


def _jsonld_event(soup: BeautifulSoup) -> dict | None:
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                if "startDate" in x:
                    return x
                stack.extend(v for v in x.values() if isinstance(v, (list, dict)))
    return None


def _event_name(soup: BeautifulSoup, ld: dict | None) -> str:
    if ld and ld.get("name"):
        return str(ld["name"]).strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].split(" | ")[0].strip()
    return soup.title.get_text(strip=True).split(" | ")[0] if soup.title else "Etkinlik"


def parse_event_page(html: str, url: str, org_key: str, listing_date: date | None = None) -> Event:
    soup = BeautifulSoup(html, "html.parser")
    tz = get_settings().tapology_tz
    ld = _jsonld_event(soup)
    text = soup.get_text(" | ", strip=True)

    start: datetime | None = None
    time_known = False
    if ld and isinstance(ld.get("startDate"), str):
        raw = ld["startDate"]
        try:
            if "T" in raw:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                start = (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(timezone.utc)
                time_known = True
            else:
                listing_date = listing_date or date.fromisoformat(raw[:10])
        except ValueError:
            pass

    if start is None:
        parsed = parse_date_text(text)
        d = parsed[0] if parsed else listing_date
        if parsed and (tm := TIME_RE.search(text, parsed[1], parsed[1] + 40)):
            t, ttz = _time_from_match(tm, tz)
            start, time_known = _combine(parsed[0], t, ttz), True
        elif d:
            start = _combine(d, dtime(12, 0), tz)       # saat bilinmiyor

    main_start = start if time_known else None
    for m in re.finditer(r"Main Card", text, re.I):
        tm = TIME_RE.search(text, m.end(), m.end() + 80)
        if tm and start:
            t, ttz = _time_from_match(tm, tz)
            local_day = start.astimezone(ttz).date()
            candidate = _combine(local_day, t, ttz)
            if candidate < start - timedelta(hours=1):
                candidate += timedelta(days=1)
            main_start, time_known = candidate, True
            break

    venue = None
    if ld and isinstance(ld.get("location"), dict):
        loc = ld["location"]
        addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
        venue = ", ".join(x for x in (loc.get("name"), addr.get("addressLocality"),
                                      addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else None) if x) or None
    if not venue and (vm := re.search(r"Venue:\s*\|\s*([^|]{3,80})", text)):
        venue = vm[1].strip()

    bouts: list[Bout] = []
    seen_pairs: set[frozenset[str]] = set()
    for node in _bout_nodes(soup):
        b = _parse_bout(node)
        if not b:
            continue
        pair = frozenset((b.fighter_a.name, b.fighter_b.name))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        bouts.append(b)

    now = datetime.now(timezone.utc)
    completed = any(b.finished for b in bouts) or bool(start and start < now - timedelta(hours=10))
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return Event(
        id=f"tapology:{slug}",
        org_key=org_key,
        name=_event_name(soup, ld),
        start=start,
        main_card_start=main_start,
        time_known=time_known,
        venue=venue,
        url=url,
        bouts=bouts,
        completed=completed,
        source="Tapology",
    )


async def _event(item: ListingItem, org: Org) -> Event:
    async def load() -> Event:
        return parse_event_page(await _fetch_html(item.url), item.url, org.key, item.date)
    return await _cache.get_or_set(f"event:{item.url}", EVENT_TTL, load)


# ---------------------------------------------------------------------------
# Genel arayüz
# ---------------------------------------------------------------------------
async def _load_many(items: list[ListingItem], org: Org) -> list[Event]:
    events = []
    for it in items:
        try:
            events.append(await _event(it, org))
        except ScrapeError as exc:
            log.warning("Etkinlik okunamadı %s: %s", it.url, exc)
    return events


async def next_event(org: Org) -> Event | None:
    items = await _listing(org)
    today = datetime.now(get_settings().tapology_tz).date()
    now = datetime.now(timezone.utc)
    dated = [i for i in items if i.date]
    if dated:
        cands = sorted((i for i in dated if i.date >= today - timedelta(days=1)), key=lambda i: i.date)[:3]
    else:
        cands = items[:6]
    events = [e for e in await _load_many(cands, org)
              if not e.completed and (e.start is None or e.start > now - timedelta(hours=12))]
    events.sort(key=lambda e: e.start or now + timedelta(days=999))
    return events[0] if events else None


async def past_events(org: Org, limit: int = 5) -> list[EventRef]:
    """Promosyon sayfasından son `limit` geçmiş etkinlik (etkinlik sayfası açılmaz, ucuzdur)."""
    items = await _listing(org)
    today = datetime.now(get_settings().tapology_tz).date()
    dated = [i for i in items if i.date]
    if dated:
        past = sorted((i for i in dated if i.date <= today), key=lambda i: i.date, reverse=True)
    else:
        past = items                     # tarih okunamadıysa sayfa sırası (Tapology'de en yeni üstte)
    return [EventRef(ref=f"t:{i.event_id}", name=i.name, date=i.date)
            for i in past if i.event_id][:limit]


async def event_by_id(org: Org, event_id: str) -> Event:
    """Tapology sayısal etkinlik ID'si ile etkinliği (sonuçlarıyla) getirir."""
    try:
        items = await _listing(org)
    except ScrapeError:
        items = []
    item = next((i for i in items if i.event_id == event_id), None) \
        or ListingItem(url=f"{BASE}/fightcenter/events/{event_id}", name="", date=None)
    return await _event(item, org)