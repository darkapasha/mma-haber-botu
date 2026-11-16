# 🥊 MMA Telegram Botu

UFC, PFL, ONE Championship, Cage Warriors, CORE FC ve Arena Fight Night için maç kartı,
sonuç, sıralama ve anlık tartı/iptal bildirimleri.

## Yapı

```
bot.py                 Giriş noktası, handler ve zamanlayıcı kurulumu
handlers.py            /kart /sonuclar /siralamalar /haberler /abone /ayril
jobs.py                APScheduler görevleri (RSS alarmları, dövüş günü hatırlatması)
formatters.py          Telegram HTML mesaj şablonları
config.py              .env ayarları ve organizasyon tanımları
models.py, countries.py
services/api.py        ESPN MMA API (UFC, PFL)
services/events.py     Kaynak seçici: ESPN → Tapology yedeği
services/news.py       RSS okuma + anahtar kelime tespiti
services/storage.py    aiosqlite (görülen haberler, gönderilen bildirimler, aboneler)
services/http.py, services/cache.py
scrapers/tapology.py   Tapology kazıyıcı (httpx → Playwright yedeği)
scrapers/rankings.py   ufc.com ve onefc.com sıralamaları
deploy/mma-bot.service systemd servis dosyası
```

## Veri kaynakları

| Veri | Kaynak |
|---|---|
| UFC / PFL kart & sonuç | ESPN site API, olmazsa Tapology |
| ONE, Cage Warriors, CORE FC, Arena | Tapology |
| UFC sıralaması | ufc.com/rankings (şampiyon + ilk 10, P4P) |
| ONE sıralaması | onefc.com/world-champions (ONE, MMA için ilk 10 yayınlamıyor; yayınlarsa otomatik eklenir) |
| Haber / alarm | MMA Fighting, MMA Junkie, Sherdog RSS (`EXTRA_FEEDS` ile genişletilebilir) |

## Sunucuya kurulum (Ubuntu/Debian)

```bash
sudo useradd --system --create-home --home-dir /opt/mma-bot mmabot
sudo -u mmabot git clone <repo> /opt/mma-bot   # veya dosyaları kopyalayın
cd /opt/mma-bot
sudo -u mmabot python3 -m venv .venv
sudo -u mmabot .venv/bin/pip install -r requirements.txt
sudo -u mmabot env PLAYWRIGHT_BROWSERS_PATH=/opt/mma-bot/.playwright .venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium
sudo -u mmabot cp .env.example .env && sudo -u mmabot nano .env   # TELEGRAM_BOT_TOKEN

sudo cp deploy/mma-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mma-bot
journalctl -u mma-bot -f
```

Python 3.11+ gerekir.

## Kullanım

- `/kart ufc` · `/kart cw` · argümansız `/kart` → organizasyon butonları
- `/sonuclar pfl`
- `/siralamalar ufc` (tüm şampiyonlar) · `/siralamalar ufc hafif` · `/siralamalar ufc kadın sinek` · `/siralamalar ufc p4p`
  Siklet adları: atom, saman, sinek, horoz, tüy, hafif, yarı orta, orta, yarı ağır, ağır (İngilizce de olur)
- `/haberler`
- `/start` veya `/abone` sohbeti bildirimlere abone eder, `/ayril` aboneliği kapatır.
  Kanal/grup için `.env` içinde `ALERT_CHAT_IDS` kullanın.

## Bildirim mantığı

- **Haber alarmları** (10 dk'da bir): başlık ve özet `missed weight`, `weigh-in`, `injured`,
  `cancelled`, `replacement`, `pulls out`, `steps in` vb. kalıplarla taranır. Her haber SQLite'ta
  kimliğiyle ve normalize başlığıyla kontrol edilir; gönderim atomik bir kilitle (`INSERT OR IGNORE`)
  yapıldığı için yeniden başlatmalarda ya da farklı kaynaklarda aynı haber iki kez gitmez.
  Bot ilk çalıştığında mevcut haberleri bildirim atmadan "görüldü" olarak işaretler.
- **Dövüş günü hatırlatması** (5 dk'da bir): her organizasyonun sıradaki etkinliği kontrol edilir,
  ana karta ≤ 60 dk kaldığında bir kez hatırlatma gönderilir (saat bilinmiyorsa gönderilmez).

## Notlar

- Tapology Cloudflare arkasındadır; düz HTTP isteği engellenirse sayfa headless Chromium ile açılır.
  İstekler arasında en az 2,5 sn beklenir ve sonuçlar 15–30 dk önbelleğe alınır.
- Cage Warriors, CORE FC ve Arena için Tapology adresi verilmezse Tapology aramasıyla bulunur ve
  bulunan adres log'a yazılır. Yanlış bir promosyon eşleşirse doğru adresi `.env` içinde
  `TAPOLOGY_URL_CW=...` gibi sabitleyin.
- ESPN API bazı ağlardan/bölgelerden 403 döndürebilir; bu durumda bot otomatik olarak Tapology'ye geçer.
