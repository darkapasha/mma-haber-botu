# 🥊🏎️ MMA ve F1 Haber Botları

Tek kod tabanından iki Telegram botu: **@mma_haber_bot** ve **@F1_haberbot**.
`.env` içindeki `SPORTS` değeri (`mma` / `f1`) hangi botun çalışacağını seçer.

## Ne yapıyorlar?

- 📰 **Önemli haberler**: RSS'ten gelen haberleri tam metniyle okur, Gemini ile Türkçe başlık ve özet yazar.
  Sadece önemli olanlar gider (MMA: tartı, iptal, sakatlık, rakip değişikliği; F1: ceza, kaza, transfer).
  Herkesin konuşacağı haberler "🔥 POPÜLER" etiketiyle; aynı olayın tekrarı elenir.
- 📻 **Sabah radyosu**: her sabah son sonuçlar, haberler ve yaklaşan maç/yarıştan 1-2 dakikalık
  Türkçe bir bülten yazılır ve sesli mesaj olarak gönderilir
  (metin: Gemini, olmazsa yerel gemma3; ses: Gemini TTS → Edge → Piper).
- 🗓️ **Kart, sonuç, sıralama**: UFC/PFL (ESPN), ONE ve diğerleri (Tapology); F1 takvimi, sonuçlar,
  pilot/takım puanları (Jolpica). Seanslardan 30 dk önce hatırlatma.

## Doğruluk

Radyo metni yalnızca verilen malzemeden yazılır. Dövüşlerin nasıl bittiği (nakavt, hakem kararı,
pes ettirme) ESPN'in olay akışından okunur; bilinmiyorsa modele "bitiriş bilinmiyor" denir ki uydurmasın.

## Kurulum

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, SPORTS=mma|f1 ...
sudo cp deploy/mma-bot.service /etc/systemd/system/ && sudo systemctl enable --now mma-bot
```

Anahtarlar yalnızca sunucudaki `.env` dosyasında; depoda yok. Ayrıntılı yapı: [TEKNIK.md](TEKNIK.md)

---
Claude Code ile birlikte geliştirildi.
