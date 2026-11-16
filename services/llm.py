"""Haber çevirisi ve özeti: önce Gemini (hızlı, iyi Türkçe), olmazsa yerel Ollama."""
from __future__ import annotations

import json
import logging
import re

import httpx

from config import SPORTS, Settings
from models import NewsItem, TranslatedNews

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

ARTICLE_PROMPT = """Aşağıdaki İngilizce {sport} haberini Türk okuyucular için Türkçeye aktar.

Kurallar:
- "baslik": haberin Türkçe başlığı, doğal ve kısa.
- "ozet": haberin TAMAMINI kapsayan, eksiksiz 4-6 cümlelik Türkçe özet. Önemli isimleri, \
sayıları, tarihleri ve alıntıları koru. Yarıda kesme, "..." kullanma.
- Sadece metindeki bilgileri kullan, hiçbir şey ekleme ya da uydurma.
- Kişi, takım ve organizasyon isimlerini çevirme.
- Yanıt yalnızca şu biçimde JSON olsun: {{"baslik": "...", "ozet": "..."}}

BAŞLIK: {title}

METİN:
{text}"""

TITLES_PROMPT = """Aşağıdaki İngilizce spor haber başlıklarını doğal Türkçeye çevir. İsimleri çevirme.
Yanıt yalnızca aynı sırayla bir JSON dizisi olsun: ["...", "..."]

{titles}"""


POPULARITY_PROMPT = """Sen deneyimli bir {sport} editörüsün. Aşağıdaki yeni haberlerin her biri için, \
{sport} hayranları arasında ne kadar konuşulacağını 1-10 arası puanla.

Ölçek:
- 9-10: Herkesin konuşacağı büyük haber (büyük şampiyonluk maçı/yarış sonucu sürprizi, emeklilik, \
dev transfer ya da takım değişikliği, büyük skandal, yasak, ölüm, rekor).
- 7-8: Hayranların çoğunun ilgisini çeken önemli gelişme (büyük isim yeni maç/sözleşme, \
önemli açıklama veya kavga, kural değişikliği, büyük bir takımın kriz haberi).
- 4-6: Normal haber; takip edenler okur.
- 1-3: Rutin içerik (röportaj, tahmin, analiz, liste, "neler bekleniyor", ürün, oyun, podcast).

Tekrar: Bir haber "DAHA ÖNCE GÖNDERİLENLER" listesindeki bir haberle ya da yeni haberler listesinde \
KENDİSİNDEN ÖNCE gelen bir haberle AYNI olayı anlatıyorsa "tekrar": true, değilse false.

DAHA ÖNCE GÖNDERİLENLER:
{sent}

YENİ HABERLER:
{items}

Yanıt yalnızca yeni haberlerle aynı sırada bir JSON dizisi olsun, \
örnek: [{{"puan": 3, "tekrar": false}}, {{"puan": 8, "tekrar": true}}]"""
POPULARITY_SCHEMA = {"type": "array", "items": {
    "type": "object", "required": ["puan", "tekrar"],
    "properties": {"puan": {"type": "integer"}, "tekrar": {"type": "boolean"}}}}


class LLMError(Exception):
    pass


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)      # kod bloğuna sarılmışsa
    return json.loads(text)


async def _gemini(s: Settings, prompt: str):
    if not s.gemini_api_key:
        raise LLMError("GEMINI_API_KEY tanımlı değil")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
        resp = await client.post(GEMINI_URL.format(model=s.gemini_text_model),
                                 headers={"x-goog-api-key": s.gemini_api_key}, json=body)
    if resp.status_code != 200:
        raise LLMError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Gemini yanıtı boş: {resp.text[:200]}") from exc
    return _parse_json(text)


async def _ollama(s: Settings, prompt: str, schema: dict | None = None):
    # Şema verilirse Ollama çıktıyı ona zorlar (küçük modeller serbest JSON'da tırnak kaçırabiliyor)
    payload = {"model": s.radio_model, "stream": False, "format": schema or "json", "keep_alive": "5m",
               "options": {"temperature": 0.3, "num_ctx": 8192},
               "messages": [{"role": "user", "content": prompt}]}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
        resp = await client.post(f"{s.ollama_url}/api/chat", json=payload)
    if resp.status_code != 200:
        raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
    return _parse_json((resp.json().get("message") or {}).get("content", ""))


ARTICLE_SCHEMA = {"type": "object", "required": ["baslik", "ozet"],
                  "properties": {"baslik": {"type": "string"}, "ozet": {"type": "string"}}}
TITLES_SCHEMA = {"type": "array", "items": {"type": "string"}}


async def ask_json(s: Settings, prompt: str, schema: dict | None = None):
    """JSON yanıt bekleyen istem: Gemini, olmazsa Ollama."""
    try:
        return await _gemini(s, prompt)
    except Exception as exc:
        log.warning("Gemini metin modeli kullanılamadı, Ollama'ya geçiliyor: %s", exc)
    return await _ollama(s, prompt, schema)


async def translate_article(s: Settings, item: NewsItem, text: str) -> TranslatedNews:
    prompt = ARTICLE_PROMPT.format(sport=SPORTS.get(item.sport, ("", "spor"))[1],
                                   title=item.title, text=text)
    try:
        data = await ask_json(s, prompt, ARTICLE_SCHEMA)
        title, summary = str(data.get("baslik", "")).strip(), str(data.get("ozet", "")).strip()
        if title and len(summary) > 40:
            return TranslatedNews(title, summary)
        log.warning("Çeviri eksik döndü: %s", item.link)
    except Exception as exc:
        log.warning("Haber çevrilemedi (%s): %s", item.link, exc)
    # Son çare: orijinal başlık ve RSS özeti (kesilmeden)
    return TranslatedNews(item.title, item.summary or text[:600], translated=False)


async def rate_popularity(s: Settings, sport: str, items: list[NewsItem],
                          sent_titles: list[str]) -> list[tuple[int, bool]] | None:
    """Her haber için (1-10 popülerlik puanı, daha önce gönderilen/önceki bir haberle aynı olay mı).
    Model kullanılamazsa None: popüler haber seçilmez, önemli haberler tekrar kontrolsüz gönderilir."""
    if not items:
        return []
    listing = "\n".join(
        f"{n + 1}. {i.title}" + (f" — {i.summary[:180]}" if i.summary else "") for n, i in enumerate(items))
    prompt = POPULARITY_PROMPT.format(sport=SPORTS.get(sport, ("", "spor"))[1],
                                      sent="\n".join(f"- {t}" for t in sent_titles) or "(yok)",
                                      items=listing)
    try:
        data = await ask_json(s, prompt, POPULARITY_SCHEMA)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        out = [(int(x.get("puan", 0)), bool(x.get("tekrar", False))) for x in data]
        if len(out) == len(items):
            return out
        log.warning("Popülerlik puanları eksik döndü (%d/%d)", len(out), len(items))
    except Exception as exc:
        log.warning("Popülerlik puanlanamadı: %s", exc)
    return None


async def translate_titles(s: Settings, titles: list[str]) -> list[str]:
    if not titles:
        return []
    prompt = TITLES_PROMPT.format(titles="\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles)))
    try:
        data = await ask_json(s, prompt, TITLES_SCHEMA)
        if isinstance(data, dict):           # bazı modeller diziyi bir anahtar altında döndürür
            data = next((v for v in data.values() if isinstance(v, list)), [])
        out = [re.sub(r"^\d+[.)]\s*", "", str(x).strip()) for x in data]
        if len(out) == len(titles) and all(out):
            return out
        log.warning("Başlık çevirisi eksik döndü (%d/%d)", len(out), len(titles))
    except Exception as exc:
        log.warning("Başlıklar çevrilemedi: %s", exc)
    return titles
