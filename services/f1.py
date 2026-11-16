"""Formula 1 verisi: Jolpica (Ergast'ın devamı) API'si — takvim, sonuçlar, puan durumu."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services import http
from services.cache import TTLCache

log = logging.getLogger(__name__)

BASE = "https://api.jolpi.ca/ergast/f1"
_cache = TTLCache()

# Jolpica alan adı -> (Türkçe ad, hatırlatma gönderilsin mi)
SESSIONS: dict[str, tuple[str, bool]] = {
    "FirstPractice": ("1. Antrenman", False),
    "SecondPractice": ("2. Antrenman", False),
    "ThirdPractice": ("3. Antrenman", False),
    "SprintQualifying": ("Sprint Sıralama", True),
    "Sprint": ("Sprint", True),
    "Qualifying": ("Sıralama", True),
    "Race": ("Yarış", True),
}

# Jolpica uyruk adı -> ISO-2 ülke kodu (country_flag kodu doğrudan bayrağa çevirir)
_NATIONALITY = {
    "dutch": "NL", "british": "GB", "spanish": "ES", "mexican": "MX", "monegasque": "MC",
    "australian": "AU", "german": "DE", "french": "FR", "canadian": "CA", "japanese": "JP",
    "thai": "TH", "chinese": "CN", "danish": "DK", "finnish": "FI", "american": "US",
    "italian": "IT", "brazilian": "BR", "argentine": "AR", "argentinian": "AR",
    "new zealander": "NZ", "swiss": "CH", "austrian": "AT", "belgian": "BE", "polish": "PL",
    "russian": "RU", "swedish": "SE", "estonian": "EE", "indian": "IN", "israeli": "IL",
}


class F1Error(Exception):
    pass


@dataclass
class Session:
    key: str
    name: str
    start: datetime          # UTC
    remind: bool


@dataclass
class Race:
    season: str
    round: int
    name: str
    circuit: str
    locality: str
    country: str
    sessions: list[Session] = field(default_factory=list)

    @property
    def race_start(self) -> datetime | None:
        return next((x.start for x in self.sessions if x.key == "Race"), None)

    @property
    def is_sprint(self) -> bool:
        return any(x.key == "Sprint" for x in self.sessions)


@dataclass
class ResultRow:
    position: str
    driver: str
    nationality: str
    team: str
    time: str                # kazanan için toplam süre, diğerleri için fark ya da durum
    points: str
    grid: str = ""


@dataclass
class RaceResult:
    race: str
    date: str
    rows: list[ResultRow]


@dataclass
class Standing:
    position: str
    name: str
    nationality: str
    team: str
    points: str
    wins: str


def nationality_country(nat: str) -> str:
    return _NATIONALITY.get(nat.lower(), nat)


async def _get(path: str, ttl: int) -> dict:
    async def load() -> dict:
        resp = await http.get(f"{BASE}/{path}", headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise F1Error(f"Jolpica HTTP {resp.status_code}: {path}")
        return resp.json()["MRData"]
    return await _cache.get_or_set(path, ttl, load)


def _dt(block: dict | None) -> datetime | None:
    if not block or not block.get("date"):
        return None
    t = block.get("time") or "12:00:00Z"
    try:
        return datetime.fromisoformat(f"{block['date']}T{t.replace('Z', '+00:00')}")
    except ValueError:
        return None


def _race(r: dict) -> Race:
    loc = (r.get("Circuit") or {}).get("Location") or {}
    race = Race(season=r.get("season", ""), round=int(r.get("round", 0)), name=r.get("raceName", ""),
                circuit=(r.get("Circuit") or {}).get("circuitName", ""),
                locality=loc.get("locality", ""), country=loc.get("country", ""))
    for key, (tr_name, remind) in SESSIONS.items():
        start = _dt(r) if key == "Race" else _dt(r.get(key))
        if start:
            race.sessions.append(Session(key, tr_name, start, remind))
    race.sessions.sort(key=lambda x: x.start)
    return race


async def schedule() -> list[Race]:
    data = await _get("current.json", 6 * 3600)
    return [_race(r) for r in data["RaceTable"]["Races"]]


async def next_race() -> Race | None:
    """Yarışı henüz bitmemiş ilk Grand Prix (yarış başladıktan 3 saat sonrasına kadar)."""
    now = datetime.now(timezone.utc)
    for race in await schedule():
        start = race.race_start
        if start and (start - now).total_seconds() > -3 * 3600:
            return race
    return None


def _result_rows(results: list[dict]) -> list[ResultRow]:
    rows = []
    for x in results:
        d = x.get("Driver") or {}
        time = (x.get("Time") or {}).get("time") or x.get("status", "")
        rows.append(ResultRow(
            position=x.get("position", ""),
            driver=f"{d.get('givenName', '')} {d.get('familyName', '')}".strip(),
            nationality=d.get("nationality", ""),
            team=(x.get("Constructor") or {}).get("name", ""),
            time=time, points=x.get("points", "0"), grid=x.get("grid", "")))
    return rows


async def last_result() -> RaceResult | None:
    data = await _get("current/last/results.json", 600)
    races = data["RaceTable"]["Races"]
    if not races:
        return None
    r = races[0]
    return RaceResult(r.get("raceName", ""), r.get("date", ""), _result_rows(r.get("Results") or []))


async def driver_standings() -> tuple[str, list[Standing]]:
    data = await _get("current/driverStandings.json", 1800)
    lists = data["StandingsTable"]["StandingsLists"]
    if not lists:
        return "", []
    out = []
    for x in lists[0]["DriverStandings"]:
        d = x.get("Driver") or {}
        teams = x.get("Constructors") or [{}]
        out.append(Standing(x.get("position", x.get("positionText", "-")),
                            f"{d.get('givenName', '')} {d.get('familyName', '')}".strip(),
                            d.get("nationality", ""), teams[-1].get("name", ""),
                            x.get("points", "0"), x.get("wins", "0")))
    return lists[0].get("round", ""), out


async def constructor_standings() -> tuple[str, list[Standing]]:
    data = await _get("current/constructorStandings.json", 1800)
    lists = data["StandingsTable"]["StandingsLists"]
    if not lists:
        return "", []
    out = []
    for x in lists[0]["ConstructorStandings"]:
        c = x.get("Constructor") or {}
        out.append(Standing(x.get("position", x.get("positionText", "-")), c.get("name", ""),
                            c.get("nationality", ""), "", x.get("points", "0"), x.get("wins", "0")))
    return lists[0].get("round", ""), out
