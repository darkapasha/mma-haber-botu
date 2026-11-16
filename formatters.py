"""Telegram HTML mesaj biçimlendiricileri."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from config import ORGS, SPORTS, Org, Settings
from countries import country_flag
from models import Bout, Division, Event, EventRef, Fighter, NewsItem, RankEntry, TranslatedNews
from services.f1 import nationality_country as f1_nationality_country
from services.news import AlertRule

TG_LIMIT = 4000
BUTTON_LABEL_MAX = 48

TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık"]

_WEIGHT_TR = {
    "light heavyweight": "Yarı Ağır", "heavyweight": "Ağır", "cruiserweight": "Cruiser",
    "middleweight": "Orta", "welterweight": "Yarı Orta", "lightweight": "Hafif",
    "featherweight": "Tüy", "bantamweight": "Horoz", "flyweight": "Sinek",
    "strawweight": "Saman", "atomweight": "Atom", "catchweight": "Catchweight",
    "openweight": "Açık", "pound-for-pound": "Pound-for-Pound",
}
SEGMENT_TITLES = {"main": "🔥 <b>ANA KART</b>", "prelims": "🥊 <b>ÖN KART</b>",
                  "early": "🌅 <b>ERKEN ÖN KART</b>"}

# Yaygın submission adlarının kısaltmaları
_SUB_ABBR = {
    "rear naked choke": "RNC", "guillotine choke": "Guillotine", "guillotine": "Guillotine",
    "arm triangle choke": "Arm Triangle", "arm triangle": "Arm Triangle",
    "triangle choke": "Triangle", "d'arce choke": "D'Arce", "anaconda choke": "Anaconda",
    "north south choke": "North-South", "von flue choke": "Von Flue", "ezekiel choke": "Ezekiel",
    "peruvian necktie": "Peruvian Necktie", "brabo choke": "Brabo",
    "armbar": "Armbar", "kimura": "Kimura", "americana": "Americana", "heel hook": "Heel Hook",
    "kneebar": "Kneebar", "ankle lock": "Ankle Lock", "calf slicer": "Calf Slicer",
    "neck crank": "Neck Crank", "twister": "Twister",
}
_DEC_CODES = {"unanimous": "U-DEC", "split": "S-DEC", "majority": "M-DEC"}


def weight_tr(name: str | None) -> str:
    if not name:
        return ""
    low = name.lower()
    women = "women" in low or "kadın" in low
    for en, tr in sorted(_WEIGHT_TR.items(), key=lambda kv: -len(kv[0])):
        if en in low:
            suffix = "" if en in {"pound-for-pound", "catchweight"} else " Siklet"
            extra = re.search(r"\(\d+ lbs\)", name)
            return f"{'Kadınlar ' if women else ''}{tr}{suffix}{' ' + extra[0] if extra else ''}"
    return name


def fmt_date(dt: datetime, tz: ZoneInfo) -> str:
    d = dt.astimezone(tz)
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d.year}, {TR_DAYS[d.weekday()]}"


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def _f(f: Fighter, bold: bool = False) -> str:
    name = escape(f.name)
    name = f"<b>{name}</b>" if bold else name
    return f"{f.flag} {name}".strip()


def _sub_name(detail: str) -> str:
    key = re.sub(r"[\s\-]+", " ", detail.lower().replace("’", "'")).strip()
    return _SUB_ABBR.get(key, detail)


def method_label(method: str | None, detail: str | None = None) -> tuple[str, str]:
    """(emoji, etiket): 'KO/TKO - Punches', 'SUB - RNC', 'U-DEC', 'NC', 'DQ'..."""
    if not method and not detail:
        return "", ""
    m = (method or "").strip()
    d = (detail or "").strip()
    ml, s = m.lower(), f"{m} {d}".lower()

    def with_detail(code: str) -> str:
        return f"{code} - {d}" if d else code

    if "no contest" in s or ml == "nc" or "overturned" in s:
        return "⛔", "NC"
    if "draw" in ml:
        kind = next((k for k in _DEC_CODES if k in s), "")
        return "🤝", f"BERABERE ({kind.title()})" if kind else "BERABERE"
    if re.search(r"\bdq\b|disqual", s):
        return "🚫", with_detail("DQ")
    if ml.startswith("sub"):
        return "🔒", f"SUB - {_sub_name(d)}" if d else "SUB"
    if re.search(r"\bt?ko\b|knockout", ml):
        if "/" in ml:
            code = "KO/TKO"
        elif "tko" in ml or "technical" in ml:
            code = "TKO"
        else:
            code = "KO"
        return "💥", with_detail(code)
    if "dec" in ml:
        return "📋", next((c for k, c in _DEC_CODES.items() if k in s), "DEC")
    return "🥊", with_detail(m or d)


def finish_label(b: Bout) -> str:
    """Bittiği raunt ve süre: 'R2 · 3:14'."""
    parts = []
    if b.round:
        parts.append(f"R{b.round}")
    if b.time:
        parts.append(b.time)
    return " · ".join(parts)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else block.split("\n")
        for piece in pieces:
            sep = "\n\n" if piece is block else "\n"
            cand = f"{cur}{sep}{piece}" if cur else piece
            if len(cand) > limit and cur:
                parts.append(cur)
                cur = piece
            else:
                cur = cand
    if cur:
        parts.append(cur)
    return parts


def _header(org: Org, ev: Event, s: Settings, show_time: bool = True) -> list[str]:
    lines = [f"{org.emoji} <b>{escape(org.name)}</b>", f"🏟 <b>{escape(ev.name)}</b>"]
    if ev.start:
        lines.append(f"📅 {fmt_date(ev.start, s.tz)}")
        if show_time and ev.time_known:
            if ev.main_card_start and ev.start and ev.main_card_start > ev.start:
                lines.append(f"⏰ Ana kart: <b>{fmt_time(ev.main_card_start, s.tz)}</b> {s.tz_label} "
                             f"· Ön kart: {fmt_time(ev.start, s.tz)}")
            else:
                start = ev.main_card_start or ev.start
                lines.append(f"⏰ Başlangıç: <b>{fmt_time(start, s.tz)}</b> {s.tz_label}")
        elif show_time:
            lines.append("⏰ Saat henüz açıklanmadı")
    if ev.venue:
        lines.append(f"📍 {escape(ev.venue)}")
    return lines


def _group(bouts: list[Bout]) -> list[tuple[str, list[Bout]]]:
    groups: dict[str, list[Bout]] = {}
    for b in bouts:
        groups.setdefault(b.segment, []).append(b)
    return [(seg, groups[seg]) for seg in ("main", "prelims", "early") if seg in groups]


def _detail(b: Bout) -> str:
    bits = [weight_tr(b.weight_class)] if b.weight_class else []
    if b.title_fight:
        bits.append("Unvan Maçı")
    return f"      <i>{escape(' · '.join(bits))}</i>" if bits else ""


def format_card(org: Org, ev: Event | None, s: Settings) -> str:
    if ev is None:
        return f"{org.emoji} <b>{escape(org.name)}</b>\n\nYaklaşan bir etkinlik bulunamadı."
    lines = _header(org, ev, s)
    if not ev.bouts:
        lines += ["", "🥊 Maç kartı henüz açıklanmadı."]
    first = True
    for seg, bouts in _group(ev.bouts):
        lines += ["", SEGMENT_TITLES[seg]]
        for b in bouts:
            icon = "🏆" if b.title_fight else ("⭐" if first and seg == "main" else "🥊")
            first = False
            lines.append(f"{icon} {_f(b.fighter_a, True)} <i>vs</i> {_f(b.fighter_b, True)}")
            if d := _detail(b):
                lines.append(d)
    lines.append(_footer(ev))
    return "\n".join(lines)


def format_event_list(org: Org, refs: list[EventRef]) -> str:
    return (f"🏁 {org.emoji} <b>{escape(org.name)}</b> · Son {len(refs)} etkinlik\n\n"
            "Sonuçlarını görmek istediğin etkinliği seç:")


def event_button_label(ref: EventRef) -> str:
    prefix = f"{ref.date:%d.%m.%Y} · " if ref.date else ""
    label = prefix + ref.name
    return label if len(label) <= BUTTON_LABEL_MAX else label[:BUTTON_LABEL_MAX - 1].rstrip() + "…"


def format_results(org: Org, ev: Event | None, s: Settings) -> str:
    if ev is None:
        return f"{org.emoji} <b>{escape(org.name)}</b>\n\nBu etkinliğin sonuçlarına ulaşılamadı."
    lines = _header(org, ev, s, show_time=False)
    finished = [b for b in ev.bouts if b.finished]
    if not finished:
        lines += ["", "Sonuçlar henüz açıklanmadı."]
    for seg, bouts in _group(finished):
        lines += ["", SEGMENT_TITLES[seg]]
        for b in bouts:
            emoji, method = method_label(b.method, b.method_detail)
            meta = " · ".join(x for x in (f"{emoji} {method}".strip(), finish_label(b),
                                          weight_tr(b.weight_class)) if x)
            trophy = " 🏆" if b.title_fight else ""
            if b.winner in (0, 1):
                w, l = (b.fighter_a, b.fighter_b) if b.winner == 0 else (b.fighter_b, b.fighter_a)
                lines.append(f"✅ {_f(w, True)} <i>yendi</i> {_f(l)}{trophy}")
            else:
                lines.append(f"🥊 {_f(b.fighter_a, True)} <i>vs</i> {_f(b.fighter_b, True)}{trophy}")
            if meta:
                lines.append(f"      {escape(meta)}")
    lines.append(_footer(ev))
    return "\n".join(lines)


def _footer(ev: Event) -> str:
    src = f'<a href="{escape(ev.url)}">{escape(ev.source)}</a>' if ev.url else escape(ev.source)
    return f"\n<i>Kaynak: {src}</i>" if src else ""


def _entry(e: RankEntry) -> str:
    change = f" <i>{escape(e.change)}</i>" if e.change else ""
    return f"<code>{escape(e.rank):>2}.</code> {e.flag + ' ' if e.flag else ''}{escape(e.name)}{change}"


def format_division(org: Org, d: Division) -> str:
    lines = [f"{org.emoji} <b>{escape(org.name)} · {escape(weight_tr(d.name))}</b>", ""]
    p4p = "pound-for-pound" in d.key
    if d.champion and not p4p:
        lines.append(f"👑 Şampiyon: {d.champion.flag + ' ' if d.champion.flag else ''}<b>{escape(d.champion.name)}</b>")
    if d.interim:
        lines.append(f"🥈 Geçici Şampiyon: {d.interim.flag + ' ' if d.interim.flag else ''}<b>{escape(d.interim.name)}</b>")
    if lines[-1]:
        lines.append("")
    lines += [_entry(e) for e in d.entries[:10]] or [
        "<i>Bu siklet için resmi ilk 10 listesi yayınlanmıyor.</i>"]
    return "\n".join(lines)


def format_champions(org: Org, divisions: list[Division]) -> str:
    lines = [f"{org.emoji} <b>{escape(org.name)} Şampiyonları</b>", ""]
    for d in divisions:
        if "pound-for-pound" in d.key:
            continue
        champ = escape(d.champion.name) if d.champion else "<i>Boş</i>"
        flag = d.champion.flag + " " if d.champion and d.champion.flag else ""
        lines.append(f"🏆 <b>{escape(weight_tr(d.name))}</b>: {flag}{champ}")
    hint = f"Detay için: <code>/siralamalar {org.key} hafif</code>"
    if org.rankings == "ufc":
        hint += (f" · <code>/siralamalar {org.key} kadın sinek</code>"
                 f" · <code>/siralamalar {org.key} p4p</code>")
    lines += ["", hint]
    return "\n".join(lines)


def _news_time(i: NewsItem, s: Settings) -> str:
    return fmt_time(i.published, s.tz) if i.published else ""


def format_news(items: list[NewsItem], titles: list[str], sport: str, s: Settings, classifier) -> str:
    """`titles`: Türkçeye çevrilmiş başlıklar (items ile aynı sırada)."""
    emoji, label = SPORTS[sport]
    if not items:
        return f"📰 Son 24 saatte {label} haberi bulunamadı."
    lines = [f"📰 {emoji} <b>Son 24 Saatin {label} Haberleri</b>", ""]
    for i, title in zip(items, titles):
        rule: AlertRule | None = classifier(i)
        icon = rule.emoji if rule else "▪️"
        lines.append(f'{icon} <a href="{escape(i.link)}">{escape(title)}</a>')
        lines.append(f"      <i>{escape(i.source)} · {_news_time(i, s)}</i>")
    return "\n".join(lines)


def format_alert(item: NewsItem, tr: TranslatedNews, rule: AlertRule | None, s: Settings,
                 popular: bool = False) -> str:
    emoji, label = SPORTS[item.sport]
    if rule:
        head = f"{rule.emoji} <b>{label} · {rule.label}</b>" + (" 🔥" if popular else "")
    elif popular:
        head = f"🔥 <b>{label} · POPÜLER</b>"
    else:
        head = f"{emoji} <b>{label} HABER</b>"
    lines = [head, "", f"<b>{escape(tr.title)}</b>", "", escape(tr.summary)]
    if not tr.translated:
        lines += ["", "<i>⚠️ Çeviri şu an yapılamadı, orijinal metin gösteriliyor.</i>"]
    lines += ["", f"📰 {escape(item.source)} · {_news_time(item, s)}",
              f'🔗 <a href="{escape(item.link)}">Kaynak (İngilizce)</a>']
    return "\n".join(lines)


# --- F1 ----------------------------------------------------------------------------
_PODIUM = {"1": "🥇", "2": "🥈", "3": "🥉"}


def _flag_nat(nationality: str) -> str:
    return country_flag(f1_nationality_country(nationality)) if nationality else ""


def format_f1_race(race, s: Settings) -> str:
    """`race`: services.f1.Race"""
    if race is None:
        return "🏎️ <b>F1</b>\n\nSezonda kalan yarış bulunamadı."
    flag = country_flag(race.country)
    lines = [f"🏎️ <b>F1 · {race.round}. Yarış</b>",
             f"{flag} <b>{escape(race.name)}</b>".strip(),
             f"📍 {escape(race.circuit)}, {escape(race.locality)}"]
    if race.is_sprint:
        lines.append("⚡ Sprint haftası")
    lines += ["", f"🗓 <b>Program</b> ({s.tz_label})"]
    now = datetime.now(timezone.utc)
    for x in race.sessions:
        local = x.start.astimezone(s.tz)
        done = "✅ " if x.start < now else ""
        name = f"<b>{x.name}</b>" if x.remind else x.name
        lines.append(f"{done}{TR_DAYS[local.weekday()][:3]} {local.day} {TR_MONTHS[local.month - 1][:3]} "
                     f"· {local:%H:%M} · {name}")
    return "\n".join(lines)


def format_f1_result(res, s: Settings) -> str:
    """`res`: services.f1.RaceResult"""
    if res is None or not res.rows:
        return "🏁 <b>F1</b>\n\nHenüz sonuç yok."
    lines = [f"🏁 <b>{escape(res.race)}</b> · Sonuçlar", ""]
    for r in res.rows:
        pos = _PODIUM.get(r.position, f"{r.position}.")
        pts = f" · <i>+{r.points}</i>" if r.points not in ("0", "0.0") else ""
        lines.append(f"{pos} {_flag_nat(r.nationality)} <b>{escape(r.driver)}</b> "
                     f"<i>({escape(r.team)})</i> · {escape(r.time)}{pts}")
    return "\n".join(lines)


def format_f1_standings(title: str, round_: str, rows, drivers: bool) -> str:
    lines = [f"🏆 <b>F1 · {title}</b>" + (f" <i>({round_}. yarıştan sonra)</i>" if round_ else ""), ""]
    for r in rows:
        pos = _PODIUM.get(r.position, f"{r.position}.")
        team = f" <i>({escape(r.team)})</i>" if drivers and r.team else ""
        flag = _flag_nat(r.nationality) + " " if drivers else ""
        wins = f" · {r.wins} galibiyet" if r.wins not in ("0", "") else ""
        lines.append(f"{pos} {flag}<b>{escape(r.name)}</b>{team} · <b>{r.points}</b> puan{wins}")
    return "\n".join(lines)


def format_f1_reminder(race, session, s: Settings) -> str:
    local = session.start.astimezone(s.tz)
    mins = max(1, int((session.start - datetime.now(timezone.utc)).total_seconds() // 60))
    return (f"🚦 <b>F1 HATIRLATMA</b>\n"
            f"{country_flag(race.country)} <b>{escape(race.name)}</b>\n"
            f"⏰ <b>{session.name}</b> {mins} dakika sonra başlıyor ({local:%H:%M} {s.tz_label})")


def format_reminder(org: Org, ev: Event, s: Settings) -> str:
    start = ev.main_card_start or ev.start
    mins = max(1, int((start - datetime.now(timezone.utc)).total_seconds() // 60)) if start else 0
    lines = [f"🚨 <b>DÖVÜŞ GÜNÜ HATIRLATMASI</b>",
             f"{org.emoji} <b>{escape(org.name)}</b> · {escape(ev.name)}",
             f"⏰ Ana kart <b>{mins} dakika</b> sonra başlıyor ({fmt_time(start, s.tz)} {s.tz_label})"]
    main = [b for b in ev.bouts if b.segment == "main"][:5]
    if main:
        lines += ["", SEGMENT_TITLES["main"]]
        for n, b in enumerate(main):
            icon = "🏆" if b.title_fight else ("⭐" if n == 0 else "🥊")
            lines.append(f"{icon} {_f(b.fighter_a, True)} <i>vs</i> {_f(b.fighter_b, True)}")
    return "\n".join(lines)


def radio_name(sports: tuple[str, ...]) -> str:
    return " ve ".join(SPORTS[sp][1] for sp in sports) + " Radyosu"


def format_radio_caption(b, s: Settings) -> str:
    """`b`: services.radio.Bulletin (döngüsel import olmasın diye tip verilmedi)."""
    local = b.created.astimezone(s.tz)
    mins, secs = divmod(b.duration, 60)
    return (f"🎙️ <b>{escape(radio_name(s.sports))}</b> · {local.day} {TR_MONTHS[local.month - 1]} "
            f"{TR_DAYS[local.weekday()]} · {mins}:{secs:02d}")


def help_text(s: Settings) -> str:
    sports = s.sports
    multi = len(sports) > 1
    arg = " <i>[" + "|".join(sports) + "]</i>" if multi else ""
    title = " &amp; ".join(SPORTS[sp][1] for sp in sports)
    lines = [f"{''.join(SPORTS[sp][0] for sp in sports)} <b>{title} Bot</b>", ""]
    if "mma" in sports:
        orgs = " · ".join(f"<code>{o.key}</code>" for o in ORGS.values())
        lines += (["<b>🥊 MMA</b>"] if multi else []) + [
            "/kart <i>[org]</i> — Yaklaşan etkinliğin maç kartı ve saatleri",
            "/sonuclar <i>[org]</i> — Son 5 etkinlik; seçtiğinin sonuçları (TKO · SUB - RNC · U-DEC, raunt/süre)",
            "/siralamalar <i>[org] [siklet]</i> — Şampiyon ve ilk 10 (ufc, one)",
            f"<i>Organizasyonlar:</i> {orgs}", ""]
    if "f1" in sports:
        if multi:
            lines += ["<b>🏎️ F1</b>", "/f1 — Sıradaki Grand Prix programı, son yarış, puan durumları", ""]
        else:
            lines += ["/gp — Sıradaki Grand Prix ve seans programı",
                      "/sonuclar — Son yarışın sonuçları",
                      "/pilotlar — Pilotlar şampiyonası · /takimlar — Takımlar şampiyonası",
                      "/f1 — Hepsi için menü", ""]
    lines += (["<b>Genel</b>"] if multi else []) + [
        f"/haberler{arg} — Son 24 saatin haberleri (Türkçe)",
        f"/radyo — Günün sesli bülteni: {escape(radio_name(sports))} 🎙️",
        f"/abone{arg} — Bildirimleri aç · /ayril{arg} — kapat", ""]
    reminders = []
    if "mma" in sports:
        reminders.append("MMA dövüş günlerinde ana karttan 1 saat önce")
    if "f1" in sports:
        reminders.append("F1'de sprint sıralama, sprint, sıralama ve yarıştan 30 dk önce")
    lines.append("<b>Bildirimler:</b> " + ("abone olduğun sporun " if multi else "")
                 + "tüm haberler Türkçe özetiyle anında gelir; " + ", ".join(reminders)
                 + " hatırlatma; her sabah sesli bülten.")
    return "\n".join(lines)