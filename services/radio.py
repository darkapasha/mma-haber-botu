"""MMA Radyosu: günün haberlerinden Ollama ile Türkçe bülten yazar ve seslendirir.

Akış: haber + sonuç + yaklaşan maç verisi -> Ollama (düz metin) -> ses motoru -> ffmpeg
(OGG/Opus, Telegram sesli mesaj biçimi). Ses motorları RADIO_TTS sırasıyla denenir:
gemini (Google Gemini TTS, en doğal) -> edge (Microsoft Edge sesleri) -> piper (yerel, her zaman çalışır).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import formatters as fmt
from config import ORGS, Settings
from services import api, events, f1, news

log = logging.getLogger(__name__)

OUT_DIR = Path("data/radio")
OGG = OUT_DIR / "latest.ogg"
META = OUT_DIR / "latest.json"
MAX_NEWS = {"mma": 6, "f1": 5}
RESULTS_WINDOW = timedelta(days=3)      # bu kadar yakın tarihli etkinliklerin sonuçları okunur
UPCOMING_WINDOW = timedelta(days=8)     # bu kadar yakın etkinlikler "yaklaşan" olarak okunur

def _system_prompt(sports: tuple[str, ...]) -> str:
    name = fmt.radio_name(sports)
    both = len(sports) > 1
    if both:
        structure = ("- İki bölüm olsun: önce MMA, sonra kısa bir geçiş cümlesiyle Formula 1. "
                     "Malzemede olmayan bölümü atla.\n"
                     "- Her bölümde sıra: varsa son sonuçlar, sonra en önemli haberler, en son yaklaşan "
                     "maç ya da yarış.\n"
                     "- Benzer haberleri birleştir, önemsizleri atla. MMA'da en fazla 4, F1'de en fazla 3 "
                     "konu anlat.\n- Toplam 200 ile 320 kelime arası olsun.")
    else:
        structure = ("- Sıra: varsa son sonuçlar, sonra en önemli haberler, en son yaklaşan "
                     + ("maçlar." if sports == ("mma",) else "yarış haftası ve seans saatleri.") + "\n"
                     "- Benzer haberleri birleştir, önemsizleri atla. En fazla 5 konu anlat.\n"
                     "- Toplam 150 ile 250 kelime arası olsun.")
    return f"""Sen "{name}"nun Türk spikerisin. Sana verilen İngilizce haberlerden ve \
maç/yarış bilgilerinden, sabah dinlenecek kısa bir Türkçe radyo bülteni yazıyorsun.

Kurallar:
- Sadece verilen bilgileri kullan, hiçbir şey uydurma. Her haberi kendi başlığındaki bilgiyle anlat, \
haberleri birbirine karıştırma. Emin olmadığın ayrıntıyı hiç yazma.
- Akıcı, enerjik ama doğal bir Türkçe kullan. Konuşma dili gibi, kısa cümleler kur. \
İngilizce kelime kullanma (coach değil antrenör, runner değil asistan gibi).
- Bu metin seslendirilecek: yalnızca düz paragraflar yaz. Başlık, madde işareti, tire, yıldız, \
emoji, parantez, tırnak KESİNLİKLE kullanma.
- Dövüşçü, pilot, takım ve organizasyon isimlerini olduğu gibi bırak, çevirme.
{structure}
- Sonuçlarda her galibiyeti ya da sırayı tek tek sayma; en dikkat çekici bir iki tanesini söyle. Maçın nasıl bittiğini (nakavt, hakem kararı, pes ettirme) yalnızca malzemede yazdığı gibi söyle; "bitiriş bilinmiyor" yazıyorsa nasıl bittiğinden hiç bahsetme. Kazananla kaybedeni karıştırma, isimleri malzemedeki gibi tam ve doğru eşleştir (bir dövüşçünün adını başka birinin soyadıyla birleştirme).
- Haberlerde başlıkta ne yazıyorsa onu söyle; yorum ya da tahmin ekleme.
- "Günaydın, {name}'na hoş geldiniz" diye başla, tek cümlelik kısa bir kapanışla bitir."""


def _reminder(sports: tuple[str, ...]) -> str:
    # Küçük modeller en son okuduklarına daha çok uyar; kurallar mesaj sonunda kısaca tekrarlanır
    limits = ("önce MMA sonra F1, MMA'da en fazla 4 ve F1'de en fazla 3 konu, 200-320 kelime"
              if len(sports) > 1 else "en fazla 5 konu, 150-250 kelime")
    return ("Şimdi bu malzemeden bülteni yaz. Unutma: yalnızca düz paragraflar, madde işareti ve "
            f"emoji yok, sadece malzemedeki bilgiler, {limits}.")

# Tüm ses motorları için metin düzeltmeleri (sıra önemli: TKO, KO'dan önce)
_TEXT_FIX = [
    (re.compile(r"\bTKO\b"), "teknik nakavt"),
    (re.compile(r"\bKO\b"), "nakavt"),
    (re.compile(r"\bvs\b\.?", re.I), "ile"),
    (re.compile(r"\bsaat 00[:.]00(?:'?[dt][ae])?"), "gece yarısı"),
    (re.compile(r"\b00[:.]00\b"), "gece yarısı"),
    (re.compile(r"\bmain event\b", re.I), "ana maç"),
]
# Yalnızca Piper: kısaltmaları kendisi bilmediği için harf harf yazılır
_PIPER_ACRONYMS = [
    (re.compile(r"\bUFC\b"), "Yu Ef Si"),
    (re.compile(r"\bPFL\b"), "Pi Ef El"),
    (re.compile(r"\bMMA\b"), "Em Em A"),
    (re.compile(r"\bF1\b"), "Formula Bir"),
]

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Talimat ile metin arasındaki ayrım net olmalı; bazı sesler (ör. Charon) yoksa talimatı da okuyor
GEMINI_STYLE = (
    "[SESLENDİRME TALİMATI — BU KISMI KESİNLİKLE SESLİ OKUMA. Sadece METİN: satırından sonrasını oku.]\n"
    "Enerjik bir Türk spor radyosu spikeri gibi seslendir. Doğal Türkçe vurgu ve tonlama kullan. "
    "Yabancı dövüşçü ve pilot isimlerini ve İngilizce terimleri (Contender Series, Fight Night, "
    "Hall of Famer, Grand Prix, pole position, pit stop gibi) bir Türk spor spikerinin yaptığı gibi "
    "orijinal telaffuzlarıyla söyle; Türkçe ekleri Türkçe oku. UFC'yi 'yu-ef-si', MMA'yı 'em-em-a', "
    "F1'i 'ef bir' diye oku.\n\n"
    "METİN:\n")
WORDS_PER_SEC = 2.4         # Türkçe haber okuma hızı; ses süresinin makul olup olmadığını ölçmek için

# --- Yabancı isimlerin İngilizce telaffuzu ------------------------------------------
# Türkçe ses, "Christian"ı Türkçe kurallarla "Cehristian" diye okur. Bunun yerine kaynak
# (İngilizce) metindeki büyük harfli kelimeler eSpeak'in İngilizce sesiyle fonemlere çevrilir,
# fonemler Türkçe sesin eğitimde gördüğü seslere eşlenir ve Piper'a [[ ]] bloğu olarak verilir.
# Sonuç, isimleri İngilizce telaffuzla okuyan bir Türk spiker gibidir.
_TR_PHONEMES = set("abdefhijklmnoprstuvyzæøœɔɛɟɡɪɫɯɾʃʊʒˈˌː")   # Türkçe sesin bildiği fonemler
_EN_TO_TR_SEQ = [("eɪ", "ej"), ("aɪ", "aj"), ("ɔɪ", "ɔj"), ("oʊ", "o"), ("aʊ", "av"),
                 ("ɚ", "ɯɾ"), ("ɝ", "øɾ")]
# Aynı anda uygulanır: Amerikan "yumuşak t"si (ɾ, Pettis -> "Peris") önce t olur, ɹ sonra ɾ olur
_EN_TO_TR_CHAR = str.maketrans({"ɾ": "t", "ɹ": "ɾ", "ɐ": "a", "ʌ": "a", "ɑ": "a", "ɒ": "o", "æ": "e",
                                "ɜ": "ø", "ᵻ": "ɪ", "ŋ": "n", "θ": "t", "ð": "d", "w": "v",
                                "x": "h", "ɬ": "l", "ə": "ɯ"})
_NAME_IN_SOURCE = re.compile(r"\b[A-Z][^\W\d_]+")
_NAME_IN_SCRIPT = re.compile(r"\b([A-Z][^\W\d_]+)(['’][^\W\d_]+)?")
_espeak = None


def english_terms(material: str) -> set[str]:
    """Kaynak metindeki büyük harfle başlayan yabancı kelimeler (isimler, etkinlik adları)."""
    # malzemeye bizim eklediğimiz Türkçe kelimeler (ay/gün adları, F1 seans adları...)
    ours = (set(fmt.TR_MONTHS) | set(fmt.TR_DAYS) | {"Kazanan", "Türkiye"}
            | {w for name, _ in f1.SESSIONS.values() for w in name.split()})
    return {w for w in _NAME_IN_SOURCE.findall(material)
            if len(w) >= 3 and not w.isupper() and w not in ours}


def _phonemes(voice: str, text: str) -> str:
    global _espeak
    if _espeak is None:
        from piper.phonemize_espeak import EspeakPhonemizer
        _espeak = EspeakPhonemizer()
    return "".join(p for sentence in _espeak.phonemize(voice, text) for p in sentence)


def _en_to_tr(word: str) -> str:
    if word == "The":
        return "dɯ"
    ph = _phonemes("en-us", word).strip(" .,;:!?")
    ph = re.sub(r"ə$", "a", ph)                     # sondaki ə: Dana -> "Deyna", Silva -> "Silva"
    ph = ph.translate(_EN_TO_TR_CHAR)               # önce tek sesler (ɚ'nin ürettiği ɾ t'ye dönmesin)
    for a, b in _EN_TO_TR_SEQ:
        ph = ph.replace(a, b)
    return "".join(c for c in ph if c in _TR_PHONEMES)


def with_english_names(text: str, terms: set[str]) -> str:
    """Metindeki yabancı isimleri [[fonem]] bloklarıyla değiştirir; Türkçe ekleri korur."""
    cache: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        word, suffix = m[1], m[2]
        if word not in terms:
            return m[0]
        if word not in cache:
            cache[word] = _en_to_tr(word)
        ph = cache[word]
        if suffix:                                   # Duncan'ın -> İngilizce isim + Türkçe ek
            ph += "".join(c for c in _phonemes("tr", suffix[1:]) if c in _TR_PHONEMES - {"ˈ", "ˌ"})
        return f"[[{ph}]]" if ph else m[0]

    return _NAME_IN_SCRIPT.sub(repl, text)
_NON_SPEECH = re.compile(r"[*#_`~>|\[\]{}()\"“”«»•]|[\U0001F000-\U0001FAFF☀-➿]")


class RadioError(Exception):
    pass


@dataclass
class Bulletin:
    path: Path
    duration: int          # saniye
    created: datetime      # UTC
    script: str


_lock = asyncio.Lock()
_voice = None              # PiperVoice; ilk kullanımda yüklenir (~65 MB model)


def tr_date(dt: datetime) -> str:
    return f"{dt.day} {fmt.TR_MONTHS[dt.month - 1]} {fmt.TR_DAYS[dt.weekday()]}"


# ---------------------------------------------------------------------------
# Malzeme toplama
# ---------------------------------------------------------------------------
def _method_tr(method: str | None, detail: str | None) -> str:
    """ESPN bitiriş yöntemini malzeme için Türkçeye çevirir; boşsa açıkça 'bilinmiyor' der."""
    m = f"{method or ''} {detail or ''}".lower()
    if not m.strip():
        return "bitiriş bilinmiyor"
    if "draw" in m:
        return "berabere"
    if "decision" in m:
        kind = ("oybirliğiyle" if "unanimous" in m else "bölünmüş" if "split" in m
                else "çoğunluk" if "majority" in m else "")
        return ("hakem kararı " + kind).strip()
    if "sub" in m:
        return "pes ettirme"
    if "ko" in m:
        return "teknik nakavt" if "tko" in m and "ko/tko" not in m else "nakavt"
    if "dq" in m or "disqual" in m:
        return "diskalifiye"
    if "no contest" in m:
        return "sonuçsuz"
    return method or "bitiriş bilinmiyor"


async def _results(now: datetime) -> list[str]:
    lines: list[str] = []
    for org in ORGS.values():
        if not org.espn_league:            # Tapology'ye bağlı organizasyonlar atlanır (engelli)
            continue
        try:
            evs = await api.fetch_events(org.espn_league, org.key,
                                         (now - RESULTS_WINDOW).date(), now.date())
        except Exception as exc:
            log.warning("Radyo: %s sonuçları alınamadı: %s", org.key, exc)
            continue
        for ev in evs:
            main = [b for b in ev.bouts if b.segment == "main" and b.winner is not None][:5]
            if not main:
                continue
            lines.append(f"SON SONUÇLAR | {org.name} | {ev.name}")
            for b in main:
                w, l = (b.fighter_a, b.fighter_b) if b.winner == 0 else (b.fighter_b, b.fighter_a)
                how = _method_tr(b.method, b.method_detail)
                if how.startswith("hakem kararı") and b.round:
                    when = f"{b.round} raundun sonunda"
                else:
                    when = f"{b.round}. rauntta" if b.round else ""
                extra = [x for x in (how, when, "kemer maçı" if b.title_fight else "") if x]
                lines.append(f"- Kazanan {w.name}, rakibi {l.name}"
                             + (f" ({', '.join(extra)})" if extra else ""))
    return lines


async def _upcoming(now: datetime, s: Settings) -> list[str]:
    lines: list[str] = []
    for org in ORGS.values():
        if not org.espn_league:
            continue
        try:
            ev = await events.next_event(org)
        except Exception as exc:
            log.warning("Radyo: %s yaklaşan etkinlik alınamadı: %s", org.key, exc)
            continue
        start = ev and (ev.main_card_start or ev.start)
        if not start or not (now < start <= now + UPCOMING_WINDOW):
            continue
        local = start.astimezone(s.tz)
        when = tr_date(local) + (f", Türkiye saatiyle {local:%H:%M}" if ev.time_known else "")
        lines.append(f"YAKLAŞAN | {org.name} | {ev.name} | {when}")
        for b in [b for b in ev.bouts if b.segment == "main"][:3]:
            title = " (kemer maçı)" if b.title_fight else ""
            lines.append(f"- {b.fighter_a.name} - {b.fighter_b.name}{title}")
    return lines


async def _news(sport: str) -> list[str]:
    items = await news.recent(hours=24, limit=MAX_NEWS[sport], sport=sport)
    out = []
    for i in items:
        summary = i.summary[:200].rsplit(" ", 1)[0] if len(i.summary) > 200 else i.summary
        out.append(f"HABER | {i.title}" + (f" | {summary}" if summary else ""))
    return out


async def _f1_result(now: datetime) -> list[str]:
    res = await f1.last_result()
    if not res or not res.rows or not res.date:
        return []
    if now.date() - datetime.fromisoformat(res.date).date() > RESULTS_WINDOW:
        return []
    lines = [f"SON YARIŞ SONUCU | {res.race}"]
    lines += [f"- {r.position}. {r.driver}, {r.team}" for r in res.rows[:5]]
    return lines


async def _f1_upcoming(now: datetime, s: Settings) -> list[str]:
    race = await f1.next_race()
    start = race and race.race_start
    if not start or not (now < start <= now + UPCOMING_WINDOW):
        return []
    lines = [f"YAKLAŞAN YARIŞ | {race.name} | {race.locality}, {race.country}"
             + (" | sprint haftası" if race.is_sprint else "")]
    for x in race.sessions:
        if x.remind and x.start > now:
            local = x.start.astimezone(s.tz)
            lines.append(f"- {x.name}: {tr_date(local)}, Türkiye saatiyle {local:%H:%M}")
    return lines


async def _none() -> list[str]:
    return []


async def gather(s: Settings) -> str:
    now = datetime.now(timezone.utc)
    mma, f1_on = "mma" in s.sports, "f1" in s.sports
    blocks = await asyncio.gather(
        _results(now) if mma else _none(), _news("mma") if mma else _none(),
        _upcoming(now, s) if mma else _none(),
        _f1_result(now) if f1_on else _none(), _news("f1") if f1_on else _none(),
        _f1_upcoming(now, s) if f1_on else _none(), return_exceptions=True)

    def section(title: str, parts) -> str:
        lines = []
        for block in parts:
            if isinstance(block, Exception):
                log.warning("Radyo: malzeme toplanamadı: %s", block)
            elif block:
                lines.append("\n".join(block))
        return f"=== {title} ===\n" + "\n\n".join(lines) if lines else ""

    return "\n\n".join(x for x in (section("MMA", blocks[:3]), section("FORMULA 1", blocks[3:])) if x)


# ---------------------------------------------------------------------------
# Metin ve ses
# ---------------------------------------------------------------------------
SCRIPT_GEMINI_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest")


async def _script_gemini(s: Settings, system: str, user: str) -> str:
    last = None
    for model in SCRIPT_GEMINI_MODELS:
        gen = {"temperature": 0.4, "maxOutputTokens": 2000}
        if "lite" not in model:
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                    resp = await client.post(GEMINI_URL.format(model=model),
                                             headers={"x-goog-api-key": s.gemini_api_key}, json=body)
                if resp.status_code != 200:
                    raise RadioError(f"Gemini {model} HTTP {resp.status_code}: {resp.text[:150]}")
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if len(text) < 100:
                    raise RadioError(f"Gemini {model} çok kısa metin döndürdü")
                log.info("Radyo metni Gemini %s ile yazıldı", model)
                return text
            except Exception as exc:
                last = exc
                log.warning("Radyo: Gemini metin %s (deneme %d): %s", model, attempt + 1, exc)
                await asyncio.sleep(5)
    raise RadioError(f"Gemini metin yazamadı: {last}")


async def write_script(s: Settings, material: str) -> str:
    today = tr_date(datetime.now(s.tz))
    if s.gemini_api_key:
        try:
            return await _script_gemini(s, _system_prompt(s.sports),
                                        f"Bugün: {today}\n\nMALZEME:\n{material}\n\n{_reminder(s.sports)}")
        except Exception as exc:
            log.warning("Radyo: Gemini olmadı, Ollama ile yazılıyor: %s", exc)
    payload = {
        "model": s.radio_model,
        "stream": False,
        "keep_alive": "2m",
        "options": {"temperature": 0.35, "num_ctx": 8192, "num_predict": 1200},
        "messages": [
            {"role": "system", "content": _system_prompt(s.sports)},
            {"role": "user", "content": f"Bugün: {today}\n\nMALZEME:\n{material}\n\n{_reminder(s.sports)}"},
        ],
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(1200, connect=10)) as client:
        resp = await client.post(f"{s.ollama_url}/api/chat", json=payload)
    if resp.status_code != 200:
        raise RadioError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
    text = (resp.json().get("message") or {}).get("content", "").strip()
    if len(text) < 100:
        raise RadioError("Ollama boş ya da çok kısa bir metin döndürdü")
    return text


def for_speech(text: str) -> str:
    """Piper için: kısaltmalar harf harf + genel temizlik."""
    for rx, repl in _PIPER_ACRONYMS:
        text = rx.sub(repl, text)
    return clean_text(text)


def clean_text(text: str) -> str:
    for rx, repl in _TEXT_FIX:
        text = rx.sub(repl, text)
    # Model yine de liste yazarsa: madde işaretlerini ve "Haberler:" gibi başlık satırlarını at
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text)
    text = _NON_SPEECH.sub(" ", text)
    text = re.sub(r"(?m)^\s*[^.!?\n]{1,30}:\s*$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _synthesize(text: str, terms: set[str], voice_path: str, wav_path: Path) -> float:
    global _voice
    from piper import PiperVoice          # ağır import; yalnızca radyo kullanılınca yüklenir
    if _voice is None:
        _voice = PiperVoice.load(voice_path)
    text = with_english_names(text, terms)   # eSpeak global durumlu: sentezle aynı thread'de
    with wave.open(str(wav_path), "wb") as wf:
        _voice.synthesize_wav(text, wf)
    with wave.open(str(wav_path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


async def _tts_gemini(s: Settings, text: str, dst: Path) -> None:
    if not s.gemini_api_key:
        raise RadioError("GEMINI_API_KEY tanımlı değil")
    body = {
        "contents": [{"parts": [{"text": GEMINI_STYLE + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": s.gemini_voice}}},
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        resp = await client.post(GEMINI_URL.format(model=s.gemini_tts_model),
                                 headers={"x-goog-api-key": s.gemini_api_key}, json=body)
    if resp.status_code != 200:
        raise RadioError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        part = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
    except (KeyError, IndexError, ValueError) as exc:
        raise RadioError(f"Gemini yanıtında ses yok: {resp.text[:200]}") from exc
    data = base64.b64decode(part["data"])
    if data[:4] != b"RIFF":                 # ham PCM geldiyse WAV başlığı ekle (eski modeller)
        m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
        with wave.open(str(dst), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(m[1]) if m else 24000)
            wf.writeframes(data)
        return
    dst.write_bytes(data)


async def _tts_edge(s: Settings, text: str, dst: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, s.edge_voice).save(str(dst))


async def _speak(s: Settings, script: str, material: str) -> tuple[str, float]:
    """RADIO_TTS sırasıyla ses motorlarını dener; kullanılan motoru ve süreyi döndürür."""
    clean = clean_text(script)
    expected = len(clean.split()) / WORDS_PER_SEC
    src = OUT_DIR / "latest.audio"
    for engine in s.radio_tts:
        for attempt in range(2 if engine == "gemini" else 1):
            try:
                if engine == "gemini":
                    await _tts_gemini(s, clean, src)
                elif engine == "edge":
                    await _tts_edge(s, clean, src)
                elif engine == "piper":
                    await asyncio.to_thread(_synthesize, for_speech(script), english_terms(material),
                                            s.radio_voice, src)
                else:
                    raise RadioError(f"bilinmeyen ses motoru '{engine}'")
                await _to_ogg(src, OGG)
                duration = await _duration(OGG)
            except Exception as exc:
                log.warning("Radyo: %s sesi üretilemedi (deneme %d): %s", engine, attempt + 1, exc)
                if engine == "gemini" and attempt == 0 and "HTTP 429" not in str(exc):
                    await asyncio.sleep(5)   # Gemini ara sıra geçici 401/5xx veriyor; kota (429) hariç bir kez daha dene
                    continue
                break                        # sonraki motora geç
            finally:
                src.unlink(missing_ok=True)
            # Çok uzunsa talimatı da okumuş, çok kısaysa metni yarıda kesmiş olabilir
            if engine == "gemini" and not (0.55 * expected <= duration <= 1.3 * expected):
                log.warning("Radyo: Gemini sesi şüpheli uzunlukta (%.0f sn, beklenen ~%.0f sn), "
                            "deneme %d", duration, expected, attempt + 1)
                continue
            return engine, duration
    raise RadioError("Hiçbir ses motoru bülteni seslendiremedi")


async def _to_ogg(src: Path, ogg: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-application", "voip", str(ogg),
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RadioError(f"ffmpeg hatası: {err.decode(errors='replace')[:200]}")


async def _duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        return float(out.strip())
    except ValueError as exc:
        raise RadioError(f"ses süresi okunamadı: {path}") from exc


# ---------------------------------------------------------------------------
# Genel arayüz
# ---------------------------------------------------------------------------
def latest(max_age: timedelta) -> Bulletin | None:
    """Diskteki son bülten `max_age`'den yeniyse onu döndürür."""
    try:
        meta = json.loads(META.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(meta["created"])
    except (OSError, ValueError, KeyError):
        return None
    if not OGG.exists() or datetime.now(timezone.utc) - created > max_age:
        return None
    return Bulletin(OGG, int(meta["duration"]), created, meta.get("script", ""))


async def _make(s: Settings) -> Bulletin:
    t0 = time.monotonic()
    material = await gather(s)
    if not material:
        raise RadioError("Bülten için haber ya da maç bilgisi bulunamadı")
    script = await write_script(s, material)
    t_text = time.monotonic() - t0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    engine, duration = await _speak(s, script, material)

    created = datetime.now(timezone.utc)
    META.write_text(json.dumps({"created": created.isoformat(), "duration": round(duration),
                                "engine": engine, "script": script, "material": material},
                               ensure_ascii=False), encoding="utf-8")
    log.info("Radyo bülteni hazır: %d sn ses, %s (metin %.0f sn, toplam %.0f sn)",
             duration, engine, t_text, time.monotonic() - t0)
    return Bulletin(OGG, round(duration), created, script)


async def get(s: Settings, max_age: timedelta = timedelta(hours=6)) -> Bulletin:
    """Yeterince yeni bülten varsa onu, yoksa yenisini üretir (aynı anda tek üretim)."""
    if b := latest(max_age):
        return b
    async with _lock:
        if b := latest(max_age):          # beklerken başka istek üretmiş olabilir
            return b
        return await _make(s)
