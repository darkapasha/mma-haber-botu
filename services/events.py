"""Kaynak seçici: büyük organizasyonlarda önce ESPN, olmazsa Tapology."""
from __future__ import annotations

import logging

from config import Org
from models import Event, EventRef
from scrapers import tapology
from services import api

log = logging.getLogger(__name__)


async def next_event(org: Org) -> Event | None:
    espn_event: Event | None = None
    if org.espn_league:
        try:
            espn_event = await api.next_event(org.espn_league, org.key)
            if espn_event and espn_event.bouts:
                return espn_event
        except Exception as exc:  # ağ/JSON/erişim hatası -> Tapology'ye düş
            log.warning("ESPN %s başarısız, Tapology'ye geçiliyor: %s", org.key, exc)
    try:
        return await tapology.next_event(org) or espn_event
    except Exception:
        if espn_event:
            return espn_event
        raise


async def past_events(org: Org, limit: int = 5) -> list[EventRef]:
    """Son `limit` geçmiş etkinlik. Her kaydın `ref` alanı kaynağını da taşır (e: ESPN, t: Tapology)."""
    if org.espn_league:
        try:
            refs = await api.past_events(org.espn_league, org.key, limit)
            if refs:
                return refs
        except Exception as exc:
            log.warning("ESPN %s geçmiş etkinlikleri alınamadı, Tapology'ye geçiliyor: %s", org.key, exc)
    return await tapology.past_events(org, limit)


async def event_by_ref(org: Org, ref: str) -> Event | None:
    kind, _, value = ref.partition(":")
    if not value:
        return None
    if kind == "e" and org.espn_league:
        return await api.event_by_id(org.espn_league, org.key, value)
    if kind == "t" and value.isdigit():
        return await tapology.event_by_id(org, value)
    return None