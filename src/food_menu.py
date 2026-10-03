"""
food_menu.py - استخراج برنامه غذایی از Reservation.aspx (فقط با requests؛ بدون سلنیوم).

    menu = fetch_menu(session, week="current")                 # هفته جاری
    menu = fetch_menu(session, week="next", all_selves=True)   # هفته بعد، غذاهای همه‌ی سلف‌ها
    if menu.has_food: ...

نکته: صفحه برای هر روز فقط غذاهای سلفِ انتخاب‌شده را می‌دهد. با all_selves=True برای هر
(وعده، روز، سلف دیگر) یک postback زده می‌شود تا غذاهای بقیه‌ی سلف‌ها هم پر شود.
"""
from __future__ import annotations

import re
from html import escape
from dataclasses import asdict, dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

RESERVATION_URL = "https://jeton.yazd.ac.ir/Reservation/Reservation.aspx"
WEEK_BUTTONS = {"next": "imgbtnNextWeek"}  # "current" = همان GET اولیه
TABLE_ID_RE = re.compile(r"^cphMain_grdReservation(\w+)$")
FOOD_RE = re.compile(r"^(?P<name>.*?)\s+(?P<price>[\d,٬]+)\s*ریال\s*$")
PLACEHOLDER = "-1"  # روزهای غیرفعال (جمعه و ...)


# ----------------------------------------------------------------- مدل‌ها
@dataclass
class Food:
    id: str
    name: str
    price: Optional[int]  # ریال
    selected: bool = False


@dataclass
class Self_:
    id: str
    name: str
    selected: bool = False
    foods: list[Food] = field(default_factory=list)


@dataclass
class DayEntry:
    weekday: str
    date: str          # 1405/07/13
    active: bool       # False = روز غیرفعال/تعطیل
    locked: bool       # ردیف غیرقابل ویرایش
    reserved: bool
    status: Optional[str]
    selves: list[Self_] = field(default_factory=list)

    @property
    def has_food(self) -> bool:
        return any(s.foods for s in self.selves)


@dataclass
class Meal:
    key: str    # Breakfast / Lunch / Dinner / ...
    title: str  # عنوان فارسی تب
    days: list[DayEntry] = field(default_factory=list)


@dataclass
class WeekMenu:
    week_range: Optional[str]
    message: Optional[str]  # پیام سامانه (alertBox)، اگر باشد
    meals: list[Meal] = field(default_factory=list)

    @property
    def has_food(self) -> bool:
        """False یعنی هنوز برای این هفته غذایی ثبت نشده."""
        return any(d.has_food for m in self.meals for d in m.days)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        out = [f"هفته: {self.week_range or '?'}"]
        if not self.has_food:
            out.append("هنوز غذایی برای این هفته ثبت نشده است." + (f" ({self.message})" if self.message else ""))
            return "\n".join(out)
        for m in self.meals:
            out.append(f"\n== {m.title} ==")
            for d in m.days:
                out.append(f"{d.weekday} {d.date}" + ("  [رزرو شده]" if d.reserved else "") + ("" if d.active else ": —"))
                for s in d.selves:
                    if s.foods:
                        foods = "، ".join(
                            f.name + (f" ({f.price:,})" if f.price else "") + ("*" if f.selected else "")
                            for f in s.foods
                        )
                        out.append(f"   {s.name}{'*' if s.selected else ''}: {foods}")
        return "\n".join(out)

    def to_html(self) -> str:
        """یک صفحه‌ی HTML مستقل و راست‌به‌چپ؛ با مرورگر باز کن یا چاپ بگیر."""
        e = escape
        parts = [
            '<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">',
            f"<title>برنامه غذایی {e(self.week_range or '')}</title>",
            "<style>"
            "body{font-family:Vazirmatn,Tahoma,sans-serif;max-width:900px;margin:24px auto;padding:0 12px;color:#222}"
            "h1{font-size:20px;margin:0 0 8px;overflow:visible}h2{font-size:17px;margin:28px 0 8px;border-right:4px solid #2e7d32;padding-right:8px}"
            "table{width:100%;border-collapse:collapse}th,td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:right}"
            "th{background:#f0f0f0}.day{white-space:nowrap;font-weight:bold}.off{color:#999;background:#fafafa}"
            ".res td.day,.res td.self{background:#e8f5e9}.sel{font-weight:bold;color:#1b5e20}"
            ".self{white-space:nowrap}.price{color:#777;font-size:12px}.food{display:inline-block;margin:2px 0 2px 12px}"
            ".note{background:#fff8e1;border:1px solid #ffe082;padding:10px;border-radius:6px}.legend{color:#666;font-size:12px}"
            "</style></head><body>",
            f"<h1>برنامه غذایی هفته <bdi>{e(self.week_range or '')}</bdi></h1>",
        ]
        if self.message:
            parts.append(f'<p class="note">{e(self.message)}</p>')
        if not self.has_food:
            parts.append('<p class="note">هنوز غذایی برای این هفته ثبت نشده است.</p>')
        else:
            parts.append('<p class="legend">سبز = رزرو شده &nbsp;|&nbsp; پررنگ = غذای انتخاب‌شده</p>')
            for m in self.meals:
                parts.append(f"<h2>{e(m.title)}</h2><table><tr><th>روز</th><th>سلف</th><th>غذاها</th></tr>")
                for d in m.days:
                    day = f"{e(d.weekday)}<br><small>{e(d.date)}</small>"
                    rows = [s for s in d.selves if s.foods] or [None]
                    for i, s in enumerate(rows):
                        cls = "res" if d.reserved else ("off" if not d.active else "")
                        parts.append(f'<tr class="{cls}">')
                        if i == 0:
                            parts.append(f'<td class="day" rowspan="{len(rows)}">{day}</td>')
                        if s is None:
                            parts.append('<td colspan="2">—</td></tr>')
                            continue
                        foods = "".join(
                            f'<span class="food{" sel" if f.selected else ""}">{e(f.name)}'
                            + (f' <span class="price">{f.price:,}</span>' if f.price else "")
                            + "</span>"
                            for f in s.foods
                        )
                        parts.append(f'<td class="self">{e(s.name)}{" ✓" if s.selected else ""}</td><td>{foods}</td></tr>')
                parts.append("</table>")
        parts.append("</body></html>")
        return "\n".join(parts)


# ----------------------------------------------------------------- پارس
def _int(text: str) -> Optional[int]:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def _parse_row(tr) -> Optional[DayEntry]:
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 7:
        return None
    m = re.match(r"^(.+?)\s+(\d{4}/\d{2}/\d{2})$", tds[1].get_text(" ", strip=True))
    weekday, date = (m[1], m[2]) if m else (tds[1].get_text(strip=True), "")

    self_sel, food_sel = tds[2].find("select"), tds[3].find("select")
    chk = tds[0].find("input", type="checkbox")
    img = tds[7].find("img") if len(tds) > 7 else None

    foods = []
    for o in (food_sel.find_all("option") if food_sel else []):
        if o["value"] == PLACEHOLDER:
            continue
        text = o.get_text(" ", strip=True)
        fm = FOOD_RE.match(text)
        foods.append(Food(o["value"], fm["name"].strip() if fm else text,
                          _int(fm["price"]) if fm else None, o.has_attr("selected")))

    selves = []
    for o in (self_sel.find_all("option") if self_sel else []):
        if o["value"] == PLACEHOLDER:
            continue
        sel = o.has_attr("selected")
        selves.append(Self_(o["value"], o.get_text(strip=True), sel, foods if sel else []))

    return DayEntry(weekday, date, bool(selves), bool(chk and chk.has_attr("disabled")),
                    bool(chk and chk.has_attr("checked")), img.get("title") if img else None, selves)


def parse_page(html: str) -> WeekMenu:
    soup = BeautifulSoup(html, "lxml")
    week = soup.find(id="cphMain_lblWeekDate")
    msg = soup.find(id="cphMain_alertBoxValue")
    menu = WeekMenu(
        week_range=" ".join(week.get_text().split()) if week else None,
        message=msg.get_text(strip=True) or None if msg else None,
    )
    for table in soup.find_all("table", id=TABLE_ID_RE):
        label = table.find_parent("li").find("label") if table.find_parent("li") else None
        meal = Meal(TABLE_ID_RE.match(table["id"])[1], label.get_text(strip=True) if label else table["id"])
        meal.days = [d for d in map(_parse_row, table.find_all("tr")[1:]) if d]
        menu.meals.append(meal)
    return menu


# --------------------------------------------------------- درخواست‌ها (requests)
def _form_payload(soup: BeautifulSoup) -> dict:
    """همه‌ی فیلدهای فرم، مثل ارسال توسط مرورگر (ViewState و ...)."""
    data: dict[str, str] = {}
    for el in soup.find("form").find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "input":
            t = (el.get("type") or "text").lower()
            if t in ("submit", "button", "image", "file"):
                continue
            if t in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            data[name] = el.get("value", "on" if t == "checkbox" else "")
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            if opt is not None:
                data[name] = opt.get("value", opt.get_text())
        else:
            data[name] = el.get_text()
    return data


def _post(session, url: str, html: str, **fields) -> str:
    data = _form_payload(BeautifulSoup(html, "lxml"))
    data.update(fields)
    r = session.post(url, data=data)
    r.raise_for_status()
    return r.text


def fetch_menu(session, week: str = "current", all_selves: bool = False, url: str = RESERVATION_URL) -> WeekMenu:
    """
    session    : requests.Session لاگین‌شده
    week       : "current" یا "next"
    all_selves : غذاهای همه‌ی سلف‌ها (درخواست بیشتر)
    اگر برنامه‌ی هفته‌ی بعد هنوز ثبت نشده باشد، menu.has_food برابر False است.
    """
    if week not in ("current", "next"):
        raise ValueError("week must be 'current' or 'next'")

    r = session.get(url)
    r.raise_for_status()
    html = r.text
    if "txtUsernamePlain" in html:  # صفحه‌ی لاگین برگشته
        raise PermissionError("session لاگین نیست یا منقضی شده")

    if week == "next":
        btn = f"ctl00$cphMain${WEEK_BUTTONS['next']}"
        html = _post(session, url, html, **{f"{btn}.x": "1", f"{btn}.y": "1"})

    menu = parse_page(html)
    if not all_selves or not menu.has_food:
        return menu

    soup = BeautifulSoup(html, "lxml")
    for meal in menu.meals:
        table = next(t for t in soup.find_all("table", id=TABLE_ID_RE) if TABLE_ID_RE.match(t["id"])[1] == meal.key)
        for day, tr in zip(meal.days, table.find_all("tr")[1:]):
            if not day.active or day.locked:
                continue
            target = tr.find("select", id=re.compile(r"_drpSelf_\d+$"))["name"]
            for s in day.selves:
                if s.selected:
                    continue
                page = parse_page(_post(session, url, html, __EVENTTARGET=target, __EVENTARGUMENT="", **{target: s.id}))
                d2 = next(d for m in page.meals if m.key == meal.key for d in m.days if d.date == day.date)
                if any(x.selected and x.id == s.id for x in d2.selves):
                    s.foods = next(x.foods for x in d2.selves if x.id == s.id)
    return menu