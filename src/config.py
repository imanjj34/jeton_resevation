"""
config.py - همه‌ی تنظیمات برنامه در یک فایل (config.ini) و خواندن آن‌ها از همان‌جا.

    from config import cfg
    cfg.week            # "next"
    cfg.headless        # True

- فایل کنار برنامه ساخته می‌شود (یا مسیر دلخواه در متغیر محیطی JETON_CONFIG).
- اولین اجرا (فایل وجود ندارد): در ترمینال مقادیر پرسیده می‌شود و گزینه‌های قابل انتخاب نشان داده می‌شود؛
  بعد فایل ساخته می‌شود. دوباره‌سازی: python jeton_menu.py --setup
- تنها استثنا: JETON_USER و JETON_PASS (متغیر محیطی) از نام کاربری/رمز داخل فایل مقدم‌اند تا رمز لازم نباشد
  در فایل نوشته شود.
- راهنمای کامل: CONFIG.md  (بازتولید: python config.py --docs)
"""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# ----------------------------------------------------------------------------- تعریف گزینه‌ها
@dataclass(frozen=True)
class Opt:
    section: str
    key: str
    default: Any
    kind: str                       # str | bool | int | float | choice | path
    doc: str                        # توضیح فارسی (در فایل و در راهنما)
    choices: tuple = ()
    choice_docs: dict = field(default_factory=dict)
    ask: bool = False               # در راه‌اندازی اولین اجرا پرسیده شود؟
    lo: Optional[float] = None
    hi: Optional[float] = None
    attr: Optional[str] = None      # نام ویژگی روی cfg (پیش‌فرض: key)
    secret: bool = False

    @property
    def name(self) -> str:
        return self.attr or self.key


OPTIONS: list[Opt] = [
    # ---------------------------------------------------------------- app
    Opt("app", "ui", "web", "choice", "پرسش‌ها و کارها کجا انجام شود؟",
        ("terminal", "web"), {"terminal": "پرسش‌ها در ترمینال، انتخاب غذا در صفحه‌ی وب",
                              "web": "از همان اول Chrome باز می‌شود و همه‌چیز مرحله‌به‌مرحله در صفحه‌ی وب است"}, ask=True),
    Opt("app", "mode", "reserve", "choice", "کار پیش‌فرض هنگام شروع.",
        ("reserve", "file", "pending"), {"reserve": "مشاهده‌ی منو، انتخاب غذا و ثبت رزرو",
                                          "file": "فقط دریافت منو و ساخت فایل",
                                          "pending": "ثبت انتخاب‌های ذخیره‌شده (وقتی موجودی کم بود)"}, ask=True),
    Opt("app", "week", "next", "choice", "هفته‌ی پیش‌فرض. اگر هفته‌ی بعد هنوز غذایی نداشته باشد، خودکار هفته‌ی جاری نشان داده می‌شود.",
        ("current", "next"), {"current": "هفته‌ی جاری", "next": "هفته‌ی بعد (برای رزرو)"}, ask=True),
    Opt("app", "exit_after_success", False, "bool",
        "بعد از ثبت موفق، خودِ برنامه (سرور محلی) بسته شود؟ false = باز می‌ماند تا Ctrl+C.", ask=True),
    Opt("app", "logout_on_exit", True, "bool",
        "هنگام بسته شدن برنامه (دکمه‌ی خروج یا Ctrl+C) از حساب سامانه هم خارج شود تا نشستِ روی سرور باز نماند؟"),
    Opt("app", "pending_file", "pending.json", "path",
        "فایلی که انتخاب‌های ذخیره‌شده (هنگام کمبود موجودی) در آن نگه داشته می‌شود."),
    # ---------------------------------------------------------------- site
    Opt("site", "base_url", "https://jeton.yazd.ac.ir/", "str",
        "آدرس پایه‌ی سامانه (با / در آخر). صفحه‌های رزرو، پرداخت و ورود از روی آن ساخته می‌شوند."),
    # ---------------------------------------------------------------- login
    Opt("login", "username", "", "str",
        "نام کاربری. خالی = هر بار پرسیده می‌شود. (متغیر محیطی JETON_USER بر این مقدم است.)", ask=True),
    Opt("login", "password", "", "str",
        "رمز عبور. توصیه: خالی بگذار و از متغیر محیطی JETON_PASS استفاده کن؛ رمز داخل فایل به‌صورت متن ساده می‌ماند.",
        secret=True, ask=True),
    Opt("login", "headless", True, "bool",
        "مرورگرِ ورود و کپچا مخفی باشد؟ (پنجره‌ی پرداخت همیشه دیده می‌شود.)", ask=True),
    Opt("login", "eager_load", True, "bool",
        "صفحه‌ی ورود را بدون انتظار برای همه‌ی منابع باز کن (سریع‌تر). اگر ورود مشکل پیدا کرد false کن."),
    Opt("login", "retries", 5, "int", "تعداد تلاش برای ورود (کپچا گاهی اشتباه تشخیص داده می‌شود).", lo=1, hi=20),
    Opt("login", "captcha_dataset", r"dataset", "path",
        "پوشه‌ی دیتاست تصاویر مرجع کپچا (زیرپوشه‌هایی به نام ارقام)."),
    Opt("login", "captcha_threshold", 0.4, "float", "کمترین شباهت (SSIM) برای پذیرفتن یک رقم کپچا.", lo=0, hi=1),
    Opt("login", "chromedriver_path", "", "path",
        "مسیر chromedriver دستی. خالی = Selenium خودش مدیریت می‌کند. فقط اگر دانلود خودکار 403 داد پر کن."),
    # ---------------------------------------------------------------- fetch
    Opt("fetch", "method", "threads", "choice",
        "روش دریافت غذاهای همه‌ی سلف‌ها (برای سرعت).",
        ("threads", "async", "sequential"),
        {"threads": "برای هر وعده در هر روز یک thread؛ همه‌ی منو یک‌جا دریافت می‌شود (پیش‌فرض)",
         "async": "asyncio با کتابخانه‌ی httpx و حداکثر fetch_concurrency درخواست همزمان",
         "sequential": "درخواست‌ها پشت‌سرهم (کند؛ برای اشکال‌یابی)"}, ask=True, attr="fetch_method"),
    Opt("fetch", "max_threads", 8, "int",
        "فقط برای روش threads: حداکثر تعداد thread همزمان. 0 = برای هر وعده در هر روز یک thread.",
        lo=0, hi=64, attr="fetch_max_threads"),
    Opt("fetch", "concurrency", 8, "int", "فقط برای روش async: حداکثر درخواست همزمان.", lo=1, hi=64,
        attr="fetch_concurrency"),
    # ---------------------------------------------------------------- payment
    Opt("payment", "close_window", False, "bool",
        "بعد از نتیجه‌ی پرداخت، پنجره‌ی بانک خودکار بسته شود؟ false = باز می‌ماند.", ask=True, attr="close_payment_window"),
    Opt("payment", "timeout_minutes", 15, "int",
        "چند دقیقه منتظر برگشت از بانک بمانیم؟", lo=1, hi=240, attr="payment_timeout_minutes"),
    # ---------------------------------------------------------------- ui
    Opt("ui", "font", "2 Nazanin", "str",
        "فونت صفحه‌ها (باید روی سیستم نصب باشد). در نبودش B Nazanin، Nazanin، Vazirmatn و فونت سیستم."),
    Opt("ui", "theme", "system", "choice", "تم پیش‌فرض (دکمه‌ی تم داخل صفحه همچنان کار می‌کند).",
        ("system", "light", "dark"), {"system": "مطابق تنظیم سیستم", "light": "روشن", "dark": "تاریک"}, ask=True),
    # ---------------------------------------------------------------- advanced
    Opt("advanced", "log_level", "INFO", "choice",
        "سطح لاگ ترمینال. خط‌های HTTP (مثل «HTTP/1.1 200 OK») فقط در DEBUG نشان داده می‌شوند.",
        ("DEBUG", "INFO", "WARNING", "ERROR"),
        {"DEBUG": "همه‌چیز از جمله لاگ HTTP", "INFO": "پیام‌های اصلی برنامه (پیش‌فرض)", "WARNING": "فقط هشدار و خطا",
         "ERROR": "فقط خطا"}),
    Opt("advanced", "prefetch_max_age", 600, "int",
        "منوی پیش‌دریافت‌شده در ویزارد وب بعد از چند ثانیه کهنه حساب شود و دوباره گرفته شود؟", lo=10, hi=3600),
]
BY_NAME = {o.name: o for o in OPTIONS}
SECTIONS = list(dict.fromkeys(o.section for o in OPTIONS))
SECTION_TITLES = {"app": "برنامه", "site": "سامانه", "login": "ورود و کپچا", "fetch": "دریافت منو", "payment": "پرداخت",
                  "ui": "ظاهر", "advanced": "پیشرفته"}


class ConfigError(ValueError):
    """خطای مقدار نامعتبر در فایل تنظیمات (همه‌ی خطاها یک‌جا گزارش می‌شوند)."""


# ----------------------------------------------------------------------------- مقدارها
_TRUE, _FALSE = {"1", "true", "yes", "on", "بله"}, {"0", "false", "no", "off", "خیر"}


def parse_value(opt: Opt, raw: Any) -> Any:
    """متن را به نوع گزینه تبدیل و اعتبارسنجی می‌کند؛ خطا → ValueError با پیام فارسی."""
    if opt.kind == "bool":
        if isinstance(raw, bool):
            return raw
        t = str(raw).strip().lower()
        if t in _TRUE:
            return True
        if t in _FALSE:
            return False
        raise ValueError("باید true یا false باشد")
    if opt.kind in ("int", "float"):
        try:
            v = int(str(raw).strip()) if opt.kind == "int" else float(str(raw).strip())
        except ValueError:
            raise ValueError("باید عدد باشد") from None
        if opt.lo is not None and v < opt.lo or opt.hi is not None and v > opt.hi:
            raise ValueError(f"باید بین {opt.lo:g} و {opt.hi:g} باشد")
        return v
    t = str(raw).strip()
    if opt.kind == "choice":
        for c in opt.choices:  # بدون حساس بودن به بزرگی/کوچکی حروف
            if t.lower() == c.lower():
                return c
        raise ValueError("یکی از این‌ها باشد: " + " | ".join(opt.choices))
    if opt.kind == "path":
        return os.path.expandvars(os.path.expanduser(t)) if t else ""
    return t


def _fmt(opt: Opt, v: Any) -> str:
    return ("true" if v else "false") if opt.kind == "bool" else str(v)


# ----------------------------------------------------------------------------- شیء تنظیمات
class Config:
    """مقدارها به‌صورت ویژگی خوانده می‌شوند: cfg.week. (هر بار هنگام استفاده خوانده می‌شود، پس با load دوباره به‌روز است.)"""

    def __init__(self) -> None:
        object.__setattr__(self, "_v", {o.name: o.default for o in OPTIONS})
        object.__setattr__(self, "path", None)
        object.__setattr__(self, "exists", False)
        object.__setattr__(self, "warnings", [])

    def __getattr__(self, name: str) -> Any:
        v = object.__getattribute__(self, "_v")
        if name in v:
            if name == "username":
                return os.environ.get("JETON_USER") or v[name]
            if name == "password":
                return os.environ.get("JETON_PASS") or v[name]
            return v[name]
        base = v["base_url"].rstrip("/") + "/"
        derived = {"home_url": [base + "Master/Default.aspx", base + "MyCullinan/ChangePassword.aspx?mustRelogin=true"], "epay_url": base + "ePay/ePay_Payment.aspx",
                   "reservation_url": base + "Reservation/Reservation.aspx"}
        if name in derived:
            return derived[name]
        raise AttributeError(name)

    def update(self, **kw: Any) -> None:
        """تغییر موقت در حافظه (مثل --headed)؛ داخل فایل نوشته نمی‌شود."""
        for k, val in kw.items():
            if k not in BY_NAME:
                raise KeyError(k)
            self._v[k] = val

    def values(self) -> dict:
        return dict(self._v)


cfg = Config()


def config_path() -> Path:
    env = os.environ.get("JETON_CONFIG")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent / "config.ini"


def load(path: str | os.PathLike | None = None) -> Config:
    """فایل را می‌خواند و cfg را به‌روز می‌کند. فایل نبود → پیش‌فرض‌ها (cfg.exists=False)."""
    p = Path(path) if path else config_path()
    values = {o.name: o.default for o in OPTIONS}
    warnings: list[str] = []
    errors: list[str] = []
    exists = p.is_file()
    if exists:
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",), comment_prefixes=(";", "#"))
        try:
            cp.read(p, encoding="utf-8-sig")
        except configparser.Error as e:
            raise ConfigError(f"فایل {p} خوانده نشد: {e}") from None
        known = {(o.section, o.key): o for o in OPTIONS}
        for sec in cp.sections():
            for key, raw in cp.items(sec):
                opt = known.get((sec, key))
                if opt is None:
                    warnings.append(f"گزینه‌ی ناشناخته نادیده گرفته شد: [{sec}] {key}")
                    continue
                try:
                    values[opt.name] = parse_value(opt, raw)
                except ValueError as e:
                    errors.append(f"[{sec}] {key} = {raw!r}: {e}")
    if errors:
        raise ConfigError(f"مقدار نامعتبر در {p}:\n  " + "\n  ".join(errors))
    object.__setattr__(cfg, "_v", values)
    object.__setattr__(cfg, "path", p)
    object.__setattr__(cfg, "exists", exists)
    object.__setattr__(cfg, "warnings", warnings)
    return cfg


# ----------------------------------------------------------------------------- نوشتن فایل
def render(values: dict | None = None) -> str:
    """متن کامل فایل (با توضیح هر گزینه) از روی مقدارها."""
    values = {**{o.name: o.default for o in OPTIONS}, **(values or {})}
    out = ["; تنظیمات برنامه‌ی رزرو غذا. هر خط «کلید = مقدار» است؛ خط‌هایی که با ; شروع می‌شوند توضیح‌اند.",
           "; راهنمای کامل: CONFIG.md   |   ساخت دوباره با پرسش: python jeton_menu.py --setup", ""]
    for sec in SECTIONS:
        out.append(f"[{sec}]  ; {SECTION_TITLES[sec]}")
        for o in (x for x in OPTIONS if x.section == sec):
            out.append(f"; {o.doc}")
            if o.choices:
                out.append("; مقدارهای ممکن: " + " | ".join(o.choices))
            elif o.kind == "bool":
                out.append("; مقدارهای ممکن: true | false")
            elif o.lo is not None:
                out.append(f"; بازه: {o.lo:g} تا {o.hi:g}")
            out.append(f"{o.key} = {_fmt(o, values[o.name])}")
        out.append("")
    return "\n".join(out)


def write(values: dict | None = None, path: str | os.PathLike | None = None) -> Path:
    p = Path(path) if path else config_path()
    p.write_text(render(values), encoding="utf-8")
    return p


def save_option(name: str, value: Any) -> None:
    """یک گزینه را در حافظه و (اگر فایل وجود دارد) داخل فایل به‌روز می‌کند."""
    cfg.update(**{name: value})
    if cfg.exists and cfg.path:
        write(cfg.values(), cfg.path)


# ----------------------------------------------------------------------------- راه‌اندازی اولین اجرا
def _ask_option(opt: Opt, current: Any, idx: int, total: int, input_fn: Callable, print_fn: Callable) -> Any:
    section = SECTION_TITLES[opt.section]
    print_fn(f"\n[{idx}/{total}] {opt.key} ({section}) - {opt.doc}")
    if opt.kind == "choice" or opt.kind == "bool":
        items = list(opt.choices) if opt.kind == "choice" else ["true", "false"]
        for n, c in enumerate(items, 1):
            label = opt.choice_docs.get(c, "") if opt.kind == "choice" else ("بله" if c == "true" else "خیر")
            print_fn(f"   {n}) {c}" + (f"  - {label}" if label else ""))
        default_txt = _fmt(opt, current)
        while True:
            ans = input_fn(f"انتخاب (شماره یا مقدار) [{default_txt}]: ").strip()
            if not ans:
                return current
            if ans.isdigit() and 1 <= int(ans) <= len(items):
                ans = items[int(ans) - 1]
            try:
                return parse_value(opt, ans)
            except ValueError as e:
                print_fn(f"   نامعتبر: {e}")
    hint = f" (بازه {opt.lo:g} تا {opt.hi:g})" if opt.lo is not None else ""
    while True:
        ans = input_fn(f"مقدار{hint} [{current}]: ").strip()
        if not ans:
            return current
        try:
            return parse_value(opt, ans)
        except ValueError as e:
            print_fn(f"   نامعتبر: {e}")


def first_run(path: str | os.PathLike | None = None, input_fn: Callable = input, print_fn: Callable = print,
              current: dict | None = None) -> Path:
    """مقدارهای اصلی را در ترمینال می‌پرسد (با نمایش گزینه‌های قابل انتخاب) و فایل تنظیمات را می‌سازد."""
    p = Path(path) if path else config_path()
    values = {**{o.name: o.default for o in OPTIONS}, **(current or {})}
    asked = [o for o in OPTIONS if o.ask]
    print_fn(f"\n=== راه‌اندازی اولیه ===\nفایل تنظیمات وجود ندارد؛ مقدارهای اصلی را مشخص کن (Enter = مقدار داخل [ ]).\n"
             f"بقیه‌ی گزینه‌ها با مقدار پیش‌فرض نوشته می‌شوند و بعداً داخل {p.name} قابل ویرایش‌اند (راهنما: CONFIG.md).")
    for i, o in enumerate(asked, 1):
        values[o.name] = _ask_option(o, values[o.name], i, len(asked), input_fn, print_fn)
    write(values, p)
    print_fn(f"\n✓ فایل تنظیمات ساخته شد: {p}")
    return p


def ensure(path: str | os.PathLike | None = None, force_setup: bool = False, input_fn: Callable = input,
           print_fn: Callable = print, interactive: Optional[bool] = None) -> Config:
    """اگر فایل نیست (یا force_setup) راه‌اندازی اولیه را اجرا می‌کند، بعد فایل را می‌خواند."""
    p = Path(path) if path else config_path()
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if force_setup or not p.is_file():
        if interactive:
            current = None
            if p.is_file():
                load(p)
                current = cfg.values()
            first_run(p, input_fn, print_fn, current)
        else:
            write(None, p)
            print_fn(f"فایل تنظیمات با مقدارهای پیش‌فرض ساخته شد: {p}")
    return load(p)


# ----------------------------------------------------------------------------- لاگ
def apply_logging() -> None:
    """سطح لاگ از فایل. لاگ‌های HTTP (httpx/urllib3/selenium) فقط در DEBUG نشان داده می‌شوند."""
    import logging
    level = getattr(logging, str(cfg.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s", force=True)
    noisy = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    for name in ("httpx", "httpcore", "urllib3", "selenium", "asyncio", "charset_normalizer"):
        logging.getLogger(name).setLevel(noisy)


# ----------------------------------------------------------------------------- راهنمای markdown
def docs_markdown() -> str:
    """CONFIG.md از روی همین تعریف گزینه‌ها ساخته می‌شود تا هیچ‌وقت با کد ناهماهنگ نشود."""
    L = ["# راهنمای تنظیمات", "",
         "همه‌ی تنظیمات برنامه در یک فایل به نام **`config.ini`** (کنار برنامه) است و همه‌ی بخش‌های کد از همان‌جا می‌خوانند.", "",
         "## ساخت و ویرایش فایل", "",
         "- **اولین اجرا:** اگر `config.ini` وجود نداشته باشد، برنامه در ترمینال مقدارهای اصلی را می‌پرسد، گزینه‌های قابل انتخاب را نشان می‌دهد و فایل را می‌سازد. (اگر ترمینال تعاملی نباشد، فایل با مقدارهای پیش‌فرض ساخته می‌شود.)",
         "- **پرسش دوباره:** `python jeton_menu.py --setup` (مقدارهای فعلی به‌عنوان پیش‌فرض نشان داده می‌شوند).",
         "- **ویرایش دستی:** فایل را با هر ویرایشگر متنی (UTF-8) باز کن؛ هر گزینه توضیح و مقدارهای ممکن را بالای خودش دارد. برنامه را دوباره اجرا کن.",
         "- **مسیر دیگر:** متغیر محیطی `JETON_CONFIG` را به مسیر فایل بده.",
         "- **مقدار نامعتبر:** برنامه همه‌ی خطاها را یک‌جا با نام بخش و کلید نشان می‌دهد و شروع نمی‌شود. گزینه‌ی ناشناخته فقط هشدار می‌دهد.",
         "- **فقط برای این اجرا:** `--ui web|terminal` و `--headed` همان گزینه‌های `ui` و `headless` را موقتاً عوض می‌کنند (در فایل نوشته نمی‌شوند).",
         "- **متغیرهای محیطی:** `JETON_USER` و `JETON_PASS` از `username` و `password` داخل فایل مقدم‌اند (برای این‌که رمز در فایل نماند).", ""]
    for sec in SECTIONS:
        L += [f"## [{sec}] {SECTION_TITLES[sec]}", "", "| کلید | پیش‌فرض | مقدارهای ممکن | توضیح |", "|---|---|---|---|"]
        for o in (x for x in OPTIONS if x.section == sec):
            if o.choices:
                poss = "<br>".join(f"`{c}` — {o.choice_docs.get(c, '')}" if o.choice_docs.get(c) else f"`{c}`" for c in o.choices)
            elif o.kind == "bool":
                poss = "`true` / `false`"
            elif o.lo is not None:
                poss = f"عدد {o.lo:g} تا {o.hi:g}"
            elif o.kind == "path":
                poss = "مسیر"
            else:
                poss = "متن"
            dflt = f"`{_fmt(o, o.default)}`" if o.default != "" else "(خالی)"
            asked = " *(در اولین اجرا پرسیده می‌شود)*" if o.ask else ""
            L.append(f"| `{o.key}` | {dflt} | {poss} | {o.doc}{asked} |")
        L.append("")
    L += ["## نمونه", "", "```ini", "[app]", "ui = web", "mode = reserve", "week = next", "",
          "[login]", "username = 401234567", "headless = true", "",
          "[fetch]", "method = threads      ; برای هر وعده در هر روز یک thread", "max_threads = 0", "",
          "[ui]", "font = 2 Nazanin", "theme = dark", "```", "",
          "## نکته‌های روش دریافت منو (`[fetch] method`)", "",
          "- `threads` (پیش‌فرض): برای هر **وعده در هر روز** یک thread ساخته می‌شود و همه با هم شروع می‌کنند؛ هر thread غذای سلف‌های دیگر همان وعده/روز را می‌گیرد. اگر سایت درخواست‌های همزمان را کند یا خطا می‌دهد، `max_threads` را (مثلاً 4) بگذار یا روش را `async`/`sequential` کن.",
          "- `async`: یک حلقه‌ی asyncio و اتصال‌پول مشترک؛ تعداد درخواست همزمان با `concurrency` تنظیم می‌شود (نیاز به `httpx`).",
          "- `sequential`: ساده‌ترین و کندترین؛ برای پیدا کردن خطا.", "",
          "## لاگ", "",
          "خط‌هایی مثل `HTTP Request: POST ... \"HTTP/1.1 200 OK\"` فقط وقتی `log_level = DEBUG` باشد در ترمینال نشان داده می‌شوند.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    if "--docs" in sys.argv:
        Path(__file__).resolve().with_name("CONFIG.md").write_text(docs_markdown(), encoding="utf-8")
        print("CONFIG.md ساخته شد")
    elif "--write-default" in sys.argv:
        print("ساخته شد:", write())
    else:
        print(__doc__)
