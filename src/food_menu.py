"""
food_menu.py - استخراج برنامه غذایی از Reservation.aspx (فقط با requests؛ بدون سلنیوم).

    menu = fetch_menu(session, week="current")                 # هفته جاری
    menu = fetch_menu(session, week="next", all_selves=True)   # هفته بعد، غذاهای همه‌ی سلف‌ها
    if menu.has_food: ...

نکته: صفحه برای هر روز فقط غذاهای سلفِ انتخاب‌شده را می‌دهد. با all_selves=True برای هر
(وعده، روز، سلف دیگر) یک postback زده می‌شود تا غذاهای بقیه‌ی سلف‌ها هم پر شود.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html import escape
from dataclasses import asdict, dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import cfg
from ui import GRID_CSS, THEME_BUTTON, THEME_TOGGLE_JS, base_css, theme_boot

logger = logging.getLogger(__name__)
WEEK_BUTTONS = {"next": "imgbtnNextWeek"}  # "current" = همان GET اولیه
TABLE_ID_RE = re.compile(r"^cphMain_grdReservation(\w+)$")
FOOD_RE = re.compile(r"^(?P<name>.*?)\s+(?P<price>[\d,٬]+)\s*ریال\s*$")
DEFAULT_TRANSPORT = None  # فقط برای تست: httpx.MockTransport
PLACEHOLDER = "-1"  # روزهای غیرفعال (جمعه و ...)


# فایل منو: سربرگ هر وعده (بدون JS همه‌ی وعده‌ها پشت‌سرهم دیده می‌شوند)
_MENU_CSS = """
.f{padding:3px 8px;border-radius:6px}.f.cur{font-weight:700}
.f.reg{box-shadow:inset 0 0 0 2px var(--leaf);background:var(--leaf-soft)}
.alert{margin:6px 0 12px}
.js .panel h2{display:none}
@media print{#tabs,#theme{display:none}.panel{display:block !important}.js .panel h2{display:block}body{background:#fff}}
"""

_MENU_JS = """(function(){
  var panels = document.querySelectorAll(".panel"), box = document.getElementById("tabs");
  if(!panels.length || !box) return;
  document.body.className += " js";
  function show(k){
    for(var j=0;j<panels.length;j++){
      panels[j].style.display = (j===k) ? "block" : "none";
      box.children[j].className = "tabbtn" + (j===k ? " on" : "");
      box.children[j].setAttribute("aria-selected", j===k ? "true" : "false");
    }
  }
  for(var i=0;i<panels.length;i++){
    var b = document.createElement("button"); b.type = "button"; b.className = "tabbtn";
    b.textContent = panels[i].getAttribute("data-title");
    b.onclick = (function(k){ return function(){ show(k); }; })(i);
    box.appendChild(b);
  }
  show(0);
})();"""


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
    amount: Optional[int] = None                       # مبلغ خریداری‌شده‌ی فعلی (ریال)
    fields: dict = field(default_factory=dict)          # نام فیلدهای فرم: chk / self / food (برای ثبت رزرو)

    @property
    def selected_self(self) -> Optional[Self_]:
        return next((s for s in self.selves if s.selected), None)

    @property
    def selected_food(self) -> Optional[Food]:
        s = self.selected_self
        return next((f for f in s.foods if f.selected), None) if s else None

    @property
    def has_food(self) -> bool:
        return any(s.foods for s in self.selves)


@dataclass
class Meal:
    key: str    # Breakfast / Lunch / Dinner / ...
    title: str  # عنوان فارسی تب
    days: list[DayEntry] = field(default_factory=list)
    save_name: Optional[str] = None   # دکمه‌ی «ذخیره»ی همین وعده
    save_value: Optional[str] = None


@dataclass
class WeekMenu:
    week_range: Optional[str]
    message: Optional[str]  # پیام سامانه (alertBox)، اگر باشد
    meals: list[Meal] = field(default_factory=list)
    credit: Optional[int] = None  # «اعتبار شما» (ریال)
    account_name: Optional[str] = None  # نامی که سایت در هدر برای حسابِ واردشده نشان می‌دهد
    account_id: Optional[str] = None    # «شماره شناسایی» همان حساب (بازخورد سایت)

    @property
    def has_food(self) -> bool:
        """False یعنی هنوز برای این هفته غذایی ثبت نشده."""
        return any(d.has_food for m in self.meals for d in m.days)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("account_name", None)   # مشخصات حساب داخل فایل خروجی نمی‌رود
        d.pop("account_id", None)
        return d

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
                        out.append(f"   {s.name}:")
                        for f in s.foods:
                            out.append(f"      - {f.name}" + (f" ({f.price:,})" if f.price else "") + (" ✓" if f.selected else ""))
        return "\n".join(out)

    def to_html(self) -> str:
        """صفحه‌ی HTML مستقل و راست‌به‌چپ: هر وعده یک سربرگ، در هر روز هر سلف ستون خودش را دارد. تم روشن/تاریک دارد."""
        e = escape
        out = [
            '<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">',
            f"<title>برنامه غذایی {e(self.week_range or '')}</title>", theme_boot(),
            f"<style>{base_css()}{GRID_CSS}{_MENU_CSS}</style></head><body><div class=\"wrap\">",
            f'<header class="top"><div><span class="eyebrow">برنامه غذایی</span><h1><bdi>{e(self.week_range or "")}</bdi></h1></div>'
            f'<div class="tools">{THEME_BUTTON}</div></header>',
        ]
        if self.message:
            out.append(f'<div class="alert info">{e(self.message)}</div>')
        if not self.has_food:
            out.append('<div class="alert info">هنوز غذایی برای این هفته ثبت نشده است.</div>')
        else:
            out.append('<nav id="tabs" role="tablist"></nav>')
            for mi, m in enumerate(self.meals):
                cols: list = []  # ستون‌ها: هر سلفی که در این وعده در یک روز غذا دارد (به ترتیب دیده‌شدن)
                for d in m.days:
                    for sv in d.selves:
                        if sv.foods and sv.id not in [c[0] for c in cols]:
                            cols.append((sv.id, sv.name))
                head = "".join(f"<th>{e(n)}</th>" for _, n in cols)
                out.append(f'<section class="panel" id="panel_{mi}" data-title="{e(m.title)}"><h2>{e(m.title)}</h2>'
                           f'<div class="gridwrap"><table class="grid"><thead><tr><th class="dayh">روز</th>{head}</tr></thead><tbody>')
                for d in m.days:
                    badge = (f'<span class="badge">رزرو شده{" · " + e(d.selected_self.name) if d.selected_self else ""}</span>') if d.reserved else ""
                    day = f'<td class="day"><strong>{e(d.weekday)}</strong><span class="dt">{e(d.date)}</span>{badge}</td>'
                    cls = "res" if d.reserved else ("off" if not d.active else "")
                    if not d.active or not cols:
                        out.append(f'<tr class="{cls}">{day}<td class="msg" colspan="{max(len(cols), 1)}">—</td></tr>')
                        continue
                    cells = []
                    for sid, _ in cols:
                        sv = next((x for x in d.selves if x.id == sid and x.foods), None)
                        if sv is None:
                            cells.append('<td class="cell empty">·</td>')
                            continue
                        foods = "".join(
                            f'<div class="f{" cur" if f.selected else ""}{" reg" if d.reserved and sv.selected and f.selected else ""}">- {e(f.name)}'
                            + (f' <span class="pr num">{f.price:,}</span>' if f.price else "")
                            + ('<span class="tick">✓</span>' if f.selected else "") + "</div>"
                            for f in sv.foods
                        )
                        cells.append(f'<td class="cell">{foods}</td>')
                    out.append(f'<tr class="{cls}">{day}{"".join(cells)}</tr>')
                out.append("</tbody></table></div></section>")
        out.append(f"</div><script>{THEME_TOGGLE_JS}\n{_MENU_JS}</script></body></html>")
        return "".join(out)

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
                    bool(chk and chk.has_attr("checked")), img.get("title") if img else None, selves,
                    amount=_int(tds[6].get_text()),
                    fields={"chk": chk.get("name") if chk else None,
                            "self": self_sel.get("name") if self_sel else None,
                            "food": food_sel.get("name") if food_sel else None})


def _clean_text(el) -> Optional[str]:
    t = " ".join(el.get_text(" ", strip=True).split()) if el else ""
    return t or None


def parse_page(html: str) -> WeekMenu:
    soup = BeautifulSoup(html, "lxml")
    week = soup.find(id="cphMain_lblWeekDate")
    msg = soup.find(id="cphMain_alertBoxValue")
    credit = soup.find(id="cphMain_lblCreditValue")
    menu = WeekMenu(
        week_range=" ".join(week.get_text().split()) if week else None,
        message=msg.get_text(strip=True) or None if msg else None,
        credit=_int(credit.get_text()) if credit else None,
        account_name=_clean_text(soup.find(id="divUsername")),
        account_id=_clean_text(soup.find(id="lblIdNumber")),
    )
    for table in soup.find_all("table", id=TABLE_ID_RE):
        li = table.find_parent("li")
        label = li.find("label") if li else None
        meal = Meal(TABLE_ID_RE.match(table["id"])[1], label.get_text(strip=True) if label else table["id"])
        meal.days = [d for d in map(_parse_row, table.find_all("tr")[1:]) if d]
        btn = li.find("input", attrs={"type": "submit", "name": re.compile(r"btnSave")}) if li else None
        if btn:
            meal.save_name, meal.save_value = btn["name"], btn.get("value", "")
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


def _post(session, url: str, payload: dict, **fields) -> str:
    r = session.post(url, data={**payload, **fields})
    r.raise_for_status()
    return r.text


def fetch_menu(session, week: str = "current", all_selves: bool = True, url: Optional[str] = None,
               on_progress=None, method: Optional[str] = None, concurrency: Optional[int] = None,
               max_threads: Optional[int] = None, transport=None) -> WeekMenu:
    """
    session     : requests.Session لاگین‌شده
    week        : "current" یا "next"
    all_selves  : غذاهای همه‌ی سلف‌ها (پیش‌فرض True). ردیف‌های قفل‌شده (گذشته/رزرو قطعی)
                  فقط همان غذای رزروشده را دارند، چون سایت اجازه‌ی تغییر سلف در آن‌ها را نمی‌دهد.
    on_progress : تابع اختیاری f(انجام‌شده, کل) برای نمایش پیشرفت
    method      : روش دریافت (پیش‌فرض از config.ini → [fetch] method):
                  "threads"    برای هر وعده در هر روز یک thread؛ همه‌ی منو یک‌جا دریافت می‌شود
                  "async"      asyncio + httpx با حداکثر concurrency درخواست همزمان
                  "sequential" پشت‌سرهم
    max_threads : برای threads؛ 0 = به تعداد (وعده × روز) thread
    transport   : فقط برای تست (httpx.MockTransport)
    برای روزِ غیرفعال (یا روزی که حین کار غیرفعال شود) درخواستی فرستاده نمی‌شود.
    اگر برنامه‌ی هفته‌ی بعد هنوز ثبت نشده باشد، menu.has_food برابر False است.
    """
    if week not in ("current", "next"):
        raise ValueError("week must be 'current' or 'next'")
    url = url or cfg.reservation_url
    method = method or cfg.fetch_method
    if method not in ("threads", "async", "sequential"):
        raise ValueError("method must be threads | async | sequential")

    r = session.get(url)
    r.raise_for_status()
    html = r.text
    if "txtUsernamePlain" in html:  # صفحه‌ی لاگین برگشته
        raise PermissionError("session لاگین نیست یا منقضی شده")

    if week == "next":
        btn = f"ctl00$cphMain${WEEK_BUTTONS['next']}"
        payload = _form_payload(BeautifulSoup(html, "lxml"))
        html = _post(session, url, payload, **{f"{btn}.x": "1", f"{btn}.y": "1"})

    menu = parse_page(html)
    if not all_selves or not menu.has_food:
        return menu

    soup = BeautifulSoup(html, "lxml")
    payload = _form_payload(soup)  # یک بار؛ برای همه‌ی postbackها مشترک است
    dates = list(dict.fromkeys(d.date for m in menu.meals for d in m.days))
    jobs_by_date: dict[str, list] = {dt: [] for dt in dates}  # تاریخ -> [(meal, day, نام select سلف, سلف دیگر)]
    for meal in menu.meals:
        table = next(t for t in soup.find_all("table", id=TABLE_ID_RE) if TABLE_ID_RE.match(t["id"])[1] == meal.key)
        for day, tr in zip(meal.days, table.find_all("tr")[1:]):
            if day.active and not day.locked:
                target = tr.find("select", id=re.compile(r"_drpSelf_\d+$"))["name"]
                jobs_by_date[day.date] += [(meal, day, target, s) for s in day.selves if not s.selected]

    total = sum(map(len, jobs_by_date.values()))
    done = 0

    def advance(n: int = 1) -> None:
        nonlocal done
        done += n
        if on_progress:
            on_progress(done, total)

    if total == 0:
        return menu
    if method == "async" and total > 1 and _can_async():
        asyncio.run(_run_jobs_async(session, url, payload, jobs_by_date, advance,
                                    concurrency or cfg.fetch_concurrency, transport or DEFAULT_TRANSPORT))
    elif method in ("threads", "async") and total > 1:   # async بدون httpx هم به threads برمی‌گردد
        _run_jobs_threads(session, url, payload, jobs_by_date, advance,
                          cfg.fetch_max_threads if max_threads is None else max_threads)
    else:
        _run_jobs_sync(session, url, payload, jobs_by_date, advance)
    return menu


def _clone_session(session):
    """هر thread یک session مستقل (با همان کوکی‌ها) دارد؛ requests.Session کاملاً thread-safe نیست."""
    s = requests.Session()
    s.headers.update(session.headers)
    s.cookies.update(session.cookies)
    return s


def _run_jobs_threads(session, url, payload, jobs_by_date, advance, max_threads: int = 0) -> None:
    """
    برای هر «وعده در هر روز» یک thread؛ همه با هم شروع می‌کنند تا تمام منو یک‌جا دریافت شود.
    هر thread غذای سلف‌های دیگرِ همان وعده/روز را می‌گیرد. اگر روزی غیرفعال دیده شود، thread‌های همان روز زودتر تمام می‌شوند.
    """
    cells: dict = {}
    for date, jobs in jobs_by_date.items():
        for job in jobs:
            cells.setdefault((job[0].key, date), []).append(job)
    if not cells:
        return
    lock, abort, dead = threading.Lock(), threading.Event(), set()

    def safe_advance(n: int = 1) -> None:
        with lock:
            advance(n)

    def run_cell(date: str, jobs: list) -> None:
        sess = _clone_session(session)
        try:
            for i, (meal, day, target, s) in enumerate(jobs):
                if abort.is_set():
                    return
                if date in dead:
                    return safe_advance(len(jobs) - i)
                html = _post(sess, url, payload, __EVENTTARGET=target, __EVENTARGUMENT="", **{target: s.id})
                if not _apply_result(date, meal, day, s, html):
                    dead.add(date)
                    return safe_advance(len(jobs) - i)
                safe_advance()
        except BaseException:
            abort.set()
            raise

    workers = min(max_threads or len(cells), len(cells))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(run_cell, key[1], jobs) for key, jobs in cells.items()]:
            f.result()  # خطای thread را بالا می‌آورد


def _apply_result(date: str, meal, day, s, html: str) -> bool:
    """پاسخِ postback را در سلف s می‌گذارد. False یعنی روز در پاسخ سایت غیرفعال شده است."""
    page = parse_page(html)
    d2 = next((d for m in page.meals if m.key == meal.key for d in m.days if d.date == day.date), None)
    if d2 is None or not d2.active:
        return False
    got = next((x for x in d2.selves if x.id == s.id and x.selected), None)
    if got:
        s.foods = got.foods
    return True


def _run_jobs_sync(session, url, payload, jobs_by_date, advance) -> None:
    """روش sequential: درخواست‌ها پشت‌سرهم. روزِ غیرفعال، بقیه‌ی کارهایش لغو می‌شود."""
    for date, jobs in jobs_by_date.items():
        for i, (meal, day, target, s) in enumerate(jobs):
            html = _post(session, url, payload, __EVENTTARGET=target, __EVENTARGUMENT="", **{target: s.id})
            if not _apply_result(date, meal, day, s, html):
                advance(len(jobs) - i)
                break
            advance()


def _can_async() -> bool:
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    try:
        asyncio.get_running_loop()
        return False  # داخل یک حلقه‌ی asyncio دیگر هستیم؛ مسیر thread امن‌تر است
    except RuntimeError:
        return True


async def _run_jobs_async(session, url, payload, jobs_by_date, advance, concurrency, transport=None) -> None:
    """
    همه‌ی postbackها با asyncio و یک اتصال‌پولِ مشترک، همزمان (حداکثر `concurrency` تا در هر لحظه).
    اگر روزی در یکی از پاسخ‌ها غیرفعال دیده شود، کارهای باقی‌مانده‌ی همان روز اصلاً فرستاده نمی‌شوند.
    """
    import httpx

    jar = httpx.Cookies()
    for c in session.cookies:
        jar.set(c.name, c.value, domain=c.domain or "", path=c.path or "/")
    headers = {k: v for k, v in session.headers.items() if k.lower() not in ("accept-encoding", "connection")}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)
    dead: set = set()

    async with httpx.AsyncClient(cookies=jar, headers=headers, limits=limits, timeout=60.0, transport=transport,
                                 follow_redirects=True) as client:
        async def one(date, meal, day, target, s) -> None:
            if date in dead:
                return advance()
            async with sem:
                if date in dead:
                    return advance()
                r = await client.post(url, data={**payload, "__EVENTTARGET": target, "__EVENTARGUMENT": "", target: s.id})
                r.raise_for_status()
            if not _apply_result(date, meal, day, s, r.text):
                dead.add(date)
            advance()

        tasks = [asyncio.ensure_future(one(date, *job)) for date, jobs in jobs_by_date.items() for job in jobs]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def fallback_notice(next_menu: WeekMenu, current_menu: WeekMenu) -> str:
    return (f"منوی هفته بعد ({next_menu.week_range or '؟'}) هنوز غذایی ثبت نشده است؛ "
            f"منوی هفته جاری ({current_menu.week_range or '؟'}) نمایش داده می‌شود.")


def fetch_menu_with_fallback(session, week: str, **kw) -> tuple[WeekMenu, Optional[str]]:
    """
    مثل fetch_menu؛ اما اگر «هفته بعد» هنوز غذایی نداشت، بلافاصله منوی «هفته جاری» را می‌گیرد.
    خروجی: (منو، پیام اطلاع‌رسانی یا None). اگر هر دو هفته خالی باشند، همان منوی خالی‌ی هفته بعد برمی‌گردد.
    """
    menu = fetch_menu(session, week, **kw)
    if week == "next" and not menu.has_food:
        current = fetch_menu(session, "current", **kw)
        if current.has_food:
            return current, fallback_notice(menu, current)
    return menu, None

