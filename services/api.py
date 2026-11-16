"""ESPN MMA site API istemcisi (UFC, PFL)."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from countries import country_flag
from models import SEGMENT_ORDER, Bout, Event, EventRef, Fighter, normalize_segment, split_method
from services import http
from services.cache import TTLCache

log = logging.getLogger(__name__)

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/{league}/scoreboard"
PAST_WINDOW_DAYS = 120
_cache = TTLCache()


class ESPNError(Exception):
    pass


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fighter(comp: dict) -> Fighter:
    athlete = comp.get("athlete") or {}
    name = (athlete.get("displayName") or athlete.get("fullName")
            or athlete.get("shortName") or "TBA")
    flag = athlete.get("flag") or {}
    records = comp.get("records") or []
    return Fighter(
        name=name,
        flag=country_flag(flag.get("alt")) if isinstance(flag, dict) else "",
        record=records[0].get("summary") if records and isinstance(records[0], dict) else None,
    )


def _result(status: dict) -> tuple[str | None, str | None]:
    """ESPN bitiriş bilgisini (yöntem, detay) olarak döndürür."""
    stype = status.get("type") or {}
    result = status.get("result") or {}
    raw = (result.get("displayName") or result.get("shortDisplayName") or result.get("name")
           or stype.get("detail") or stype.get("shortDetail"))
    if not raw or raw.strip().lower() in {"final", "ft", "post"}:
        return None, None
    method, detail = split_method(raw)
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    extra = result.get("description") or target.get("displayName")
    if isinstance(extra, str) and extra.strip().lower() not in {"", raw.strip().lower(), (method or "").lower()}:
        detail = detail or extra.strip()
    return method, detail


_WINNER_METHOD = {"kotko": "KO/TKO", "ko": "KO/TKO", "tko": "KO/TKO", "decision": "Decision",
                  "submission": "Submission", "dq": "DQ", "disqualification": "DQ"}


def _method_from_details(c: dict, status: dict) -> str | None:
    """ESPN 'result' alanı boşsa bitirişi olay akışından ('Unofficial Winner Decision') ya da
    son raundun 5:00'te bitmesinden çıkarır. Bilinmiyorsa None (radyo uydurmasın)."""
    for d in c.get("details") or []:
        text = ((d.get("type") or {}).get("text") or "") if isinstance(d, dict) else ""
        m = re.match(r"\s*Unofficial Winner\s+(.+)", text, re.I)
        if m:
            return _WINNER_METHOD.get(m[1].strip().lower(), m[1].strip())
    periods = ((c.get("format") or {}).get("regulation") or {}).get("periods")
    if periods and status.get("period") == periods and status.get("displayClock") == "5:00":
        return "Decision"
    return None


def _parse_bout(c: dict) -> Bout | None:
    comps = sorted(c.get("competitors") or [], key=lambda x: x.get("order", 0))
    if len(comps) < 2:
        return None

    seg = c.get("cardSegment") or {}
    seg_text = " ".join(str(seg.get(k, "")) for k in ("name", "description", "abbreviation")) \
        if isinstance(seg, dict) else str(seg)

    ctype = c.get("type") or {}
    weight = ctype.get("abbreviation") or ctype.get("text") if isinstance(ctype, dict) else None
    notes = " ".join(str(n.get("text", "")) for n in c.get("notes") or [] if isinstance(n, dict))

    status = c.get("status") or {}
    stype = status.get("type") or {}
    done = bool(stype.get("completed")) or stype.get("state") == "post"
    method, detail = _result(status) if done else (None, None)
    if done and not method:
        method = _method_from_details(c, status)

    winner = next((i for i, x in enumerate(comps[:2]) if x.get("winner") is True), None)
    period = status.get("period")
    clock = status.get("displayClock")
    return Bout(
        fighter_a=_fighter(comps[0]),
        fighter_b=_fighter(comps[1]),
        weight_class=weight,
        segment=normalize_segment(seg_text) or "main",
        title_fight="title" in f"{weight or ''} {notes}".lower(),
        winner=winner if done else None,
        method=method,
        method_detail=detail,
        round=int(period) if done and str(period).isdigit() and int(period) > 0 else None,
        time=clock if done and clock and clock != "0:00" else None,
        start=_dt(c.get("date") or c.get("startDate")),
    )


def _parse_event(org_key: str, ev: dict) -> Event:
    comps = ev.get("competitions") or []
    indexed = [(i, b) for i, b in ((i, _parse_bout(c)) for i, c in enumerate(comps)) if b]
    # Ana maç en üstte: segment sırası, sonra en geç başlayan, sonra listedeki son sıra
    indexed.sort(key=lambda p: (SEGMENT_ORDER.get(p[1].segment, 9),
                                -(p[1].start.timestamp() if p[1].start else 0), -p[0]))
    bouts = [b for _, b in indexed]

    ev_start = _dt(ev.get("date"))
    starts = [b.start for b in bouts if b.start]
    main_starts = [b.start for b in bouts if b.start and b.segment == "main"]

    venue = None
    if comps:
        v = comps[0].get("venue") or {}
        addr = v.get("address") or {}
        venue = ", ".join(x for x in (v.get("fullName"), addr.get("city"), addr.get("country")) if x) or None

    stype = (ev.get("status") or {}).get("type") or {}
    completed = bool(stype.get("completed")) or stype.get("state") == "post" \
        or (bool(bouts) and all(b.finished for b in bouts))

    links = ev.get("links") or []
    return Event(
        id=f"espn:{ev.get('id')}",
        org_key=org_key,
        name=ev.get("name") or ev.get("shortName") or "Etkinlik",
        start=min(starts) if starts else ev_start,
        main_card_start=min(main_starts) if main_starts else ev_start,
        venue=venue,
        url=links[0].get("href") if links and isinstance(links[0], dict) else None,
        bouts=bouts,
        completed=completed,
        source="ESPN",
    )


async def fetch_events(league: str, org_key: str, start: date, end: date) -> list[Event]:
    async def load() -> list[Event]:
        resp = await http.get(
            SCOREBOARD.format(league=league),
            params={"dates": f"{start:%Y%m%d}-{end:%Y%m%d}", "limit": 100},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise ESPNError(f"ESPN {league} HTTP {resp.status_code}")
        events = resp.json().get("events") or []
        return [_parse_event(org_key, e) for e in events]

    return await _cache.get_or_set(f"{league}:{start}:{end}", 600, load)


async def next_event(league: str, org_key: str) -> Event | None:
    now = datetime.now(timezone.utc)
    events = await fetch_events(league, org_key, now.date() - timedelta(days=1),
                                now.date() + timedelta(days=60))
    upcoming = [e for e in events if not e.completed
                and (e.start is None or e.start > now - timedelta(hours=12))]
    upcoming.sort(key=lambda e: e.start or now + timedelta(days=999))
    return upcoming[0] if upcoming else None


async def _past(league: str, org_key: str) -> list[Event]:
    now = datetime.now(timezone.utc)
    events = await fetch_events(league, org_key, now.date() - timedelta(days=PAST_WINDOW_DAYS), now.date())
    past = [e for e in events if (e.completed or any(b.finished for b in e.bouts))
            and (e.start is None or e.start <= now)]
    past.sort(key=lambda e: e.start or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return past


async def past_events(league: str, org_key: str, limit: int = 5) -> list[EventRef]:
    return [EventRef(ref="e:" + e.id.split(":", 1)[1], name=e.name,
                     date=e.start.date() if e.start else None)
            for e in (await _past(league, org_key))[:limit]]


async def event_by_id(league: str, org_key: str, event_id: str) -> Event | None:
    return next((e for e in await _past(league, org_key) if e.id == f"espn:{event_id}"), None)