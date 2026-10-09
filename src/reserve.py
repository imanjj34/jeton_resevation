"""
reserve.py - انتخاب غذا و ثبت رزرو (و تغییر/لغو رزروهای فعلی) در سامانه.

جریان کار (انتخاب در صفحه‌ی وب: web_select.py):
    choices = ...                                 # از صفحه‌ی HTML می‌آید
    delta = price_delta(menu, choices)            # جمع تغییر اعتبار
    اگر delta <= menu.credit :  submit(session, choices)
    وگرنه                    :  save_pending(choices)؛ بعد از افزایش اعتبار دوباره ثبت می‌شود (بدون پایش مداوم)

submit() دقیقاً مثل مرورگر عمل می‌کند: برای هر ردیف اول سلف را عوض می‌کند (postback)، بعد غذا را،
بعد تیک رزرو را می‌گذارد و در پایان دکمه‌ی «ذخیره»ی همان وعده را می‌زند؛ سپس نتیجه را از صفحه‌ی
برگشتی دوباره می‌خواند و با خواسته‌ی تو مقایسه می‌کند.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from bs4 import BeautifulSoup

from config import cfg
from food_menu import WEEK_BUTTONS, DayEntry, Meal, WeekMenu, _form_payload, _post, parse_page



# ----------------------------------------------------------------- مدل
@dataclass
class Choice:
    date: str                  # 1405/07/14
    meal: str                  # Breakfast / Lunch / Dinner
    reserve: bool              # False = لغو رزرو
    self_id: Optional[str] = None
    food_id: Optional[str] = None
    # فقط برای نمایش
    meal_title: str = ""
    weekday: str = ""
    self_name: str = ""
    food_name: str = ""
    price: int = 0


def find_day(menu: WeekMenu, meal_key: str, date: str) -> Optional[DayEntry]:
    return next((d for m in menu.meals if m.key == meal_key for d in m.days if d.date == date), None)


def find_meal(menu: WeekMenu, meal_key: str) -> Optional[Meal]:
    return next((m for m in menu.meals if m.key == meal_key), None)


def _is_noop(day: DayEntry, ch: Choice) -> bool:
    if not ch.reserve:
        return not day.reserved
    return bool(day.reserved and day.selected_self and day.selected_food
                and day.selected_self.id == ch.self_id and day.selected_food.id == ch.food_id)


# ------------------------------------------------------------- محاسبه‌ی قیمت
def price_delta(menu: WeekMenu, choices: list[Choice]) -> int:
    """
    تغییر خالص اعتبار (ریال): مثبت = باید پرداخت شود، منفی = برگشت پول.
    فرض: رزرو فعلی که عوض یا لغو شود، مبلغش (ستون «مبلغ خرید») برمی‌گردد.
    """
    total = 0
    for ch in choices:
        day = find_day(menu, ch.meal, ch.date)
        if day is None:
            raise LookupError(f"{ch.date} / {ch.meal} در این هفته پیدا نشد")
        if _is_noop(day, ch):
            continue
        old = (day.amount or 0) if day.reserved else 0
        new = ch.price if ch.reserve else 0
        total += new - old
    return total


def summary_lines(menu: WeekMenu, choices: list[Choice]) -> list[str]:
    lines = []
    for ch in choices:
        day = find_day(menu, ch.meal, ch.date)
        head = f"{ch.weekday} {ch.date} - {ch.meal_title}: "
        old = f" (رزرو فعلی {day.amount:,} ریال برمی‌گردد)" if day and day.reserved and day.amount else ""
        if ch.reserve:
            lines.append(f"{head}{ch.self_name} / {ch.food_name} ({ch.price:,} ریال){old}")
        else:
            lines.append(f"{head}لغو رزرو{old}")
    return lines


# ---------------------------------------------------------------- ثبت در سایت
def _step(session, url: str, html: str, ticks: dict, **fields) -> str:
    """یک postback مثل مرورگر؛ تیک‌های رزرو (که سمت کلاینت‌اند) هم به فرم اضافه می‌شوند."""
    payload = _form_payload(BeautifulSoup(html, "lxml"))
    for name, on in ticks.items():
        if on:
            payload[name] = "on"
        else:
            payload.pop(name, None)
    return _post(session, url, payload, **fields)


def _open_week(session, url: str, dates: set) -> str:
    """هفته‌ای (جاری یا بعد) که همه‌ی تاریخ‌های انتخاب‌شده در آن است را باز می‌کند."""
    r = session.get(url)
    r.raise_for_status()
    html = r.text
    if "txtUsernamePlain" in html:
        raise PermissionError("session لاگین نیست یا منقضی شده")
    for attempt in range(2):
        menu = parse_page(html)
        if dates <= {d.date for m in menu.meals for d in m.days}:
            return html
        if attempt == 0:
            btn = f"ctl00$cphMain${WEEK_BUTTONS['next']}"
            html = _step(session, url, html, {}, **{f"{btn}.x": "1", f"{btn}.y": "1"})
    raise LookupError("تاریخ‌های انتخاب‌شده در هفته‌ی جاری و هفته‌ی بعد پیدا نشدند")


def read_credit(session, url: Optional[str] = None) -> Optional[int]:
    """«اعتبار شما» را از صفحه‌ی رزرو می‌خواند (برای پایش پرداخت)."""
    r = session.get(url or cfg.reservation_url)
    r.raise_for_status()
    if "txtUsernamePlain" in r.text:
        raise PermissionError("session لاگین نیست یا منقضی شده")
    return parse_page(r.text).credit


def open_week_state(session, choices: list[Choice], url: Optional[str] = None) -> tuple[WeekMenu, str]:
    """صفحه‌ی هفته‌ی شامل این انتخاب‌ها را یک بار باز می‌کند؛ (وضعیت، html) برمی‌گرداند. html را می‌شود به submit داد."""
    html = _open_week(session, url or cfg.reservation_url, {c.date for c in choices})
    return parse_page(html), html


def current_state(session, choices: list[Choice], url: Optional[str] = None) -> WeekMenu:
    """وضعیت فعلی صفحه (اعتبار و رزروها) برای هفته‌ی شامل این انتخاب‌ها."""
    return open_week_state(session, choices, url)[0]


def _apply_row(session, url, html, ticks, ch, say):
    """
    مراحل یک ردیف (یک روز از یک وعده) مثل کاربر واقعی، با راستی‌آزمایی بعد از هر مرحله:
      ۱) انتخاب سلف  ۲) انتخاب غذا  ۳) روشن کردن تیک (یا خاموش کردن برای لغو)
    خروجی: (html جدید، متن خطا یا None، نام فیلد تیک)
    """
    tag = f"  {ch.weekday} {ch.date}"
    day = find_day(parse_page(html), ch.meal, ch.date)
    if day is None or not day.active or day.locked:
        say(f"{tag} ✗ روز قفل یا غیرفعال است")
        return html, "روز قفل یا غیرفعال است", None
    f = day.fields

    if ch.reserve:
        if not day.selected_self or day.selected_self.id != ch.self_id:
            html = _step(session, url, html, ticks, __EVENTTARGET=f["self"], __EVENTARGUMENT="", **{f["self"]: ch.self_id})
            day = find_day(parse_page(html), ch.meal, ch.date)
        if not (day and day.selected_self and day.selected_self.id == ch.self_id):
            now = day.selected_self.name if day and day.selected_self else "؟"
            say(f"{tag} ✗ سلف «{ch.self_name}» انتخاب نشد؛ سامانه «{now}» را نشان می‌دهد")
            return html, f"سلف «{ch.self_name}» انتخاب نشد؛ سامانه «{now}» را نشان می‌دهد", None
        say(f"{tag} ← ۱) سلف «{ch.self_name}» ✓")

        if not any(x.id == ch.food_id for x in day.selected_self.foods):
            say(f"{tag} ✗ غذای «{ch.food_name}» دیگر در لیست نیست")
            return html, f"غذای «{ch.food_name}» دیگر در لیست نیست", None
        if not day.selected_food or day.selected_food.id != ch.food_id:
            html = _step(session, url, html, ticks, __EVENTTARGET=f["food"], __EVENTARGUMENT="", **{f["food"]: ch.food_id})
            day = find_day(parse_page(html), ch.meal, ch.date)
        if not (day and day.selected_food and day.selected_food.id == ch.food_id):
            now = day.selected_food.name if day and day.selected_food else "؟"
            say(f"{tag} ✗ غذای «{ch.food_name}» انتخاب نشد؛ سامانه «{now}» را نشان می‌دهد")
            return html, f"غذای «{ch.food_name}» انتخاب نشد؛ سامانه «{now}» را نشان می‌دهد", None
        say(f"{tag} ← ۲) غذای «{ch.food_name}» ✓")
        ticks[f["chk"]] = True
        say(f"{tag} ← ۳) تیک رزرو روشن شد ✓")
    else:
        ticks[f["chk"]] = False
        say(f"{tag} ← تیک رزرو خاموش شد (لغو رزرو) ✓")
    return html, None, f["chk"]


def describe_state(day: Optional[DayEntry]) -> str:
    """آنچه سامانه همین الان برای این روز/وعده ثبت کرده است (برای پیام‌های خطا)."""
    if day is None:
        return "این روز در صفحه دیده نشد"
    if not day.reserved:
        return "رزرو نشده"
    sv, fd = day.selected_self, day.selected_food
    txt = "رزرو شده"
    if sv and fd:
        txt += f": {sv.name} / {fd.name}"
    if day.amount:
        txt += f" ({day.amount:,} ریال)"
    return txt


def describe_want(ch: Choice) -> str:
    return "لغو رزرو" if not ch.reserve else f"{ch.self_name} / {ch.food_name} ({ch.price:,} ریال)"


def _row_ok(menu: WeekMenu, ch: Choice) -> bool:
    """آیا سلف و غذای این ردیف در صفحه‌ی فعلی همان است که خواسته‌ایم؟"""
    d = find_day(menu, ch.meal, ch.date)
    if not ch.reserve:
        return d is not None
    return bool(d and d.selected_self and d.selected_food
                and d.selected_self.id == ch.self_id and d.selected_food.id == ch.food_id)


def submit(session, choices: list[Choice], url: Optional[str] = None, on_step: Optional[Callable[[str], None]] = None,
           html: Optional[str] = None) -> list[dict]:
    """
    ثبت انتخاب‌ها روی سایت، وعده به وعده و مرحله به مرحله:
        برای هر روزِ یک وعده: انتخاب سلف ← انتخاب غذا ← تیک رزرو
        بعد از تکمیل همه‌ی روزهای آن وعده: بررسی دوباره‌ی فرم ← دکمه‌ی «ذخیره»ی همان وعده ← راستی‌آزمایی
        سپس وعده‌ی بعدی.
    ردیفی که سلف/غذایش تأیید نشود هرگز تیک نمی‌خورد، پس غذای اشتباه رزرو نمی‌شود.
    on_step(متن) برای نمایش زنده‌ی پیشرفت صدا زده می‌شود.
    html: اگر صفحه‌ی هفته همین چند لحظه پیش با open_week_state باز شده، همان را بده تا دوباره باز نشود (سریع‌تر).
    خروجی: برای هر وعده {meal, ok, message, problems, credit}. ok فقط وقتی True است که صفحه‌ی برگشتی
    دقیقاً همان وضعیت خواسته‌شده را نشان بدهد.
    """
    say = on_step or (lambda _m: None)
    url = url or cfg.reservation_url
    if html is None:
        html = _open_week(session, url, {c.date for c in choices})
    before = parse_page(html)        # وضعیت و اعتبار پیش از ثبت (برای بررسی نهایی)
    results = []
    verified: list[Choice] = []      # انتخاب‌هایی که هر وعده‌شان بعد از ذخیره تأیید شد
    for meal_key in dict.fromkeys(c.meal for c in choices):
        chs = [c for c in choices if c.meal == meal_key]
        say(f"▶ وعده {chs[0].meal_title or meal_key}")
        ticks: dict[str, bool] = {}
        problems: list[str] = []
        applied: list[tuple[Choice, str]] = []

        for ch in chs:
            html, err, chk = _apply_row(session, url, html, ticks, ch, say)
            if err:
                problems.append(f"{ch.weekday} {ch.date}: {err}")
            else:
                applied.append((ch, chk))

        # قبل از ذخیره: اگر سایت انتخاب ردیفی را از فرم پاک کرده بود، دوباره (حداکثر دو بار) اعمال می‌شود
        for _ in range(2):
            current = parse_page(html)
            lost = [(ch, chk) for ch, chk in applied if not _row_ok(current, ch)]
            if not lost:
                break
            for ch, chk in lost:
                say(f"  {ch.weekday} {ch.date} ↻ انتخاب از فرم پاک شده بود؛ دوباره اعمال می‌شود")
                html, err, _ = _apply_row(session, url, html, ticks, ch, say)
                if err:
                    ticks.pop(chk, None)
                    applied.remove((ch, chk))
                    problems.append(f"{ch.weekday} {ch.date}: {err}")
        current = parse_page(html)
        for ch, chk in list(applied):  # آخرین بررسی؛ تأییدنشده‌ها تیک نمی‌خورند
            if not _row_ok(current, ch):
                ticks.pop(chk, None)
                applied.remove((ch, chk))
                problems.append(f"{ch.weekday} {ch.date}: انتخاب روی فرم پایدار نماند؛ ثبت نشد")
                say(f"  {ch.weekday} {ch.date} ✗ انتخاب روی فرم پایدار نماند؛ ثبت نمی‌شود")

        if not applied:
            say(f"  هیچ ردیفی برای ذخیره‌ی {chs[0].meal_title or meal_key} آماده نبود ✗")
            results.append({"meal": meal_key, "ok": False, "message": None, "problems": problems})
            continue

        meal = find_meal(current, meal_key)
        say(f"  ذخیره‌ی {meal.title} ...")
        html = _step(session, url, html, ticks, **{meal.save_name: meal.save_value})
        after = parse_page(html)

        for ch, _chk in applied:  # راستی‌آزمایی از روی صفحه‌ی برگشتی
            d = find_day(after, ch.meal, ch.date)
            ok = bool(d) and d.reserved == ch.reserve and _row_ok(after, ch)
            say(f"  {ch.weekday} {ch.date} " + ("✓ ثبت شد" if ok else f"✗ خواسته: {describe_want(ch)} ← سامانه ثبت کرده: {describe_state(d)}"))
            if not ok:
                problems.append(f"{ch.weekday} {ch.date}: خواسته‌ی تو «{describe_want(ch)}»؛ سامانه ثبت کرده «{describe_state(d)}»")
        if after.message:
            say(f"  پیام سایت: {after.message}")
        results.append({"meal": meal_key, "ok": not problems, "message": after.message, "problems": problems,
                        "credit": after.credit})
        verified += [ch for ch, _chk in applied if _row_ok(after, ch) and find_day(after, ch.meal, ch.date).reserved == ch.reserve]

    if verified:
        results.append(_final_check(session, url, before, verified, say))
    return results


def _final_check(session, url: str, before: WeekMenu, verified: list[Choice], say) -> dict:
    """
    بررسی نهایی: بعد از ذخیره‌ی همه‌ی وعده‌ها، صفحه‌ی هفته یک بار دیگر تازه از سایت خوانده می‌شود
    (نه از پاسخ ذخیره) و همه‌ی تغییرها دوباره با آن مقایسه می‌شود؛ اعتبار نهایی هم با پیش‌بینی سنجیده می‌شود.
    """
    say("▶ بررسی نهایی از روی سایت")
    problems: list[str] = []
    warnings: list[str] = []
    credit = None
    try:
        final = parse_page(_open_week(session, url, {c.date for c in verified}))
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ خواندن دوباره‌ی صفحه ناموفق بود: {type(e).__name__}: {e}")
        return {"meal": "بررسی نهایی", "ok": False, "message": None, "credit": None, "warnings": [],
                "problems": [f"خواندن دوباره‌ی صفحه ناموفق بود: {type(e).__name__}: {e}"]}
    for ch in verified:
        d = find_day(final, ch.meal, ch.date)
        ok = d is not None and d.reserved == ch.reserve and _row_ok(final, ch)
        say(f"  {ch.weekday} {ch.date} · {ch.meal_title}: " + ("✓ تأیید شد" if ok else f"✗ سامانه ثبت کرده: {describe_state(d)}"))
        if not ok:
            problems.append(f"{ch.weekday} {ch.date} ({ch.meal_title}): خواسته‌ی تو «{describe_want(ch)}»؛ سامانه ثبت کرده «{describe_state(d)}»")
        elif ch.reserve and d.amount and ch.price and d.amount != ch.price:   # غذا درست است ولی مبلغ ثبت‌شده فرق دارد
            warnings.append(f"{ch.weekday} {ch.date} ({ch.meal_title}): مبلغ ثبت‌شده در سامانه {d.amount:,} ریال است (قیمت غذا {ch.price:,})")
            say(f"  ؟ {warnings[-1]}")
    credit = final.credit
    try:
        expected = (before.credit or 0) - price_delta(before, verified)
        if before.credit is not None and credit is not None and credit != expected:
            warnings.append(f"اعتبار نهایی {credit:,} با پیش‌بینی {expected:,} ریال نمی‌خواند؛ در سایت چک کن")
            say(f"  ؟ {warnings[-1]}")
    except LookupError:
        pass
    if credit is not None:
        say(f"  اعتبار نهایی: {credit:,} ریال")
    return {"meal": "بررسی نهایی", "ok": not problems, "message": None, "problems": problems, "warnings": warnings,
            "credit": credit}


# ------------------------------------------------------ ذخیره‌ی انتخاب‌ها (موجودی کم)
def save_pending(choices: list[Choice], path: Optional[str] = None) -> None:
    with open(path or cfg.pending_file, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in choices], f, ensure_ascii=False, indent=2)


def load_pending(path: Optional[str] = None) -> list[Choice]:
    try:
        with open(path or cfg.pending_file, encoding="utf-8") as f:
            return [Choice(**c) for c in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def clear_pending(path: Optional[str] = None) -> None:
    import os
    path = path or cfg.pending_file
    if os.path.exists(path):
        os.remove(path)
