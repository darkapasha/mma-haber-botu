"""APScheduler arka plan görevleri: haber bildirimleri, MMA/F1 hatırlatmaları, sabah radyosu."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application

import formatters as fmt
from config import ORGS, Settings
from scrapers import tapology
from services import events, f1, llm, news, radio
from services.storage import Storage

log = logging.getLogger(__name__)

MAX_NEWS_PER_RUN = 12        # bir turda en fazla bu kadar haber; kalanlar sonraki tura kalır


async def broadcast(app: Application, text: str, sport: str) -> int:
    """`sport` abonelerine ve ALERT_CHAT_IDS'e metin gönderir; gönderilen sohbet sayısını döndürür."""
    async def send(chat_id: int) -> None:
        await app.bot.send_message(chat_id, text)
    return await _deliver(app, send, sport)


async def broadcast_voice(app: Application, b: radio.Bulletin, caption: str) -> int:
    """Sesli bülteni (herhangi bir spora abone olan herkese) bir kez yükler, sonra file_id ile gönderir."""
    file_id: str | None = None

    async def send(chat_id: int) -> None:
        nonlocal file_id
        msg = await app.bot.send_voice(chat_id, file_id or b.path.read_bytes(),
                                       duration=b.duration, caption=caption)
        if msg.voice:
            file_id = msg.voice.file_id
    return await _deliver(app, send, None)


async def _deliver(app: Application, send, sport: str | None) -> int:
    storage: Storage = app.bot_data["storage"]
    settings: Settings = app.bot_data["settings"]
    targets = set(await storage.subscribers(sport)) | set(settings.alert_chat_ids)
    sent = 0
    for chat_id in targets:
        for attempt in range(2):
            try:
                await send(chat_id)
                sent += 1
                break
            except RetryAfter as exc:
                ra = exc.retry_after              # PTB sürümüne göre int veya timedelta
                await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 1)
            except Forbidden:
                log.info("Sohbet botu engelledi, abonelikten çıkarılıyor: %s", chat_id)
                await storage.remove_subscriber(chat_id)
                break
            except BadRequest as exc:
                if "chat not found" in str(exc).lower():
                    await storage.remove_subscriber(chat_id)
                else:
                    log.warning("Gönderilemedi (%s): %s", chat_id, exc)
                break
            except TelegramError as exc:
                log.warning("Gönderilemedi (%s): %s", chat_id, exc)
                break
        await asyncio.sleep(0.05)       # Telegram hız sınırı (~30 mesaj/sn)
    return sent


async def news_alert_job(app: Application) -> None:
    storage: Storage = app.bot_data["storage"]
    settings: Settings = app.bot_data["settings"]
    items = await news.fetch_all(use_cache=False)
    if not items:
        return

    # Yeni eklenen bir kaynağın ilk görüşünde eski haberleri bildirmeden "görüldü" say (spam önleme)
    by_source: dict[tuple[str, str], list] = {}
    for i in items:
        by_source.setdefault((i.sport, i.source), []).append(i)
    for (sport, source), group in by_source.items():
        key = f"feed_boot:{sport}:{source}"
        if await storage.get_kv(key) is None:
            await storage.mark_news([(i.id, news.title_key(i.title)) for i in group])
            await storage.set_kv(key, datetime.now(timezone.utc).isoformat())
            log.info("Yeni haber kaynağı (%s / %s): %d eski haber atlandı", sport, source, len(group))

    # Görülmemiş ve yeterince yeni haberler, eskiden yeniye
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.alert_max_age_hours)
    fresh = []
    for item in reversed(items):
        tkey = news.title_key(item.title)
        if await storage.news_seen(item.id, tkey):
            continue
        if item.published and item.published < cutoff:
            await storage.mark_news([(item.id, tkey)])
            continue
        if not await storage.subscribers(item.sport) and not settings.alert_chat_ids:
            await storage.mark_news([(item.id, tkey)])   # kimse abone değil: boşuna çeviri yapma
            continue
        fresh.append((item, tkey))

    ratings = await _rate(storage, settings, [i for i, _ in fresh])
    sent = 0
    for item, tkey in fresh:
        if sent >= MAX_NEWS_PER_RUN:
            break                                        # kalanlar görülmemiş kalır, sonraki tura
        await storage.mark_news([(item.id, tkey)])
        rule = news.classify(item)
        score, repeat = ratings.get(item.id, (0, False))
        popular = settings.popular_min_score > 0 and score >= settings.popular_min_score
        if repeat:
            log.info("Tekrar haber atlandı [%s]: %s", item.sport, item.title)
            continue
        if settings.news_mode == "important" and not rule and not popular:
            continue
        # Veritabanında atomik kilit: aynı haber (farklı kaynak/ID) iki kez gitmez
        if not await storage.claim(f"news:{tkey}"):
            continue
        tr = await llm.translate_article(settings, item, await news.full_text(item))
        log.info("Haber [%s%s%s]: %s", item.sport, f"/{rule.key}" if rule else "",
                 f"/popüler {score}" if popular else "", item.title)
        await broadcast(app, fmt.format_alert(item, tr, rule, settings, popular=popular), item.sport)
        await _remember_sent(storage, item)
        sent += 1


SENT_MEMORY = timedelta(hours=48)       # tekrar kontrolü için hatırlanan gönderilmiş başlıklar
SENT_MEMORY_MAX = 40


async def _sent_rows(storage: Storage, sport: str) -> list[list[str]]:
    try:
        rows = json.loads(await storage.get_kv(f"sent_titles:{sport}") or "[]")
    except ValueError:
        return []
    cutoff = datetime.now(timezone.utc) - SENT_MEMORY
    return [r for r in rows if datetime.fromisoformat(r[0]) > cutoff][-SENT_MEMORY_MAX:]


async def _remember_sent(storage: Storage, item) -> None:
    rows = await _sent_rows(storage, item.sport)
    rows.append([datetime.now(timezone.utc).isoformat(), item.title])
    await storage.set_kv(f"sent_titles:{item.sport}", json.dumps(rows[-SENT_MEMORY_MAX:], ensure_ascii=False))


async def _rate(storage: Storage, settings: Settings, items: list) -> dict[str, tuple[int, bool]]:
    """Yalnızca "important" modunda: haber kimliği -> (popülerlik puanı, tekrar mı).
    Model cevap vermezse boş sözlük: popüler haber yok, önemli haberler tekrar kontrolsüz gider."""
    if settings.news_mode != "important" or not items:
        return {}
    out: dict[str, tuple[int, bool]] = {}
    for sport in {i.sport for i in items}:
        group = [i for i in items if i.sport == sport]
        sent_titles = [r[1] for r in await _sent_rows(storage, sport)]
        ratings = await llm.rate_popularity(settings, sport, group, sent_titles)
        if ratings is not None:
            out.update({i.id: r for i, r in zip(group, ratings)})
    return out


async def reminder_job(app: Application) -> None:
    storage: Storage = app.bot_data["storage"]
    settings: Settings = app.bot_data["settings"]
    now = datetime.now(timezone.utc)
    lead = timedelta(minutes=settings.reminder_lead_min)
    for org in ORGS.values():
        try:
            ev = await events.next_event(org)
        except tapology.Blocked as exc:
            # Engel başladığında tapology modülü bir kez uyarır; her 5 dakikada log'u doldurma
            log.debug("Hatırlatma atlandı (%s): %s", org.key, exc)
            continue
        except Exception as exc:
            log.warning("Hatırlatma kontrolü başarısız (%s): %s", org.key, exc)
            continue
        if not ev or not ev.time_known:
            continue
        start = ev.main_card_start or ev.start
        if not start or not (timedelta(0) < start - now <= lead):
            continue
        key = f"reminder:{ev.id}:{start:%Y%m%d%H%M}"
        if await storage.claim(key):
            log.info("Hatırlatma gönderiliyor: %s", ev.name)
            await broadcast(app, fmt.format_reminder(org, ev, settings), "mma")


async def f1_reminder_job(app: Application) -> None:
    storage: Storage = app.bot_data["storage"]
    settings: Settings = app.bot_data["settings"]
    try:
        race = await f1.next_race()
    except Exception as exc:
        log.warning("F1 takvimi alınamadı: %s", exc)
        return
    if race is None:
        return
    now = datetime.now(timezone.utc)
    lead = timedelta(minutes=settings.f1_reminder_lead_min)
    for session in race.sessions:
        if not session.remind or not (timedelta(0) < session.start - now <= lead):
            continue
        if await storage.claim(f"f1:{race.season}:{race.round}:{session.key}"):
            log.info("F1 hatırlatma: %s %s", race.name, session.name)
            await broadcast(app, fmt.format_f1_reminder(race, session, settings), "f1")


async def radio_job(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    b = await radio.get(settings, max_age=timedelta(minutes=30))     # sabah her zaman taze bülten
    sent = await broadcast_voice(app, b, fmt.format_radio_caption(b, settings))
    log.info("Radyo bülteni %d sohbete gönderildi", sent)


async def prune_job(app: Application) -> None:
    await app.bot_data["storage"].prune(days=30)


def _safe(fn):
    async def wrapper(app: Application) -> None:
        try:
            await fn(app)
        except Exception:
            log.exception("Görev hatası: %s", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def schedule(scheduler, app: Application, settings: Settings) -> None:
    soon = datetime.now(timezone.utc) + timedelta(seconds=20)
    common = {"args": [app], "max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
    scheduler.add_job(_safe(news_alert_job), "interval", minutes=settings.news_interval_min,
                      next_run_time=soon, id="news", **common)
    if "mma" in settings.sports:
        scheduler.add_job(_safe(reminder_job), "interval", minutes=settings.reminder_check_min,
                          next_run_time=soon + timedelta(seconds=40), id="reminder", **common)
    if "f1" in settings.sports:
        scheduler.add_job(_safe(f1_reminder_job), "interval", minutes=settings.reminder_check_min,
                          next_run_time=soon + timedelta(seconds=60), id="f1_reminder", **common)
    scheduler.add_job(_safe(prune_job), "cron", hour=4, minute=0, id="prune", **common)
    if settings.radio_time:
        hour, _, minute = settings.radio_time.partition(":")
        # Bülten üretimi birkaç dakika sürer; ileri kaçan çalışmaya 30 dk tolerans
        scheduler.add_job(_safe(radio_job), "cron", hour=int(hour), minute=int(minute or 0),
                          timezone=settings.tz, id="radio",
                          **{**common, "misfire_grace_time": 1800})
