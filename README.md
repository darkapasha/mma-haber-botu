# 🥊 MMA Haber Botu

Telegram'da **@mma_haber_bot**. UFC, PFL, ONE, Cage Warriors ve diğer organizasyonlar için maç kartları,
sonuçlar, sıralamalar, önemli haberlerin Türkçe özetleri ve her sabah sesli bir **MMA Radyosu** bülteni.

> Kardeş bot: [🏎️ F1 Haber Botu](https://github.com/darkapasha/f1-haber-botu) (aynı motor, ayrı bot)

---

## Ne yapıyor?

### 📰 Sadece önemli haberler, Türkçe
Bot 10 dakikada bir MMA Fighting, Sherdog, Bloody Elbow gibi kaynakların RSS'lerini okur. Her haberin **tam metnini**
çeker ve sınıflandırır. Yalnızca gerçekten önemli olanlar gönderilir:

| Etiket | Örnek |
|---|---|
| ⚖️ Tartı | "X kiloyu tutturamadı, maç catchweight'e döndü" |
| ❌ İptal | "Ana maç iptal edildi" |
| 🤕 Sakatlık | "Y sakatlandı, karttan çekildi" |
| 🔄 Değişiklik | "Z kısa sürede rakip oldu" |
| 🔥 Popüler | Herkesin konuşacağı büyük haber (yapay zekâ 1-10 arası puanlar, 8 ve üstü gider) |

Haber Gemini ile Türkçeye aktarılır: kısa bir başlık ve **haberin tamamını kapsayan** 4-6 cümlelik özet.
Aynı olayı anlatan ikinci, üçüncü haber (son 48 saatte gönderilenlerle karşılaştırılarak) elenir; böylece
bir dövüşçünün sakatlığı beş farklı siteden beş kez gelmez.

### 📻 Sabah MMA Radyosu
Her sabah 08:00'de son 3 günün sonuçları, günün haberleri ve önümüzdeki 8 günün maçlarından
**1-2 dakikalık Türkçe bir radyo bülteni** hazırlanır ve sesli mesaj olarak gönderilir. `/radyo` ile istendiğinde de gelir.

```
malzeme topla ─▶ metni yaz ─▶ seslendir ─▶ OGG/Opus ─▶ Telegram sesli mesaj
 (ESPN, RSS)    Gemini         Gemini TTS
               (yedek: yerel   (yedek: Edge,
                gemma3:4b)      sonra Piper)
```

**Doğruluk önemli.** Maçın nasıl bittiği (nakavt, hakem kararı, pes ettirme) ESPN'in maç akışından okunur
ve metne öyle yazılır. Bilgi yoksa modele açıkça *"bitiriş bilinmiyor"* denir ve uydurması yasaklanır.
Böylece hakem kararıyla biten bir maça "nakavt etti" denmez.

Yabancı isimler için küçük bir hile: yerel Piper sesi Türkçe kurallarla okur ("Christian" → "Cehristian").
Bunun yerine isimler İngilizce fonemlere çevrilip Türkçe sesin bildiği seslere eşlenir; sonuç, isimleri
İngilizce telaffuzla okuyan bir Türk spiker gibidir.

### 🗓️ Kart, sonuç, sıralama
- `/kart` — organizasyon düğmeleri; `/kart ufc`, `/kart cw` ana kart + ön kartlar, Türkiye saatiyle
- `/sonuclar` — son 5 etkinlik, seçilenin sonuçları (kazanan, yöntem, raunt)
- `/siralamalar ufc` — tüm şampiyonlar; `/siralamalar ufc hafif`, `/siralamalar ufc p4p`
- `/haberler` — son 24 saatin önemli haberleri
- `/abone` · `/ayril` — bildirimleri aç / kapat
- Dövüş günü ana karttan 60 dakika önce hatırlatma

## Veri kaynakları

| Veri | Kaynak |
|---|---|
| UFC / PFL kart ve sonuç | ESPN site API |
| ONE, Cage Warriors, CORE FC, Arena | Tapology (sunucunun IP'si engellendiği için şu an 3 saatlik "devre kesici" ile korunuyor) |
| UFC sıralaması | ufc.com/rankings |
| Haberler | RSS + tam metin (trafilatura) |

## Yapı

```
bot.py              giriş noktası, komutlar, zamanlayıcı
handlers.py         Telegram komutları
jobs.py             haber taraması, popülerlik, dövüş günü hatırlatması, sabah radyosu
formatters.py       Telegram HTML mesaj şablonları
services/api.py     ESPN istemcisi (bitiriş yöntemi dahil)
services/news.py    RSS + tam metin
services/llm.py     Gemini / Ollama: çeviri, özet, popülerlik puanı
services/radio.py   bülten: malzeme, metin, ses motorları, ffmpeg
scrapers/           Tapology ve sıralama kazıyıcıları
```

## Kurulum

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env          # TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, SPORTS=mma ...
sudo cp deploy/mma-bot.service /etc/systemd/system/
sudo systemctl enable --now mma-bot
```

Yerel yedek model için [Ollama](https://ollama.com) + `gemma3:4b`, ses için `ffmpeg` gerekir.
Anahtarlar yalnızca sunucudaki `.env` dosyasında durur; depoda yok. Ayrıntılı teknik notlar: [TEKNIK.md](TEKNIK.md)

---

Oracle Cloud'daki küçük bir ARM sunucuda 7/24 çalışıyor. Claude ile birlikte, vibe coding ile yapıldı 🎧
