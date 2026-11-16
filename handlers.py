"""Telegram komut ve buton işleyicileri.

Callback verisi biçimleri (Telegram sınırı 64 bayt):
  "kart|<org>"          -> yaklaşan kart
  "sonuc|<org>"         -> son 5 etkinliğin buton listesi
  "sira|<org>"          -> şampiyonlar
  "res|<org>|<ref>"     -> seçilen etkinliğin sonuçları (ref: "e:<espn id>" | "t:<tapology id>")
  "sub|<spor>" / "unsub|<spor>"  -> bildirim aboneliği aç/kapat (spor: mma | f1)
  "news|<spor>"         -> son 24 saatin haberleri
  "f1|<race|result|drivers|teams>" -> F1 menüsü
"""
from __future__ import annotations

import logging
from datetime import timedelta
from html import escape
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import formatters as fmt
from config import ORGS, SPORTS, Org, Settings, resolve_org, resolve_sport
from models import EventRef
from scrapers import rankings
from services import events, f1, llm, news, radio
from services.storage import Storage

log = logging.getLogger(__name__)

ERROR_TEXT = ("⚠️ Veri kaynağına şu an ulaşılamıyor ({org}). "
              "Lütfen birkaç dakika sonra tekrar deneyin.")
PAST_EVENT_COUNT = 5

Reply = tuple[str, InlineKeyboardMarkup | None]


def _settings(ctx: ContextTypes.DEFAULT_TYPE) -> Settings:
    return ctx.bot_data["settings"]


def _storage(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.bot_data["storage"]


def org_keyboard(action: str, only_rankings: bool = False) -> InlineKeyboardMarkup:
    orgs = [o for o in ORGS.values() if o.rankings or not only_rankings]
    buttons = [InlineKeyboardButton(f"{o.emoji} {o.name}", callback_data=f"{action}|{o.key}") for o in orgs]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def events_keyboard(org: Org, refs: list[EventRef]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏁 {fmt.event_button_label(r)}", callback_data=f"res|{org.key}|{r.ref}")]
        for r in refs
    ])


# --- yanıt üreticileri ------------------------------------------------------
async def card_reply(org: Org, s: Settings, _args: list[str]) -> Reply:
    try:
        return fmt.format_card(org, await events.next_event(org), s), None
    except Exception:
        log.exception("Kart alınamadı: %s", org.key)
        return ERROR_TEXT.format(org=org.name), None


async def event_list_reply(org: Org, s: Settings, _args: list[str]) -> Reply:
    try:
        refs = await events.past_events(org, limit=PAST_EVENT_COUNT)
    except Exception:
        log.exception("Geçmiş etkinlikler alınamadı: %s", org.key)
        return ERROR_TEXT.format(org=org.name), None
    if not refs:
        return f"{org.emoji} <b>{escape(org.name)}</b>\n\nGeçmiş etkinlik bulunamadı.", None
    return fmt.format_event_list(org, refs), events_keyboard(org, refs)


async def rankings_reply(org: Org, s: Settings, args: list[str]) -> Reply:
    if not org.rankings:
        return (f"{org.emoji} <b>{org.name}</b> için resmi sıralama yayınlanmıyor. "
                "Sıralamalar yalnızca <code>ufc</code> ve <code>one</code> için mevcut."), None
    try:
        divisions = await rankings.get_rankings(org.rankings)
    except Exception:
        log.exception("Sıralama alınamadı: %s", org.key)
        return ERROR_TEXT.format(org=org.name), None
    if not args:
        return fmt.format_champions(org, divisions), None
    div = rankings.find_division(divisions, args)
    if not div:
        names = ", ".join(fmt.weight_tr(d.name) for d in divisions)
        return f"❓ '{escape(' '.join(args))}' sikleti bulunamadı.\n\nMevcut: {names}", None
    return fmt.format_division(org, div), None


async def results_text(org: Org, ref: str, s: Settings) -> str:
    try:
        return fmt.format_results(org, await events.event_by_ref(org, ref), s)
    except Exception:
        log.exception("Sonuçlar alınamadı: %s %s", org.key, ref)
        return ERROR_TEXT.format(org=org.name)


Builder = Callable[[Org, Settings, list[str]], Awaitable[Reply]]
BUILDERS: dict[str, Builder] = {"kart": card_reply, "sonuc": event_list_reply, "sira": rankings_reply}


async def _send(update: Update, reply: Reply) -> None:
    """Uzun metni böler; klavyeyi son parçaya ekler."""
    text, markup = reply
    chunks = fmt.split_message(text)
    for i, chunk in enumerate(chunks):
        await update.effective_message.reply_text(
            chunk, reply_markup=markup if i == len(chunks) - 1 else None)


async def _org_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str,
                       prompt: str, only_rankings: bool = False) -> None:
    args = ctx.args or []
    org = resolve_org(args[0]) if args else None
    if org is None:
        extra = f"\n\n❓ Bilinmeyen organizasyon: <code>{escape(args[0])}</code>" if args else ""
        await update.effective_message.reply_text(prompt + extra,
                                                  reply_markup=org_keyboard(action, only_rankings))
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send(update, await BUILDERS[action](org, _settings(ctx), args[1:]))


# --- abonelik ---------------------------------------------------------------
def sport_keyboard(action: str, sports: tuple[str, ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{SPORTS[sp][0]} {SPORTS[sp][1]}",
                                                       callback_data=f"{action}|{sp}") for sp in sports]])


def _pick_sport(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Komut argümanındaki spor; bot tek sporluysa (ayrı MMA / F1 botları) her zaman o spor."""
    sports = _settings(ctx).sports
    if len(sports) == 1:
        return sports[0]
    sport = resolve_sport(ctx.args[0]) if ctx.args else None
    return sport if sport in sports else None


async def _subscription_text(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    subs = await _storage(ctx).subscriptions_of(chat_id)
    on = [f"{SPORTS[sp][0]} {SPORTS[sp][1]}" for sp in _settings(ctx).sports if sp in subs]
    return "🔔 Açık bildirimler: " + (", ".join(on) if on else "<i>yok</i>")


async def _set_subscription(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, sport: str, on: bool) -> str:
    st = _storage(ctx)
    emoji, label = SPORTS[sport]
    if on:
        changed = await st.add_subscriber(chat_id, sport)
        head = f"🔔 {emoji} {label} bildirimleri açıldı." if changed else f"🔔 {emoji} {label} bildirimleri zaten açık."
    else:
        changed = await st.remove_subscriber(chat_id, sport)
        head = f"🔕 {emoji} {label} bildirimleri kapatıldı." if changed else f"🔕 {emoji} {label} bildirimleri zaten kapalı."
    return f"{head}\n{await _subscription_text(ctx, chat_id)}"


async def _sub_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE, on: bool) -> None:
    chat_id = update.effective_chat.id
    if sport := _pick_sport(ctx):
        await update.effective_message.reply_text(await _set_subscription(ctx, chat_id, sport, on))
        return
    prompt = ("Hangi sporun bildirimlerini açmak istersin?" if on
              else "Hangi sporun bildirimlerini kapatmak istersin?")
    await update.effective_message.reply_text(
        f"{prompt}\n{await _subscription_text(ctx, chat_id)}",
        reply_markup=sport_keyboard("sub" if on else "unsub", _settings(ctx).sports))


# --- komutlar ---------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = _settings(ctx)
    name = update.effective_user.first_name if update.effective_user else "spor sever"
    hello = f"👋 Hoş geldin <b>{escape(name)}</b>!\n\n" + fmt.help_text(s)
    if len(s.sports) == 1:              # tek sporlu bot: eskisi gibi doğrudan abone et
        await _storage(ctx).add_subscriber(update.effective_chat.id, s.sports[0])
        await update.effective_message.reply_text(
            hello + "\n\n🔔 Bu sohbet bildirimlere abone edildi. Kapatmak için /ayril.")
        return
    await update.effective_message.reply_text(
        hello + "\n\n👇 Hangi sporun bildirimlerini almak istersin? (Sonra /ayril ile kapatabilirsin.)",
        reply_markup=sport_keyboard("sub", s.sports))


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(fmt.help_text(_settings(ctx)))


async def subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _sub_command(update, ctx, on=True)


async def unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _sub_command(update, ctx, on=False)


async def kart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _org_command(update, ctx, "kart", "🥊 Hangi organizasyonun kartını görmek istersin?")


async def sonuclar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _org_command(update, ctx, "sonuc", "🏁 Hangi organizasyonun sonuçları?")


async def siralamalar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _org_command(update, ctx, "sira", "🏆 Hangi organizasyonun sıralaması?", only_rankings=True)


async def news_text(sport: str, s: Settings) -> str:
    try:
        items = await news.recent(hours=24, limit=12, sport=sport)
        titles = await llm.translate_titles(s, [i.title for i in items])
        return fmt.format_news(items, titles, sport, s, news.classify)
    except Exception:
        log.exception("Haberler alınamadı")
        return "⚠️ Haber kaynaklarına şu an ulaşılamıyor."


async def haberler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sport = _pick_sport(ctx)
    if sport is None:
        await update.effective_message.reply_text(
            "📰 Hangi sporun haberleri?", reply_markup=sport_keyboard("news", _settings(ctx).sports))
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send(update, (await news_text(sport, _settings(ctx)), None))


# --- F1 ---------------------------------------------------------------------
F1_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🗓 Sıradaki GP", callback_data="f1|race"),
     InlineKeyboardButton("🏁 Son yarış", callback_data="f1|result")],
    [InlineKeyboardButton("🏆 Pilotlar", callback_data="f1|drivers"),
     InlineKeyboardButton("🏗 Takımlar", callback_data="f1|teams")],
])
_F1_ARGS = {"takvim": "race", "program": "race", "gp": "race", "yaris": "race", "yarış": "race",
            "sonuc": "result", "sonuç": "result", "sonuclar": "result", "sonuçlar": "result",
            "pilot": "drivers", "pilotlar": "drivers", "takim": "teams", "takım": "teams",
            "takimlar": "teams", "takımlar": "teams"}


async def f1_text(action: str, s: Settings) -> str:
    try:
        if action == "race":
            return fmt.format_f1_race(await f1.next_race(), s)
        if action == "result":
            return fmt.format_f1_result(await f1.last_result(), s)
        if action == "drivers":
            rnd, rows = await f1.driver_standings()
            return fmt.format_f1_standings("Pilotlar Şampiyonası", rnd, rows, drivers=True)
        if action == "teams":
            rnd, rows = await f1.constructor_standings()
            return fmt.format_f1_standings("Takımlar Şampiyonası", rnd, rows, drivers=False)
    except Exception:
        log.exception("F1 verisi alınamadı: %s", action)
        return "⚠️ F1 verisine şu an ulaşılamıyor. Lütfen birkaç dakika sonra tekrar deneyin."
    return "❓ Bilinmeyen F1 seçeneği."


async def f1_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    action = _F1_ARGS.get(args[0].lower()) if args else None
    if action is None:
        await update.effective_message.reply_text("🏎️ <b>Formula 1</b> — ne görmek istersin?",
                                                  reply_markup=F1_MENU)
        return
    await _f1_reply(update, ctx, action)


async def _f1_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send(update, (await f1_text(action, _settings(ctx)), None))


# Ayrı F1 botunun doğrudan komutları
async def f1_gp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _f1_reply(update, ctx, "race")


async def f1_sonuclar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _f1_reply(update, ctx, "result")


async def f1_pilotlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _f1_reply(update, ctx, "drivers")


async def f1_takimlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _f1_reply(update, ctx, "teams")


async def radyo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = _settings(ctx)
    b = radio.latest(timedelta(hours=6))
    if b is None:
        await update.effective_message.reply_text(
            "🎙️ Bülten hazırlanıyor, birkaç dakika sürebilir ⏳")
        try:
            b = await radio.get(s)
        except Exception:
            log.exception("Radyo bülteni hazırlanamadı")
            await update.effective_message.reply_text(
                "⚠️ Bülten şu an hazırlanamadı. Biraz sonra tekrar deneyin.")
            return
    await update.effective_chat.send_action(ChatAction.UPLOAD_VOICE)
    await update.effective_message.reply_voice(b.path.read_bytes(), duration=b.duration,
                                               caption=fmt.format_radio_caption(b, s))


# --- butonlar ---------------------------------------------------------------
async def _show_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE, org: Org, ref: str) -> None:
    # Liste mesajı yerinde kalır; sonuçlar yeni mesaj olarak gelir, başka etkinlik de seçilebilir
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send(update, (await results_text(org, ref, _settings(ctx)), None))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")

    sports = _settings(ctx).sports
    if len(parts) == 2 and parts[0] in ("sub", "unsub") and parts[1] in sports:
        await query.answer()
        await query.edit_message_text(
            await _set_subscription(ctx, update.effective_chat.id, parts[1], parts[0] == "sub"),
            reply_markup=sport_keyboard(parts[0], sports))
        return
    if len(parts) == 2 and parts[0] == "news" and parts[1] in sports:
        await query.answer("⏳ Haberler çevriliyor…")
        await update.effective_chat.send_action(ChatAction.TYPING)
        await _send(update, (await news_text(parts[1], _settings(ctx)), None))
        return
    if len(parts) == 2 and parts[0] == "f1":
        await query.answer()
        await update.effective_chat.send_action(ChatAction.TYPING)
        await _send(update, (await f1_text(parts[1], _settings(ctx)), None))
        return

    org = ORGS.get(parts[1]) if len(parts) > 1 else None
    if org is None:
        await query.answer("Bu buton artık geçerli değil.")
        return

    if parts[0] == "res" and len(parts) == 3:
        await query.answer("⏳ Sonuçlar getiriliyor…")
        await _show_results(update, ctx, org, parts[2])
        return

    builder = BUILDERS.get(parts[0])
    if builder is None:
        await query.answer()
        return
    await query.answer()
    await query.edit_message_text(f"⏳ {org.emoji} {org.name} verileri getiriliyor…")
    text, markup = await builder(org, _settings(ctx), [])
    chunks = fmt.split_message(text)
    await query.edit_message_text(chunks[0], reply_markup=markup if len(chunks) == 1 else None)
    for i, chunk in enumerate(chunks[1:], start=1):
        await query.message.reply_text(chunk, reply_markup=markup if i == len(chunks) - 1 else None)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("İşlenmeyen hata", exc_info=ctx.error)