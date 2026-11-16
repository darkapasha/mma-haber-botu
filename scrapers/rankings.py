"""Resmi sıralama sayfaları: ufc.com/rankings ve onefc.com/athlete-rankings."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from countries import country_flag
from models import Division, RankEntry
from services import http
from services.cache import TTLCache

UFC_URL = "https://www.ufc.com/rankings"
ONE_URL = "https://www.onefc.com/athlete-rankings/"
ONE_CHAMPIONS_URL = "https://www.onefc.com/world-champions/"
TTL = 6 * 3600

_cache = TTLCache()

_TR_FOLD = str.maketrans("ıİğĞüÜşŞöÖçÇâ", "iigguussoocca")

# Türkçe / kısa siklet adları -> İngilizce
DIVISION_ALIASES = {
    "p4p": "pound-for-pound", "pfp": "pound-for-pound", "pound for pound": "pound-for-pound",
    "atom": "atomweight", "saman": "strawweight", "sinek": "flyweight", "horoz": "bantamweight",
    "tuy": "featherweight", "hafif": "lightweight", "yari orta": "welterweight",
    "yariorta": "welterweight", "orta": "middleweight", "yari agir": "light heavyweight",
    "yariagir": "light heavyweight", "agir": "heavyweight", "lhw": "light heavyweight",
    "hw": "heavyweight", "mw": "middleweight", "ww": "welterweight", "lw": "lightweight",
    "fw": "featherweight", "bw": "bantamweight", "flw": "flyweight",
}
_WOMEN_PREFIXES = ("kadinlar", "kadin", "bayanlar", "bayan", "womens", "women", "w ")


class RankingsError(Exception):
    pass


def division_key(name: str) -> str:
    n = name.lower().replace("’", "'").replace("women's", "women").replace("men's", "")
    n = re.sub(r"\bmma\b", "", n)
    n = n.replace("pound for pound", "pound-for-pound")
    return re.sub(r"\s+", " ", n).strip()


def query_key(words: list[str]) -> str:
    q = " ".join(words).translate(_TR_FOLD).lower().replace("'", "").strip()
    q = re.sub(r"\bsiklet\b", "", q).strip()
    women = False
    for p in _WOMEN_PREFIXES:
        if q.startswith(p):
            women, q = True, q[len(p):].strip()
            break
    q = DIVISION_ALIASES.get(q, q)
    return f"women {q}" if women else q


def _rank_change(text: str) -> str:
    t = text.lower()
    num = re.search(r"\d+", t)
    n = num.group(0) if num else ""
    if "increase" in t:
        return f"▲{n}"
    if "decrease" in t:
        return f"▼{n}"
    if "new" in t or t.strip() == "nr":
        return "🆕"
    return ""


def parse_ufc(html: str) -> list[Division]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Division] = {}
    for g in soup.select("div.view-grouping"):
        header = g.select_one(".view-grouping-header")
        if not header:
            continue
        name = next((s.strip() for s in header.find_all(string=True, recursive=False) if s.strip()), "")
        key = division_key(name)
        if not name or key in out:           # sayfada her tablo iki kez yer alıyor
            continue
        champ_el = g.select_one(".rankings--athlete--champion h5 a")
        entries = []
        for tr in g.select("tbody tr"):
            rank_td = tr.select_one("td.views-field-weight-class-rank")
            name_a = tr.select_one("td.views-field-title a")
            if not rank_td or not name_a:
                continue
            rank = rank_td.get_text(strip=True)
            if rank.isdigit() and int(rank) > 10:
                continue
            change_td = tr.select_one("td.views-field-weight-class-rank-change")
            entries.append(RankEntry(rank=rank, name=name_a.get_text(strip=True),
                                     change=_rank_change(change_td.get_text(" ", strip=True)) if change_td else ""))
        out[key] = Division(
            name=name, key=key,
            champion=RankEntry("C", champ_el.get_text(strip=True)) if champ_el else None,
            entries=entries,
        )
    return list(out.values())


def parse_one(html: str) -> list[Division]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Division] = []
    for h in soup.select("h3.ranking-header"):
        title = h.get_text(" ", strip=True)
        if not re.search(r"\bMMA\b", title, re.I):
            continue
        table = h.find_next("table")
        if not table:
            continue
        champion = interim = None
        entries: list[RankEntry] = []
        for tr in table.select("tr"):
            rank_td = tr.select_one("td.rank")
            name_el = tr.select_one("td.name h3") or tr.select_one("td.name a")
            if not rank_td or not name_el:
                continue
            rank = rank_td.get_text(" ", strip=True)
            country = tr.select_one("td.country")
            entry = RankEntry(rank=rank, name=name_el.get_text(" ", strip=True),
                              flag=country_flag(country.get_text(strip=True)) if country else "")
            if "interim" in rank.lower():
                interim = entry
            elif "champion" in rank.lower():
                champion = entry
            else:
                entries.append(entry)
        pretty = re.sub(r"\s*\bMMA\b", "", title, flags=re.I).strip().title().replace("'S", "'s")
        out.append(Division(name=pretty, key=division_key(title), champion=champion,
                            entries=entries[:10], interim=interim))
    return out


_NICKNAME_RE = re.compile(r"\s*[“\"][^”\"]+[”\"]\s*")


def parse_one_champions(html: str) -> list[Division]:
    """onefc.com/world-champions: her kart bir sporcu, başlıkta '&' ile ayrılmış unvanlar."""
    soup = BeautifulSoup(html, "html.parser")
    divisions: dict[str, Division] = {}
    for card in soup.select("div.simple-post-card.is-champion"):
        title_el = card.select_one("h4.champion-title")
        name_el = card.select_one("h3")
        if not title_el or not name_el:
            continue
        name = _NICKNAME_RE.sub(" ", name_el.get_text(" ", strip=True)).strip()
        country = card.select_one("td")
        flag = country_flag(country.get_text(strip=True).split("/")[0].strip()) if country else ""
        for title in title_el.get_text(" ", strip=True).split("&"):
            title = title.strip()
            if not re.search(r"\bMMA\b", title, re.I):
                continue
            interim = bool(re.match(r"interim\b", title, re.I))
            clean = re.sub(r"^interim\s+", "", title, flags=re.I)
            key = division_key(clean)
            pretty = re.sub(r"\s*\bMMA\b", "", clean, flags=re.I).strip().title().replace("'S", "'s")
            div = divisions.setdefault(key, Division(name=pretty, key=key, champion=None, entries=[]))
            entry = RankEntry("C", name, flag)
            if interim:
                div.interim = entry
            else:
                div.champion = entry
    return list(divisions.values())


async def _load_one() -> list[Division]:
    # ONE şu an yalnızca Muay Thai için ilk 5 yayınlıyor; MMA için şampiyonlar
    # /world-champions sayfasından alınır, MMA sıralaması yayınlanırsa birleştirilir.
    champions = await _load(ONE_CHAMPIONS_URL, parse_one_champions)
    try:
        ranked = {d.key: d for d in await _load(ONE_URL, parse_one)}
    except RankingsError:
        ranked = {}
    for d in champions:
        if r := ranked.pop(d.key, None):
            d.entries = r.entries
    return champions + list(ranked.values())


async def _load(url: str, parser) -> list[Division]:
    resp = await http.get(url)
    if resp.status_code != 200:
        raise RankingsError(f"{url} HTTP {resp.status_code}")
    divisions = parser(resp.text)
    if not divisions:
        raise RankingsError(f"{url} sayfasından sıralama okunamadı (yapı değişmiş olabilir)")
    return divisions


async def get_rankings(source: str) -> list[Division]:
    if source == "ufc":
        return await _cache.get_or_set("ufc", TTL, lambda: _load(UFC_URL, parse_ufc))
    if source == "one":
        return await _cache.get_or_set("one", TTL, _load_one)
    raise RankingsError(f"Bilinmeyen sıralama kaynağı: {source}")


def find_division(divisions: list[Division], words: list[str]) -> Division | None:
    key = query_key(words)
    for d in divisions:
        if d.key == key:
            return d
    # "pound-for-pound" tek başına erkekler P4P'yi getirsin
    for d in divisions:
        if d.key.endswith(key) and not d.key.startswith("women"):
            return d
    return None
