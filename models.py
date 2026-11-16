"""Ortak veri modelleri."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

SEGMENT_ORDER = {"main": 0, "prelims": 1, "early": 2}


def normalize_segment(text: str | None) -> str | None:
    t = (text or "").lower()
    if "early" in t:
        return "early"
    if "prelim" in t:
        return "prelims"
    if "main" in t:
        return "main"
    return None


_METHOD_SPLIT_RE = re.compile(r"\s+-\s+|\s*[–—,·(:]\s*")


def split_method(raw: str | None) -> tuple[str | None, str | None]:
    """'Submission (Rear Naked Choke)' / 'KO/TKO - Punches' -> (yöntem, detay)."""
    if not raw or not raw.strip():
        return None, None
    parts = _METHOD_SPLIT_RE.split(raw.strip().rstrip(")"), maxsplit=1)
    method = parts[0].strip() or None
    detail = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return method, detail


@dataclass
class Fighter:
    name: str
    flag: str = ""
    record: str | None = None


@dataclass
class Bout:
    fighter_a: Fighter
    fighter_b: Fighter
    weight_class: str | None = None
    segment: str = "main"            # main | prelims | early
    title_fight: bool = False
    winner: int | None = None        # 0 -> fighter_a, 1 -> fighter_b
    method: str | None = None        # "KO/TKO", "Submission", "Decision", ...
    method_detail: str | None = None # "Punches", "Rear-Naked Choke", "Unanimous", ...
    round: int | None = None         # bittiği raunt
    time: str | None = None          # raunt içindeki süre, "3:14"
    start: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.method is not None or self.winner is not None


@dataclass
class Event:
    id: str
    org_key: str
    name: str
    start: datetime | None                 # UTC
    main_card_start: datetime | None = None
    time_known: bool = True                # False ise yalnızca tarih biliniyor
    venue: str | None = None
    url: str | None = None
    bouts: list[Bout] = field(default_factory=list)
    completed: bool = False
    source: str = ""


@dataclass
class EventRef:
    """Buton listesi için hafif etkinlik kaydı."""
    ref: str                               # callback'te taşınan kısa kimlik: "e:600051234" | "t:114644"
    name: str
    date: date | None


@dataclass
class RankEntry:
    rank: str
    name: str
    flag: str = ""
    change: str = ""


@dataclass
class Division:
    name: str
    key: str                               # normalize edilmiş: "women flyweight"
    champion: RankEntry | None
    entries: list[RankEntry]
    interim: RankEntry | None = None


@dataclass
class NewsItem:
    id: str
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str = ""
    sport: str = "mma"                     # "mma" | "f1"
    content: str = ""                      # RSS'te tam metin (content:encoded) varsa


@dataclass
class TranslatedNews:
    title: str                             # Türkçe başlık
    summary: str                           # haberin tamamından Türkçe özet
    translated: bool = True                # False: çeviri yapılamadı, orijinal metin