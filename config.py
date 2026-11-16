"""Uygulama ayarları (.env) ve organizasyon tanımları."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Org:
    key: str
    name: str
    emoji: str
    espn_league: str | None          # ESPN site API lig kodu (yoksa doğrudan Tapology)
    tapology_url: str | None         # Tapology promosyon sayfası (yoksa arama ile bulunur)
    tapology_search: str             # Tapology arama terimi (URL yoksa / 404 olursa)
    rankings: str | None = None      # "ufc" | "one" | None
    aliases: tuple[str, ...] = ()


def _org(key: str, name: str, emoji: str, espn: str | None, tapology: str | None,
         search: str, rankings: str | None = None, aliases: tuple[str, ...] = ()) -> Org:
    # Her organizasyonun Tapology adresi .env ile ezilebilir: TAPOLOGY_URL_CW=...
    return Org(
        key=key,
        name=name,
        emoji=emoji,
        espn_league=espn,
        tapology_url=os.getenv(f"TAPOLOGY_URL_{key.upper()}") or tapology,
        tapology_search=os.getenv(f"TAPOLOGY_SEARCH_{key.upper()}") or search,
        rankings=rankings,
        aliases=aliases,
    )


_T = "https://www.tapology.com/fightcenter/promotions/"

ORGS: dict[str, Org] = {o.key: o for o in [
    _org("ufc", "UFC", "🔴", "ufc", _T + "1-ultimate-fighting-championship-ufc",
         "UFC", rankings="ufc"),
    _org("pfl", "PFL", "🔵", "pfl", _T + "1969-professional-fighters-league-pfl",
         "Professional Fighters League", aliases=("professionalfightersleague",)),
    _org("one", "ONE Championship", "🟡", None, _T + "1025-one-championship-one",
         "ONE Championship", rankings="one", aliases=("onefc", "onechampionship")),
    # Tapology adresindeki "okatgon" yazımı Tapology'nin kendi slug'ıdır (ID: 1964)
    _org("oktagon", "Oktagon MMA", "⚪", None, _T + "1964-okatgon-mma-okmma",
         "Oktagon MMA", aliases=("okmma", "oktagonmma", "okt")),
    _org("cw", "Cage Warriors", "🟠", None, None, "Cage Warriors",
         aliases=("cagewarriors", "cwfc")),
    _org("corefc", "CORE FC", "🟣", None, None, "CORE FC", aliases=("core",)),
    _org("arena", "Arena Fight Night", "🟢", None, None, "Arena Fight Night",
         aliases=("afn", "arenafightnight")),
]}


def resolve_org(text: str | None) -> Org | None:
    if not text:
        return None
    t = "".join(ch for ch in text.lower() if ch.isalnum())
    for org in ORGS.values():
        if t == org.key or t in org.aliases:
            return org
    return None


@dataclass(frozen=True)
class Settings:
    token: str
    db_path: str
    tz: ZoneInfo
    tapology_tz: ZoneInfo
    alert_chat_ids: tuple[int, ...]
    news_interval_min: int
    reminder_check_min: int
    reminder_lead_min: int
    alert_max_age_hours: int
    feeds: tuple[tuple[str, str, str], ...]            # (spor, ad, adres)
    log_level: str
    radio_time: str = "08:00"                 # sabah sesli bülten saati (TIMEZONE'a göre); "" = kapalı
    radio_model: str = "gemma3:4b"
    ollama_url: str = "http://127.0.0.1:11434"
    radio_voice: str = "data/voices/tr_TR-dfki-medium.onnx"          # Piper sesi
    radio_tts: tuple[str, ...] = ("gemini", "edge", "piper")          # denenecek ses motorları, sırayla
    gemini_api_key: str = ""
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    gemini_voice: str = "Puck"
    edge_voice: str = "en-US-AndrewMultilingualNeural"
    gemini_text_model: str = "gemini-flash-lite-latest"   # haber çevirisi/özeti
    news_mode: str = "all"                                 # "all" = tüm haberler, "important" = sadece önemliler
    f1_reminder_lead_min: int = 30
    popular_min_score: int = 8                             # "important" modunda bu puan ve üstü popüler haber; 0 = kapalı
    sports: tuple[str, ...] = ("mma", "f1")               # bu botun kapsadığı sporlar (SPORTS=mma / f1)

    @property
    def tz_label(self) -> str:
        return "TSİ" if self.tz.key == "Europe/Istanbul" else self.tz.key


SPORTS: dict[str, tuple[str, str]] = {"mma": ("🥊", "MMA"), "f1": ("🏎️", "F1")}

# (spor, kaynak adı, RSS adresi)
DEFAULT_FEEDS = (
    ("mma", "MMA Fighting", "https://www.mmafighting.com/rss/index.xml"),
    # MMA Junkie RSS'i kapandı (archive.mmajunkie.com artık çözülmüyor); yerine Bloody Elbow
    ("mma", "Bloody Elbow", "https://www.bloodyelbow.com/feed"),
    ("mma", "Sherdog", "https://www.sherdog.com/rss/news.xml"),
    # Formula1.com'un sayfaları sunucudan okunamıyor (JS); tam metin verenler seçildi
    ("f1", "Motorsport.com", "https://www.motorsport.com/rss/f1/news/"),
    ("f1", "The Race", "https://www.the-race.com/feed/"),
    ("f1", "Autosport", "https://www.autosport.com/rss/f1/news/"),
)


def resolve_sport(text: str | None) -> str | None:
    t = "".join(ch for ch in (text or "").lower() if ch.isalnum())
    if t in ("mma", "ufc", "dovus", "dövüş"):
        return "mma"
    if t in ("f1", "formula1", "formula", "formul1"):
        return "f1"
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    token = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN .env dosyasında tanımlı değil.")

    # Aynı kod iki ayrı bot olarak çalışır: SPORTS=mma (MMA botu) / SPORTS=f1 (F1 botu)
    sports = tuple(sp for sp in (x.lower() for x in _env_list("SPORTS")) if sp in SPORTS) or tuple(SPORTS)
    feeds = [f for f in DEFAULT_FEEDS if f[0] in sports]
    for sport, env in (("mma", "EXTRA_FEEDS"), ("f1", "EXTRA_F1_FEEDS")):
        for item in _env_list(env):                # "Ad|https://...,Ad2|https://..."
            name, _, url = item.partition("|")
            if url and sport in sports:
                feeds.append((sport, name.strip(), url.strip()))

    return Settings(
        token=token,
        db_path=os.getenv("DB_PATH", "data/mma_bot.db"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul")),
        tapology_tz=ZoneInfo(os.getenv("TAPOLOGY_TZ", "America/New_York")),
        alert_chat_ids=tuple(int(x) for x in _env_list("ALERT_CHAT_IDS")),
        news_interval_min=_env_int("NEWS_INTERVAL_MIN", 10),
        reminder_check_min=_env_int("REMINDER_CHECK_MIN", 5),
        reminder_lead_min=_env_int("REMINDER_LEAD_MIN", 60),
        alert_max_age_hours=_env_int("ALERT_MAX_AGE_HOURS", 6),
        feeds=tuple(feeds),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        radio_time=os.getenv("RADIO_TIME", "08:00").strip(),
        radio_model=os.getenv("RADIO_MODEL", "gemma3:4b").strip(),
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        radio_voice=os.getenv("RADIO_VOICE", "data/voices/tr_TR-dfki-medium.onnx"),
        radio_tts=tuple(_env_list("RADIO_TTS")) or ("gemini", "edge", "piper"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip().strip("\"'"),
        gemini_tts_model=os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip(),
        gemini_voice=os.getenv("GEMINI_VOICE", "Puck").strip(),
        edge_voice=os.getenv("EDGE_VOICE", "en-US-AndrewMultilingualNeural").strip(),
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL", "gemini-flash-lite-latest").strip(),
        news_mode="important" if os.getenv("NEWS_MODE", "all").strip().lower() == "important" else "all",
        f1_reminder_lead_min=_env_int("F1_REMINDER_LEAD_MIN", 30),
        popular_min_score=_env_int("POPULAR_MIN_SCORE", 8),
        sports=sports,
    )