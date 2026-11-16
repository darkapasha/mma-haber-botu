"""MMA / F1 Telegram Botu — giriş noktası.

Aynı kod .env'deki SPORTS ayarına göre MMA botu (SPORTS=mma), F1 botu (SPORTS=f1)
ya da ikisi bir arada (varsayılan) olarak çalışır.

Çalıştırma:  python bot.py
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, Defaults

import handlers as h
import jobs
from config import ORGS, Settings, get_settings
from services import http
from services.storage import Storage

log = logging.getLogger("mma_bot")

_ORG_KEYS = ", ".join(ORGS)


def commands(s: Settings) -> list[BotCommand]:
    multi = len(s.sports) > 1
    arg = " (" + " | ".join(s.sports) + ")" if multi else ""
    cmds = []
    if "mma" in s.sports:
        cmds += [BotCommand("kart", f"Yaklaşan maç kartı ({_ORG_KEYS})"),
                 BotCommand("sonuclar", "Son 5 etkinlik, seçilenin sonuçları"),
                 BotCommand("siralamalar", "Şampiyon ve ilk 10 (ufc, one)")]
    if "f1" in s.sports:
        if multi:
            cmds.append(BotCommand("f1", "F1: sıradaki GP, son yarış, puan durumları"))
        else:
            cmds += [BotCommand("gp", "Sıradaki Grand Prix ve seans programı"),
                     BotCommand("sonuclar", "Son yarışın sonuçları"),
                     BotCommand("pilotlar", "Pilotlar şampiyonası"),
                     BotCommand("takimlar", "Takımlar şampiyonası")]
    return cmds + [
        BotCommand("haberler", f"Son 24 saatin haberleri{arg}"),
        BotCommand("radyo", "Günün sesli bülteni"),
        BotCommand("abone", f"Bildirimleri aç{arg}"),
        BotCommand("ayril", f"Bildirimleri kapat{arg}"),
        BotCommand("yardim", "Yardım"),
    ]


async def post_init(app: Application) -> None:
    settings = app.bot_data["settings"]
    storage = Storage(settings.db_path)
    await storage.open()
    app.bot_data["storage"] = storage

    scheduler = AsyncIOScheduler(timezone="UTC")
    jobs.schedule(scheduler, app, settings)
    scheduler.start()
    app.bot_data["scheduler"] = scheduler

    await app.bot.set_my_commands(commands(settings))
    me = await app.bot.get_me()
    log.info("Bot hazır: @%s", me.username)


async def post_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    await http.close()
    storage = app.bot_data.get("storage")
    if storage:
        await storage.close()
    log.info("Bot kapatıldı")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx INFO logları her istekte bot token'ını içeren URL'yi yazar; kapalı kalmalı
    for noisy in ("apscheduler.executors.default", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app = (
        Application.builder()
        .token(settings.token)
        .defaults(Defaults(parse_mode=ParseMode.HTML,
                           link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings

    app.add_handler(CommandHandler("start", h.start))
    app.add_handler(CommandHandler(["yardim", "help"], h.help_cmd))
    app.add_handler(CommandHandler("abone", h.subscribe))
    app.add_handler(CommandHandler("ayril", h.unsubscribe))
    if "mma" in settings.sports:
        app.add_handler(CommandHandler("kart", h.kart))
        app.add_handler(CommandHandler("sonuclar", h.sonuclar))
        app.add_handler(CommandHandler("siralamalar", h.siralamalar))
    if "f1" in settings.sports:
        app.add_handler(CommandHandler("f1", h.f1_cmd))
        if settings.sports == ("f1",):       # ayrı F1 botu: menüsüz doğrudan komutlar
            app.add_handler(CommandHandler(["gp", "takvim", "program"], h.f1_gp))
            app.add_handler(CommandHandler(["sonuclar", "sonuc"], h.f1_sonuclar))
            app.add_handler(CommandHandler("pilotlar", h.f1_pilotlar))
            app.add_handler(CommandHandler("takimlar", h.f1_takimlar))
    app.add_handler(CommandHandler("haberler", h.haberler))
    app.add_handler(CommandHandler("radyo", h.radyo))
    app.add_handler(CallbackQueryHandler(h.on_button))
    app.add_error_handler(h.on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()