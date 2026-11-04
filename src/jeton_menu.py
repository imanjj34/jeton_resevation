"""
jeton_menu.py - ورود با requests (+ کپچای SSIM)، سپس همه‌ی کارها با requests.

    python jeton_menu.py              # اولین اجرا: config.ini ساخته می‌شود؛ بعد از آن [app] ui تعیین می‌کند
    python jeton_menu.py --ui web     # از همان اول Chrome باز می‌شود و همه‌چیز مرحله‌به‌مرحله در صفحه‌ی وب انجام می‌شود
    python jeton_menu.py --setup      # پرسش دوباره‌ی تنظیمات و ساخت دوباره‌ی config.ini

همه‌ی تنظیمات (هفته، فونت، تم، روش دریافت منو، پرداخت و ...) در config.ini است؛ راهنما: CONFIG.md.
"""
from __future__ import annotations

import argparse
import json
import getpass
import os
import subprocess
import sys
import threading
from pathlib import Path
import time
import webbrowser

import requests

from config import ConfigError, apply_logging, cfg, ensure, runtime_root, save_option
from food_menu import fallback_notice, fetch_menu, fetch_menu_with_fallback
from jeton_http import PaymentError, bank_target, http_login, start_bank_payment
from pay_window import CALLBACK_MARK
from reserve import (account_warning, clear_pending, current_state, load_pending, logout_session, price_delta, submit,
                     summary_lines)
from web_app import AppServer
from web_select import SelectionServer

apply_logging()  # سطح لاگ از config.ini؛ لاگ HTTP فقط در DEBUG



def get_session(username: str, password: str) -> requests.Session:
    return http_login(username, password, cfg.captcha_dataset, cfg.retries, cfg.captcha_threshold)


class PaymentWindow:
    """
    پنجره‌ی پرداخت (pay_window.py با pywebview) در یک پردازش جدا. کوکی‌های نشست برنامه داخل همان پنجره گذاشته می‌شود
    تا صفحه‌ی بازگشت از بانک (ePay_CallBack) نشست را ببیند. نتیجه فقط گزارش می‌شود؛ موجودی را کاربر دستی بروز می‌کند.
    """

    def __init__(self, proc: subprocess.Popen, spec_path: str):
        self.proc, self.spec_path, self.events = proc, spec_path, []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass

    def wait_for_result(self, stop, timeout_sec=None, poll_sec=1.0):
        """تا بسته شدن پنجره (یا لغو/مهلت) منتظر می‌ماند. خروجی: {"manual": True, "result": رویداد نتیجه/خطا یا None} | None اگر لغو شد."""
        timeout_sec = timeout_sec or cfg.payment_timeout_minutes * 60
        waited = 0.0
        while self.proc.poll() is None and not stop.is_set() and waited < timeout_sec:
            stop.wait(poll_sec)
            waited += poll_sec
        if stop.is_set():
            return None
        time.sleep(0.3)  # خواندن آخرین خط‌های خروجی
        found = [e for e in self.events if e.get("event") in ("result", "error")]
        return {"manual": True, "result": found[-1] if found else None}

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


def open_payment(session: requests.Session, amount: int, username: str = "", password: str = "") -> PaymentWindow:
    """
    ۱) مبلغ و دکمه‌ها با requests؛ ۲) پنجره‌ی pywebview با کوکی‌های نشست باز می‌شود و بانک را نشان می‌دهد.
    اگر pywebview نصب نباشد، قبل از هر کاری خطا می‌دهد (پرداخت بدون نشست، صفحه‌ی بازگشت را می‌شکند).
    """
    import importlib.util
    import tempfile
    if importlib.util.find_spec("webview") is None:
        raise PaymentError("برای پرداخت، pywebview لازم است: pip install pywebview")
    resp = start_bank_payment(session, amount, cfg.epay_url)
    host = cfg.base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]  # بدون پورت
    spec = {"title": "پرداخت اینترنتی", "inject_url": cfg.base_url.rstrip("/") + "/favicon.ico", "callback_mark": CALLBACK_MARK, "close_on_success": bool(cfg.close_payment_window),
            "cookies": [{"name": c.name, "value": c.value, "path": c.path or "/"} for c in session.cookies
                        if not c.domain or host.endswith(c.domain.lstrip("."))],
            "bank": bank_target(resp)}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    # opencv با import شدن مسیر pluginهای Qt خودش را در محیط می‌گذارد؛ به پنجره‌ی پرداخت (Qt در لینوکس/مک) نرسد
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")}
    command = ([sys.executable, "--payment-window", f.name] if getattr(sys, "frozen", False)
               else [sys.executable, str(Path(__file__).with_name("pay_window.py")), f.name])
    proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
    return PaymentWindow(proc, f.name)


# ------------------------------------------------- ورود و دریافت منو در پس‌زمینه
class Prefetch:
    """
    ورود (و در صورت لزوم دریافت منوی هفته‌ی پیش‌فرض) را همان لحظه در پس‌زمینه شروع می‌کند تا همزمان با
    پرسیدن بقیه‌ی سؤال‌ها انجام شود. اگر هفته‌ی بعد هنوز غذایی نداشته باشد، بلافاصله منوی هفته‌ی جاری را هم
    می‌گیرد. اگر کاربر هفته‌ی دیگری بخواهد، منوی پیش‌دریافت‌شده کنار گذاشته می‌شود.
    """

    def __init__(self, username: str, password: str, week: str | None = None):
        self.week, self.session = week, None
        self.menus: dict = {}       # هفته -> منو (فقط چیزهایی که در پس‌زمینه دریافت شده)
        self.error: Exception | None = None
        self.login_done, self.menu_done = threading.Event(), threading.Event()
        self.fallback_started = threading.Event()  # هفته‌ی بعد خالی بود و هفته‌ی جاری در حال دریافت است
        threading.Thread(target=self._run, args=(username, password), daemon=True).start()

    def _run(self, username: str, password: str) -> None:
        try:
            self.session = get_session(username, password)
        except Exception as e:  # noqa: BLE001
            self.error = e
            self.login_done.set()
            self.menu_done.set()
            return
        self.login_done.set()
        if self.week:
            try:
                self.menus[self.week] = fetch_menu(self.session, self.week, all_selves=True)
                if self.week == "next" and not self.menus["next"].has_food:  # هفته بعد هنوز خالی است: فوراً هفته جاری
                    self.fallback_started.set()
                    self.menus["current"] = fetch_menu(self.session, "current", all_selves=True)
            except Exception:  # noqa: BLE001  (بعداً در پیش‌زمینه دوباره امتحان می‌شود)
                pass
        self.menu_done.set()

    def wait(self, week: str | None):
        """(session، منوی آماده یا None، پیام اطلاع‌رسانی یا None). اگر ورود شکست خورده باشد همان خطا بالا می‌آید."""
        if not self.login_done.is_set():
            print("Completing login ...")
        self.login_done.wait()
        if self.error:
            raise self.error
        if not week or week not in ("next", "current") or not self.week:
            return self.session, None, None
        if week != self.week and not (week == "current" and self.fallback_started.is_set()):
            return self.session, None, None  # این هفته پیش‌دریافت نمی‌شود؛ مستقیم گرفته می‌شود
        if not self.menu_done.is_set():
            print("Completing menu download ...")
        self.menu_done.wait()
        menu = self.menus.get(week)
        if menu is not None and not (week == "next" and not menu.has_food):
            return self.session, menu, None
        current = self.menus.get("current")
        if week == "next" and menu is not None and current is not None and current.has_food:
            return self.session, current, fallback_notice(menu, current)
        return self.session, menu, None


# ------------------------------------------------------------ پرسش‌ها
def ask(prompt: str, default: str = "") -> str:
    ans = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return ans or default


def choose(prompt: str, options: list[str], default: int = 1) -> int:
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        ans = input(f"Choice [{default}]: ").strip() or str(default)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans)
        print("Enter a valid number.")


def login_prompt() -> tuple[str, str]:
    """نام کاربری/رمز از config.ini (یا متغیر محیطی JETON_USER / JETON_PASS)؛ اگر نبود، پرسیده می‌شود."""
    username = cfg.username or ask("Username")
    password = cfg.password or getpass.getpass("Password (input hidden): ")
    return username, password


def ask_week() -> str:
    weeks = ["current", "next"]
    return weeks[choose("\nWhich week?", ["Current week", "Next week"], weeks.index(cfg.week) + 1) - 1]


def show_progress(n: int, total: int) -> None:
    width = 30
    filled = int(width * n / total) if total else width
    print(f"\r  [{'#' * filled}{'-' * (width - filled)}] {int(100 * n / total) if total else 100}%", end="", flush=True)


def report(results: list[dict]) -> None:
    print()
    for r in results:
        print(f"{'OK' if r['ok'] else 'FAILED'} {r['meal']}" + (f" - Site message: {r['message']}" if r.get("message") else ""))
        for pr in r["problems"]:
            print(f"    ! {pr}")
        for w in r.get("warnings", []):
            print(f"    WARNING: {w}")
        if r.get("credit") is not None:
            print(f"    Balance after submission: {r['credit']:,} rial")
    if all(r["ok"] for r in results):
        clear_pending()
        print("\nAll changes were submitted and verified.")
    else:
        print("\nSome items were not submitted. Review the messages and check the website.")


# ------------------------------------------------------------------ حالت‌ها
def ask_file_options() -> tuple[str, bool]:
    ext = [".html", ".json", ".txt"][choose("\nOutput format?", ["HTML", "JSON", "Plain text"]) - 1]
    default = str(runtime_root() / f"menu{ext}") if getattr(sys, "frozen", False) else f"menu{ext}"
    out = ask("نام فایل خروجی", default)
    if not os.path.splitext(out)[1]:
        out += ext
    open_after = ext == ".html" and ask("بعد از ساخت، در مرورگر باز شود؟ (y/n)", "y").lower().startswith("y")
    return out, open_after


def _menu_or_fetch(session, week: str, menu=None):
    """منوی آماده را برمی‌گرداند یا (با جایگزینی هفته جاری اگر هفته بعد خالی بود) همین حالا می‌گیرد."""
    if menu is None:
        print("\nDownloading menus from all cafeterias ...")
        menu, notice = fetch_menu_with_fallback(session, week, all_selves=True, on_progress=show_progress)
        print()
        if notice:
            print(notice)
    return menu


def mode_menu_file(session, week: str, out: str, open_after: bool, menu=None) -> None:
    menu = _menu_or_fetch(session, week, menu)
    if not menu.has_food:
        print(f"\nNo meals are available for this week ({menu.week_range})." + (f"\n{menu.message}" if menu.message else ""))
        return

    ext = os.path.splitext(out)[1].lower()
    if ext == ".html" and getattr(sys, "frozen", False) and not os.path.isabs(out):
        out = str(runtime_root() / out)
    content = (json.dumps(menu.to_dict(), ensure_ascii=False, indent=2) if ext == ".json"
               else menu.to_text() if ext == ".txt" else menu.to_html())
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\nSaved to: {os.path.abspath(out)}")
    if open_after:
        webbrowser.open("file://" + os.path.abspath(out))


def mode_reserve(session, relogin, username: str, password: str, week: str, menu=None) -> None:
    menu = _menu_or_fetch(session, week, menu)
    if not menu.has_food:
        print(f"\nNo meals are available for this week ({menu.week_range}).")
        return
    # انتخاب در صفحه‌ی وب انجام می‌شود و بعد از «ثبت نهایی» روی سایت اعمال می‌شود
    SelectionServer(session, menu, relogin,
                    pay_opener=lambda sess, amount: open_payment(sess, amount, username, password), username=username,
                    refetch=lambda sess, wk, on_progress: fetch_menu(sess, wk, all_selves=True, on_progress=on_progress),
                    week_key=week, close_pay_window=cfg.close_payment_window, exit_when_done=cfg.exit_after_success).run()


def mode_pending(session, relogin) -> None:
    """انتخاب‌های ذخیره‌شده را (فقط یک بار موجودی را چک می‌کند) در صورت کافی بودن اعتبار ثبت می‌کند."""
    choices = load_pending()
    try:
        menu = current_state(session, choices)
    except LookupError as e:
        print(f"\n{e}\nThese saved choices are no longer valid. Removing {cfg.pending_file}.")
        clear_pending()
        return
    delta, credit = price_delta(menu, choices), menu.credit or 0
    print("\nSaved choices:\n" + "\n".join(summary_lines(menu, choices)))
    print(f"\nBalance change: {-delta:+,} rial  |  Balance: {credit:,} rial")
    if delta > credit:
        print(f"\nBalance is insufficient; shortfall: {delta - credit:,} rial.\n"
              "Add credit, then run this option again.")
        return
    if ask("\nثبت شود؟ (y/n)", "y").lower().startswith("y"):
        report(submit(session, choices, on_step=print))


MODE_KEYS = ["reserve", "file", "pending"]


def _logout_if_configured(session) -> None:
    """[app] logout_on_exit: بعد از پایان کار، نشستِ روی سرور هم بسته می‌شود (نه فقط برنامه). سرور وب خودش خارج می‌شود."""
    if cfg.logout_on_exit and session is not None:
        ok, msg = logout_session(session)
        print("Logged out." if ok else f"Logout was not confirmed: {msg}")


def terminal_main() -> None:
    print("=== Meal Reservation ===\n")
    username, password = login_prompt()
    pending = load_pending()

    # از همین لحظه ورود (و دریافت منوی هفته‌ی پیش‌فرض) در پس‌زمینه شروع می‌شود؛ بقیه‌ی سؤال‌ها همزمان پرسیده می‌شود
    prefetch = Prefetch(username, password, cfg.week if cfg.mode in ("reserve", "file") else None)

    options = ["View menu, select meals in the web page, and reserve", "Download menu and create a file"]
    if pending:
        options.append(f"Submit saved choices ({len(pending)} items)")
    default = MODE_KEYS.index(cfg.mode) + 1 if MODE_KEYS.index(cfg.mode) < len(options) else 1
    mode = MODE_KEYS[choose("\nچه کاری انجام شود؟", options, default) - 1]
    week = ask_week() if mode in ("reserve", "file") else None
    file_opts = ask_file_options() if mode == "file" else None

    session, menu, notice = prefetch.wait(week)  # فقط اگر ورود هنوز تمام نشده باشد منتظر می‌ماند
    if notice:
        print("\n" + notice)
    if menu is not None and (menu.account_name or menu.account_id):   # بازخورد خودِ سایت
        acct = {"name": menu.account_name, "id": menu.account_id}
        print("Account on the website: " + " · ".join(x for x in acct.values() if x))
        if account_warning(username, acct):
            print("WARNING: " + account_warning(username, acct))
    relogin = lambda: get_session(username, password)
    if mode == "reserve":
        mode_reserve(session, relogin, username, password, week, menu)
    elif mode == "file":
        mode_menu_file(session, week, *file_opts, menu=menu)
        _logout_if_configured(session)
    else:
        mode_pending(session, relogin)
        _logout_if_configured(session)


def web_main() -> None:
    """از همان اول Chrome باز می‌شود و ورود، انتخاب کار، دریافت منو و انتخاب غذا همه در صفحه‌ی وب انجام می‌شود."""
    creds: dict = {}

    def login(u: str, p: str) -> requests.Session:
        s = get_session(u, p)
        creds.update(u=u, p=p)  # فقط در حافظه، برای ورود دوباره و پرداخت
        return s

    def set_dataset(path: str) -> None:
        save_option("captcha_dataset", path)  # در حافظه و در config.ini

    AppServer(
        login=login,
        fetch=lambda session, week, on_progress: fetch_menu(session, week, all_selves=True, on_progress=on_progress),
        relogin=lambda: get_session(creds["u"], creds["p"]),
        pay_opener=lambda sess, amount: open_payment(sess, amount, creds["u"], creds["p"]),
        env_user=cfg.username, env_pass=cfg.password,
        dataset_ok=lambda: True, set_dataset=set_dataset,
        default_mode=cfg.mode, default_week=cfg.week,
        close_pay_window=cfg.close_payment_window, exit_when_done=cfg.exit_after_success,
    ).run()


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--payment-window":
        from pay_window import run
        with open(sys.argv[2], encoding="utf-8-sig") as f:
            raise SystemExit(run(json.load(f)))
    ap = argparse.ArgumentParser(description="Download menus and reserve meals (config: config.ini)")
    ap.add_argument("--ui", choices=("terminal", "web"), default=None, help="Choose terminal or web interaction for this run")
    ap.add_argument("--setup", action="store_true", help="Recreate the configuration file")
    args = ap.parse_args()
    try:
        ensure(force_setup=args.setup)   # فایل نبود (یا --setup): مقدارها در ترمینال پرسیده و فایل ساخته می‌شود
    except ConfigError as e:
        print(f"\nERROR: {e}\nFix the configuration file or run: python jeton_menu.py --setup")
        raise SystemExit(2)
    for w in cfg.warnings:
        print("WARNING:", w)
    apply_logging()
    web_main() if (args.ui or cfg.ui) == "web" else terminal_main()


if __name__ == "__main__":
    main()