"""
web_select.py - انتخاب غذا داخل یک صفحه‌ی HTML و اعمال آن روی سایت اصلی.

برنامه یک سرور محلی (فقط 127.0.0.1) باز می‌کند و صفحه را در مرورگر نشان می‌دهد. هر وعده در هر روز
یک رادیوباتن دارد، پس حداکثر یک غذا انتخاب می‌شود. بعد از انتخاب همه‌ی غذاها، دکمه‌ی «ثبت نهایی»
انتخاب‌ها را به همین برنامه می‌فرستد و برنامه آن‌ها را روی سایت اصلی اعمال می‌کند.

    srv = SelectionServer(session, menu, relogin)
    srv.run()          # صفحه را باز می‌کند و تا پایان کار (یا Ctrl+C) منتظر می‌ماند
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from food_menu import WeekMenu, fallback_notice
from ui import BASE_CSS, GRID_CSS, THEME_BOOT, THEME_BUTTON, THEME_TOGGLE_JS
from reserve import (Choice, _is_noop, clear_pending, find_day, find_meal, open_week_state, price_delta,
                     read_credit, save_pending, submit, summary_lines)


def open_browser(url: str) -> None:
    """ترجیحاً در Chrome باز می‌کند؛ اگر پیدا نشد، مرورگر پیش‌فرض."""
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        try:
            webbrowser.get(name).open(url)
            return
        except webbrowser.Error:
            pass
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.exists(path):
            subprocess.Popen([path, url])
            return
    webbrowser.open(url)


def _parse_amount(v) -> int:
    """مبلغ را از متن می‌خواند؛ هر چیز نامعتبر (منفی، حروف، خالی) صفر برمی‌گردد."""
    t = re.sub(r"[,٬\s]", "", str(v or ""))
    return int(t) if t.isdigit() else 0


# ----------------------------------------------------------------- داده‌ی صفحه
def build_data(menu: WeekMenu, preselect: dict | None = None) -> dict:
    meals = []
    for m in menu.meals:
        days = []
        for d in m.days:
            cur = d.selected_food if d.reserved else None
            days.append({
                "date": d.date, "weekday": d.weekday, "active": d.active, "locked": d.locked,
                "reserved": d.reserved, "amount": d.amount or 0, "curFood": cur.name if cur else None,
                "selves": [{"id": s.id, "name": s.name,
                            "foods": [{"id": f.id, "name": f.name, "price": f.price or 0,
                                       "cur": bool(d.reserved and s.selected and f.selected)} for f in s.foods]}
                           for s in d.selves if s.foods],
            })
        meals.append({"key": m.key, "title": m.title, "days": days})
    return {"week": menu.week_range, "credit": menu.credit or 0, "meals": meals, "preselect": preselect or {}}


def selections_to_choices(menu: WeekMenu, selections: list[dict]) -> list[Choice]:
    """انتخاب‌های صفحه -> Choice. نام و قیمت فقط از داده‌ی خود برنامه گرفته می‌شود، نه از صفحه."""
    choices = []
    seen = set()
    for sel in selections:
        key = (sel.get("meal"), sel.get("date"))
        if key in seen:
            raise ValueError("برای یک وعده در یک روز بیش از یک انتخاب فرستاده شد")
        seen.add(key)
        day, meal = find_day(menu, *key), find_meal(menu, key[0])
        if day is None or meal is None or not day.active or day.locked:
            raise ValueError(f"{key[1]} / {key[0]} قابل ویرایش نیست")
        value = str(sel.get("value", "none"))
        if value == "cur":
            continue
        if value == "none":
            if day.reserved:
                choices.append(Choice(day.date, meal.key, False, meal_title=meal.title, weekday=day.weekday))
            continue
        self_id, _, food_id = value.partition("|")
        s = next((x for x in day.selves if x.id == self_id), None)
        f = next((x for x in (s.foods if s else []) if x.id == food_id), None)
        if f is None:
            raise ValueError(f"گزینه‌ی انتخاب‌شده برای {day.date} / {meal.key} معتبر نیست")
        ch = Choice(day.date, meal.key, True, s.id, f.id, meal.title, day.weekday, s.name, f.name, f.price or 0)
        if not _is_noop(day, ch):
            choices.append(ch)
    return choices


# ------------------------------------------------------------------ سرور
class SelectionServer:
    def __init__(self, session, menu: WeekMenu, relogin, pay_opener=None, refetch=None, week_key=None,
                 close_pay_window=False, exit_when_done=False):
        """
        pay_opener(session, amount) -> شیئی با close() و wait_for_result(stop) که پنجره‌ی پرداخت اینترنتی را
        باز می‌کند و بعد از برگشت از بانک نتیجه را برمی‌گرداند (اختیاری؛ بدون آن بخش پرداخت پنهان است).
        refetch(session, week, on_progress) -> WeekMenu و week_key ("current"/"next"): برای دکمه‌ی «دریافت مجدد».
        close_pay_window: بعد از نتیجه‌ی پرداخت، پنجره‌ی پرداخت خودکار بسته شود؟ (پیش‌فرض: نه)
        exit_when_done:  بعد از ثبت موفق، خودِ برنامه (سرور محلی) بسته شود؟ (پیش‌فرض: نه)
        """
        self.session, self.menu, self._relogin = session, menu, relogin
        self.pay_opener = pay_opener
        self._refetch, self.week_key = refetch, week_key
        self.close_pay_window, self.exit_when_done = close_pay_window, exit_when_done
        self._kept_browsers: list = []  # پنجره‌های پرداختی که عمداً باز نگه داشته شده‌اند
        self.preselect: dict = {}
        self.notice: str | None = None  # مثلاً «هفته بعد هنوز غذایی ندارد؛ هفته جاری نمایش داده شد»
        self.pay_browser = None
        self.pay_opening = False  # از لحظه‌ی کلیک تا باز شدن پنجره؛ مانع دو بار باز شدن می‌شود
        self.pending_choices: list[Choice] = []
        self.shortfall = 0
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.job = {"job": "idle", "log": [], "results": None, "ok": None, "paid": None, "pay": None,
                    "progress": None, "data_version": 0, "error": None}
        self.httpd: ThreadingHTTPServer | None = None

    # -- کمکی‌ها
    def relogin(self):
        self.session = self._relogin()
        return self.session

    def _log(self, text: str) -> None:
        print(text)
        self.job["log"] = (self.job["log"] + [text])[-300:]

    def _with_relogin(self, fn):
        try:
            return fn()
        except PermissionError:
            self.relogin()
            return fn()

    def _busy(self) -> bool:
        return self.job["job"] in ("submitting", "paying", "reloading") or self.pay_opening or self.pay_browser is not None

    def _finish_soon(self) -> None:
        if self.exit_when_done and self.httpd:
            threading.Timer(4.0, self.httpd.shutdown).start()

    def _progress(self, n: int, total: int) -> None:
        self.job["progress"] = {"n": n, "total": total}

    def _close_pay(self) -> None:
        if self.pay_browser is not None:
            try:
                self.pay_browser.close()
            finally:
                self.pay_browser = None

    def _release_pay(self) -> None:
        """بعد از نتیجه‌ی پرداخت: پنجره را می‌بندد یا (پیش‌فرض) باز می‌گذارد و فقط از کنترل برنامه جدا می‌کند."""
        if self.close_pay_window:
            self._close_pay()
        elif self.pay_browser is not None:
            self._kept_browsers.append(self.pay_browser)
            self.pay_browser = None

    def _close_kept(self) -> None:
        for b in self._kept_browsers:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
        self._kept_browsers.clear()

    # -- ثبت رزرو
    def api_submit(self, body: dict) -> dict:
        if self._busy():
            return {"state": "error", "message": "یک عملیات در حال انجام است؛ صبر کن تمام شود."}
        with self.lock:
            choices = selections_to_choices(self.menu, body.get("selections", []))
            if not choices:
                return {"state": "nochange"}
            fresh, html = self._with_relogin(lambda: open_week_state(self.session, choices))
            delta, credit = price_delta(fresh, choices), fresh.credit or 0
            if self.menu:
                self.menu.credit = credit
            base = {"lines": summary_lines(fresh, choices), "delta": delta, "credit": credit}

            if delta <= credit:
                self.job.update(job="submitting", log=[], results=None, ok=None, paid=None, pay=None)
                threading.Thread(target=self._submit_job, args=(choices, html), daemon=True).start()
                return {"state": "started", **base}

            save_pending(choices)
            self.pending_choices, self.shortfall = choices, delta - credit
            return {"state": "insufficient", "shortfall": self.shortfall, "canPay": self.pay_opener is not None, **base}

    def _submit_job(self, choices: list[Choice], html: str | None = None) -> None:
        """ثبت مرحله‌به‌مرحله روی سایت؛ پیشرفت زنده از طریق self.job['log'] به صفحه می‌رسد.
        html: صفحه‌ی هفته که همین لحظه باز شده (دوباره باز نمی‌شود). بعد از ورود دوباره دیگر معتبر نیست."""
        first = [html]

        def run():
            h, first[0] = first[0], None
            return submit(self.session, choices, on_step=self._log, html=h)
        try:
            results = self._with_relogin(run)
        except Exception as e:  # noqa: BLE001
            self._log(f"خطا: {type(e).__name__}: {e}")
            results = [{"meal": "-", "ok": False, "message": None, "problems": [f"{type(e).__name__}: {e}"]}]
        ok = all(r["ok"] for r in results)
        self.job.update(job="finished", results=results, ok=ok)
        if ok:
            clear_pending()
            self._finish_soon()

    # -- بروزرسانی دستی موجودی (دکمه‌ی صفحه)
    def api_credit(self) -> dict:
        if self.job["job"] == "submitting":
            return {"state": "error", "message": "ثبت رزرو در حال انجام است؛ بعد از پایانش بروزرسانی کن."}
        credit = self._with_relogin(lambda: read_credit(self.session))
        if credit is None:
            return {"state": "error", "message": "موجودی از صفحه خوانده نشد."}
        if self.menu:
            self.menu.credit = credit
        return {"state": "credit", "credit": credit}

    # -- پرداخت اینترنتی
    def _start_pay(self, amount: int, choices) -> dict:
        if self.pay_opener is None:
            return {"state": "error", "message": "پرداخت اینترنتی در این اجرا فعال نیست."}
        if self._busy():
            return {"state": "error", "message": "یک عملیات (یا پنجره‌ی پرداخت) از قبل باز است."}
        self.stop.clear()
        self.pay_opening = True
        self.job.update(job="paying", log=[], results=None, ok=None, paid=None, pay=None)
        threading.Thread(target=self._pay_job, args=(amount, choices), daemon=True).start()
        return {"state": "paying", "amount": amount}

    def api_pay(self, body: dict) -> dict:
        """پرداخت برای جبران کمبود؛ بعد از پرداخت موفق (و کافی بودن اعتبار) همان انتخاب‌ها ثبت می‌شوند."""
        if not self.pending_choices or self.shortfall <= 0:
            return {"state": "error", "message": "ابتدا انتخاب‌ها را ثبت کن تا کمبود موجودی مشخص شود."}
        amount = _parse_amount(body.get("amount"))
        if amount < self.shortfall:
            return {"state": "error", "message": f"مبلغ باید حداقل برابر کمبود ({self.shortfall:,} ریال) باشد."}
        return self._start_pay(amount, list(self.pending_choices))

    def api_topup(self, body: dict) -> dict:
        """پرداخت مستقل با مبلغ دلخواه (مثلاً برای آزمایش درگاه)؛ بدون ثبت رزرو."""
        amount = _parse_amount(body.get("amount"))
        if amount <= 0:
            return {"state": "error", "message": "مبلغ نامعتبر است."}
        return self._start_pay(amount, None)

    def _pay_job(self, amount: int, choices) -> None:
        self._log(f"در حال باز کردن پنجره‌ی پرداخت اینترنتی ({amount:,} ریال) ...")
        try:
            self.pay_browser = self.pay_opener(self.session, amount)
        except Exception as e:  # noqa: BLE001
            self._log(f"خطا در باز کردن پنجره‌ی پرداخت: {type(e).__name__}: {e}")
            self.job.update(job="idle")
            return
        finally:
            self.pay_opening = False
        self._log("پنجره‌ی پرداخت باز شد و مبلغ تأیید شد. کارت و رمز را فقط در همان پنجره (صفحه‌ی بانک) وارد کن. "
                  "بعد از برگشت به سایت، نتیجه خوانده می‌شود و (در صورت پرداخت موفق) ادامه می‌دهم.")
        try:
            res = self.pay_browser.wait_for_result(self.stop)  # فقط آدرس پنجره را می‌بیند، به سایت درخواست نمی‌زند
        except Exception as e:  # noqa: BLE001
            self._log(f"خطا در پایش پنجره‌ی پرداخت: {type(e).__name__}: {e}")
            res = {"closed": True}
        if res is None:  # لغو شد (api_cancel خودش پنجره را بسته)
            self.job.update(job="idle")
            return
        self._release_pay()
        if res.get("closed"):
            self._log("پنجره‌ی پرداخت قبل از برگشت از بانک بسته شد. اگر پرداخت کرده‌ای «بروزرسانی موجودی» را بزن.")
            self.job.update(job="idle")
            return

        self._log(f"نتیجه‌ی تراکنش: {res['result']} - {res['message']}"
                  + (f" - کد پیگیری: {res['trace']}" if res.get("trace") else ""))
        if not res["ok"]:
            self.job.update(job="payfailed", pay=res)
            return

        # فقط یک بار، بعد از برگشت از بانک، اعتبار از سایت خوانده می‌شود
        before = self.menu.credit if self.menu else None
        try:
            html = None
            if choices:
                fresh, html = self._with_relogin(lambda: open_week_state(self.session, choices))
                credit, delta = fresh.credit or 0, price_delta(fresh, choices)
            else:
                credit, delta = self._with_relogin(lambda: read_credit(self.session)), 0
        except Exception as e:  # noqa: BLE001
            self._log(f"پرداخت موفق بود ولی خواندن اعتبار خطا داد ({type(e).__name__}: {e}). «بروزرسانی موجودی» را بزن.")
            self.job.update(job="idle")
            return
        if self.menu:
            self.menu.credit = credit
        paid = {"before": before, "after": credit, "trace": res.get("trace", "")}
        self._log(f"اعتبار پس از پرداخت: {credit:,} ریال")
        if choices and delta <= credit:
            self.job.update(job="submitting", paid=paid)
            self._submit_job(choices, html)
        else:
            self.job.update(job="paid", paid=paid)
            if choices:
                self._log(f"اعتبار هنوز برای انتخاب‌ها کافی نیست (لازم: {delta:,} ریال).")

    # -- دریافت مجدد منو از سایت
    def _reload_menu(self):
        wk = self.week_key or "current"
        menu = self._refetch(self.session, wk, self._progress)
        if wk == "next" and not menu.has_food:  # هفته بعد هنوز خالی است: همان جایگزینیِ هفته جاری
            current = self._refetch(self.session, "current", self._progress)
            if current.has_food:
                return current, fallback_notice(menu, current)
        return menu, None

    def api_reload(self, body: dict | None = None) -> dict:
        if self._refetch is None:
            return {"state": "error", "message": "دریافت مجدد در این اجرا فعال نیست."}
        if self._busy():
            return {"state": "error", "message": "یک عملیات در حال انجام است؛ صبر کن تمام شود."}
        self.job.update(job="reloading", log=[], error=None, progress=None)
        threading.Thread(target=self._reload_job, daemon=True).start()
        return {"state": "reloading"}

    def _reload_job(self) -> None:
        try:
            self._log("در حال دریافت دوباره‌ی منو از سایت ...")
            menu, notice = self._with_relogin(self._reload_menu)
            self.menu, self.notice = menu, notice
            self.job["data_version"] += 1
            self.job.update(job="idle", progress=None)
        except Exception as e:  # noqa: BLE001
            self.job.update(job="idle", progress=None, error=f"دریافت مجدد ناموفق: {type(e).__name__}: {e}")

    def data(self) -> dict:
        d = build_data(self.menu, self.preselect)
        d.update(canPay=self.pay_opener is not None, notice=self.notice, version=self.job["data_version"],
                 exitWhenDone=self.exit_when_done, canReload=self._refetch is not None)
        return d

    def api_cancel(self) -> dict:
        self.stop.set()
        self._close_pay()
        if self.job["job"] == "paying":
            self.job.update(job="idle")
        return {"state": "cancelled"}

    # -- HTTP
    def page(self) -> bytes:
        data = json.dumps(self.data(), ensure_ascii=False).replace("</", "<\\/")
        data = data.replace("\u2028", "\\u2028")
        html = (PAGE.replace("__THEME_BOOT__", THEME_BOOT).replace("__BASE_CSS__", BASE_CSS).replace("__GRID_CSS__", GRID_CSS)
                .replace("__THEME_JS__", THEME_TOGGLE_JS).replace("__THEME_BUTTON__", THEME_BUTTON)
                .replace("__TOKEN__", self.token).replace("__DATA__", data))
        return html.encode("utf-8")

    def status(self) -> dict:
        return self.job

    def extra_get(self, path: str):
        """قلاب برای زیرکلاس‌ها: (بدنه، content-type) یا None."""
        return None

    def routes(self) -> dict:
        return {"/api/submit": self.api_submit, "/api/pay": self.api_pay, "/api/topup": self.api_topup,
                "/api/credit": lambda body: self.api_credit(), "/api/cancel": lambda body: self.api_cancel(),
                "/api/reload": self.api_reload}

    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # بی‌صدا
                pass

            def _ok_host(self) -> bool:
                return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

            def _ok_token_query(self, u) -> bool:
                return secrets.compare_digest(parse_qs(u.query).get("t", [""])[0], srv.token)

            def _ok_token(self) -> bool:
                return secrets.compare_digest(self.headers.get("X-Token", ""), srv.token)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self):
                u = urlparse(self.path)
                if not self._ok_host():
                    return self._send(403, b"forbidden", "text/plain")
                if u.path == "/" and secrets.compare_digest(parse_qs(u.query).get("t", [""])[0], srv.token):
                    return self._send(200, srv.page(), "text/html; charset=utf-8")
                if u.path == "/api/status" and self._ok_token():
                    return self._json(srv.status())
                if u.path == "/api/data" and self._ok_token() and srv.menu is not None:
                    return self._json(srv.data())
                extra = srv.extra_get(u.path) if self._ok_token_query(u) else None
                if extra:
                    return self._send(200, extra[0], extra[1])
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if not (self._ok_host() and self._ok_token()):
                    return self._send(403, b"forbidden", "text/plain")
                routes = srv.routes()
                try:
                    n = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if self.path in routes:
                        return self._json(routes[self.path](body))
                    self._send(404, b"not found", "text/plain")
                except Exception as e:  # noqa: BLE001
                    self._json({"state": "error", "message": f"{type(e).__name__}: {e}"})

        return Handler

    def start(self) -> str:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.token}"

    def run(self, launch: bool = True) -> None:
        url = self.start()
        print(f"\nصفحه‌ی انتخاب غذا: {url}")
        if launch:
            open_browser(url)
        print("بعد از انتخاب همه‌ی غذاها در صفحه روی «ثبت نهایی» بزن. توقف برنامه: Ctrl+C")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nمتوقف شد.")
        finally:
            self.stop.set()
            self._close_pay()
            self._close_kept()
            self.httpd.server_close()


# ------------------------------------------------------------------ صفحه (ES5 تا همه‌جا کار کند)
PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>انتخاب غذا</title>
__THEME_BOOT__
<style>
__BASE_CSS__
__GRID_CSS__
body{padding-bottom:170px}
#out{display:none;margin:4px 0 12px}
label.opt{display:flex;align-items:baseline;gap:8px;padding:4px 8px;border-radius:6px;cursor:pointer}
label.opt:hover{background:var(--sunken)}
label.opt.cur .tx{font-weight:700}
label.opt.chosen{background:var(--saffron-soft);box-shadow:inset 0 0 0 1.5px var(--saffron)}
td.cancel{position:relative;padding:0;width:104px;min-width:104px}
.cbtn{position:absolute;inset:0;display:block;cursor:pointer}
.cbtn span{display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:4px;text-align:center;
  color:var(--muted);font-size:13px;font-weight:700}
.cbtn:hover span{background:var(--sunken)}
tr.res .cbtn input:checked+span{background:var(--brick-soft);color:var(--brick)}
.cbtn input:focus-visible+span{outline:3px solid var(--tile);outline-offset:-3px}

#dock{position:fixed;inset-inline:0;bottom:0;z-index:10;background:var(--surface);border-top:1px solid var(--line)}
.dockin{position:relative;display:flex;align-items:center;flex-wrap:wrap;gap:12px 28px;padding:12px clamp(12px,2.2vw,32px)}
.ledger{flex:1 1 360px;min-width:280px}
.meter{display:flex;height:14px;border-radius:99px;background:var(--sunken);overflow:hidden}
.meter i{display:block;height:100%;transition:width .25s ease}
#mRemain{background:var(--tile)}#mSpend{background:var(--saffron)}
.meter.short #mSpend{background:var(--brick)}
#nums{display:flex;flex-wrap:wrap;gap:4px 28px;margin:8px 0 0;padding:0}
#nums div{display:flex;flex-direction:column;line-height:1.35}
#nums dt{font-size:12px;color:var(--muted)}
#nums dd{margin:0;font-weight:700;font-size:18px;direction:ltr;unicode-bidi:isolate;text-align:right}
.bad{color:var(--brick)}.good{color:var(--leaf)}
.pay{display:flex;align-items:center;gap:8px}
.pay[hidden]{display:none}
#paytoggle{display:none}
.pay label{color:var(--muted);font-size:13px}
.pay input{width:124px;text-align:left;direction:ltr}
.pay.need input{border-color:var(--brick)}
.pay .unit{color:var(--muted);font-size:13px}
.acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.chip{background:var(--saffron-soft);color:var(--ink);border:1px solid var(--saffron);border-radius:99px;padding:4px 14px;
  font-weight:700;cursor:pointer}
.chip[hidden]{display:none}
#chg{position:absolute;bottom:100%;inset-inline-end:clamp(12px,2.2vw,32px);margin:0;padding:10px 30px 10px 16px;min-width:320px;
  max-height:260px;overflow:auto;background:var(--surface);border:1px solid var(--line);border-bottom:0;border-radius:12px 12px 0 0;
  font-size:14px}
#chg[hidden]{display:none}
@media (max-width:640px){.pay input{width:100px}.pay:not(.open):not(.need){display:none}#paytoggle{display:inline-block}
  .dockin{gap:8px 16px;padding-top:10px;padding-bottom:10px}#nums dd{font-size:16px}.acts{width:100%}.acts .btn{padding:6px 12px;font-size:13px}.acts #go{order:-1;flex:1 1 150px;font-size:15px;padding:9px 14px}}
</style></head><body>
<div class="wrap">
<header class="top">
  <div><span class="eyebrow">برنامه غذایی</span><h1><bdi id="week"></bdi></h1></div>
  <div class="tools">
    <button id="reload" class="btn ghost" type="button" style="display:none">دریافت مجدد</button>
    __THEME_BUTTON__
  </div>
</header>
<div id="out" role="status" aria-live="polite"></div>
<nav id="tabs" role="tablist" aria-label="وعده‌ها"></nav>
<main id="meals"></main>
</div>
<footer id="dock"><div class="dockin">
  <div class="ledger">
    <div id="meter" class="meter" aria-hidden="true"><i id="mRemain"></i><i id="mSpend"></i></div>
    <dl id="nums"></dl>
  </div>
  <div id="payrow" class="pay" hidden>
    <label for="payamt">پرداخت</label>
    <input id="payamt" inputmode="numeric" autocomplete="off" value="10,000" aria-label="مبلغ پرداخت (ریال)">
    <span class="unit">ریال</span>
    <button id="pay" class="btn secondary" type="button">پرداخت اینترنتی</button>
    <button id="stopwait" class="btn ghost" type="button" style="display:none">لغو پرداخت</button>
  </div>
  <div class="acts">
    <button id="paytoggle" class="btn ghost" type="button" aria-expanded="false" hidden>پرداخت</button>
    <button id="refreshcredit" class="btn ghost" type="button">بروزرسانی موجودی</button>
    <button id="changes" class="chip" type="button" aria-expanded="false" hidden></button>
    <button id="go" class="btn primary" type="button" disabled>ثبت نهایی</button>
  </div>
  <ul id="chg" hidden></ul>
</div></footer>
<script>
var DATA = __DATA__;
var TOKEN = "__TOKEN__";
__THEME_JS__
var rows = [], busy = false, poll = null, preApplied = 0, activeTab = -1, lastShort = 0;
var dataVersion = DATA.version || 0, autoReload = false, manualReload = false;

function h(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function $(id){ return document.getElementById(id); }
function fmt(n){ return String(Math.abs(Math.round(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function sgn(n){ return (n<0?"\u2212":(n>0?"+":"")) + fmt(n); }
function parseAmt(t){ return parseInt(String(t).replace(/[^0-9]/g,""),10) || 0; }

function selvesOf(meal){      // ستون‌ها: هر سلفی که در این وعده در هر روزی غذا دارد (به ترتیب دیده‌شدن)
  var seen = {}, list = [];
  meal.days.forEach(function(d){ d.selves.forEach(function(s){ if(!seen[s.id]){ seen[s.id]=1; list.push({id:s.id, name:s.name}); } }); });
  return list;
}

function buildRow(meal, day, selves, tb){
  var tr = h("tr", day.reserved ? "res" : ""), dayTd = h("td","day");
  dayTd.appendChild(h("strong","",day.weekday)); dayTd.appendChild(h("span","dt",day.date));
  if(day.reserved) dayTd.appendChild(h("span","badge","رزرو شده"));
  tr.appendChild(dayTd); tb.appendChild(tr);
  var span = selves.length + 1, td;
  if(!day.active){ tr.className = "off"; td = h("td","msg","—"); td.colSpan = span; tr.appendChild(td); return; }
  if(day.locked){ td = h("td","msg", day.reserved ? ("قفل‌شده · " + (day.curFood||"")) : "قفل‌شده"); td.colSpan = span; tr.appendChild(td); return; }

  var name = meal.key + "|" + day.date, info = {meal: meal, day: day, name: name, initial: day.reserved ? "cur" : "none", prices: {}, labels: {}, tr: tr};
  var hasCur = false;
  day.selves.forEach(function(s){ s.foods.forEach(function(f){ if(f.cur) hasCur = true; }); });
  function radio(value, checked){ var i = h("input"); i.type="radio"; i.name=name; i.value=value; i.checked=checked; i.onchange=refresh; return i; }

  if(day.reserved && !hasCur){   // رزرو فعلی در فهرست نیست: گزینه‌ی «بدون تغییر»
    info.prices["cur"] = day.amount;
    var cl = h("label","opt cur"); cl.setAttribute("data-base","opt cur");
    cl.appendChild(radio("cur", true)); cl.appendChild(h("span","tx","بدون تغییر: " + (day.curFood||"")));
    dayTd.appendChild(cl);
  }
  selves.forEach(function(sv){
    var cell = h("td","cell"), ds = null;
    day.selves.forEach(function(s){ if(s.id===sv.id) ds = s; });
    if(ds){
      ds.foods.forEach(function(f){
        var value = sv.id + "|" + f.id, l = h("label","opt" + (f.cur ? " cur" : ""));
        l.setAttribute("data-base", "opt" + (f.cur ? " cur" : ""));
        var tx = h("span","tx",f.name);
        if(f.cur){ tx.appendChild(h("span","tick","✓")); info.initial = value; }
        l.appendChild(radio(value, f.cur)); l.appendChild(tx); l.appendChild(h("span","pr num",fmt(f.price)));
        info.prices[value] = f.price; info.labels[value] = sv.name + " / " + f.name;
        cell.appendChild(l);
      });
    } else { cell.className = "cell empty"; cell.textContent = "·"; }
    tr.appendChild(cell);
  });
  // لغو رزرو: آخرین ستون و کل سلول
  var ct = h("td","cancel"), cb = h("label","cbtn");
  var ci = radio("none", !day.reserved); ci.className = "vh";
  cb.appendChild(ci); cb.appendChild(h("span","", day.reserved ? "لغو رزرو" : "رزرو نشود")); ct.appendChild(cb); tr.appendChild(ct);
  info.labels["none"] = day.reserved ? "لغو رزرو" : "رزرو نشود";

  var pv = DATA.preselect && DATA.preselect[name];
  if(pv){
    var els = tr.getElementsByTagName("input"), has = false, k;
    for(k=0;k<els.length;k++) if(els[k].value===pv) has = true;
    if(has){ for(k=0;k<els.length;k++) els[k].checked = (els[k].value===pv); preApplied++; }
  }
  rows.push(info);
}

function build(){
  rows = []; preApplied = 0;
  var root = $("meals"), tabs = $("tabs"); root.innerHTML = ""; tabs.innerHTML = "";
  $("week").textContent = DATA.week || "";
  DATA.meals.forEach(function(meal, mi){
    var panel = h("div","panel"), btn = h("button","tabbtn");
    panel.id = "panel_" + mi; panel.setAttribute("role","tabpanel"); panel.style.display = "none";
    btn.type = "button"; btn.id = "tab_" + mi; btn.setAttribute("role","tab"); btn.textContent = meal.title;
    btn.onclick = (function(i){ return function(){ showTab(i); }; })(mi);
    tabs.appendChild(btn); root.appendChild(panel);
    var selves = selvesOf(meal), wrap = h("div","gridwrap"), table = h("table","grid"), thead = h("thead"), hr = h("tr");
    hr.appendChild(h("th","dayh","روز"));
    selves.forEach(function(s){ hr.appendChild(h("th","",s.name)); });
    hr.appendChild(h("th","cancelh","لغو رزرو"));
    thead.appendChild(hr); table.appendChild(thead);
    var tb = h("tbody");
    meal.days.forEach(function(day){ buildRow(meal, day, selves, tb); });
    table.appendChild(tb); wrap.appendChild(table); panel.appendChild(wrap);
  });
  var first = 0, i, j;      // اولین تبی که روزِ قابل‌ویرایش دارد
  for(i=0;i<DATA.meals.length;i++){
    var ok = false;
    for(j=0;j<DATA.meals[i].days.length;j++){ var d = DATA.meals[i].days[j]; if(d.active && !d.locked && d.selves.length) ok = true; }
    if(ok){ first = i; break; }
  }
  showTab(activeTab >= 0 && activeTab < DATA.meals.length ? activeTab : first);
}

function showTab(k){
  activeTab = k;
  for(var i=0;i<DATA.meals.length;i++){
    $("panel_" + i).style.display = (i===k) ? "block" : "none";
    var b = $("tab_" + i); b.className = "tabbtn" + (i===k ? " on" : ""); b.setAttribute("aria-selected", i===k ? "true" : "false");
  }
}

function current(info){
  var els = document.getElementsByName(info.name);
  for(var i=0;i<els.length;i++) if(els[i].checked) return els[i].value;
  return info.initial;
}

function calc(){
  var delta = 0, list = [], byMeal = {};
  rows.forEach(function(r){
    var v = current(r), old = r.day.reserved ? r.day.amount : 0, nw, changed = (v !== r.initial);
    var labels = r.tr.getElementsByTagName("label");
    for(var i=0;i<labels.length;i++){
      var base = labels[i].getAttribute("data-base");
      if(base) labels[i].className = base + (changed && labels[i].getElementsByTagName("input")[0].checked ? " chosen" : "");
    }
    if(!changed) return;
    nw = (v==="none") ? 0 : r.prices[v];
    delta += nw - old;
    byMeal[r.meal.key] = (byMeal[r.meal.key] || 0) + 1;
    list.push(r.day.weekday + " " + r.day.date + " · " + r.meal.title + ": " + (v==="none" ? "لغو رزرو" : r.labels[v]));
  });
  return {delta: delta, list: list, byMeal: byMeal};
}

function refresh(){
  var c = calc(), credit = DATA.credit, after = credit - c.delta, short = c.delta - credit;
  // ترازنامه‌ی موجودی: فیروزه‌ای = می‌ماند، زعفرانی = خرج می‌شود، آجری = کمبود
  var remainPct = credit > 0 ? Math.max(0, Math.min(100, after / credit * 100)) : 0;
  var spendPct = credit > 0 ? Math.max(0, Math.min(100 - remainPct, Math.max(0, c.delta) / credit * 100)) : 0;
  $("mRemain").style.width = short > 0 ? "0%" : (c.delta <= 0 ? 100 : remainPct) + "%";
  $("mSpend").style.width = short > 0 ? "100%" : spendPct + "%";
  $("meter").className = "meter" + (short > 0 ? " short" : "");
  var nums = $("nums"); nums.innerHTML = "";
  function item(label, val, cls){ var d = h("div"), dt = h("dt","",label), dd = h("dd","num " + (cls||""),val); d.appendChild(dt); d.appendChild(dd); nums.appendChild(d); }
  item("موجودی", fmt(credit), "");
  item("تغییر", sgn(c.delta), "");
  item("پس از ثبت", (after<0 ? "\u2212" : "") + fmt(after), after < 0 ? "bad" : "good");
  if(short > 0) item("کمبود", fmt(short), "bad");
  DATA.meals.forEach(function(m, i){
    var b = $("tab_" + i), n = c.byMeal[m.key] || 0; b.textContent = m.title;
    if(n) b.appendChild(h("span","n num",String(n)));
  });
  var ul = $("chg"); ul.innerHTML = "";
  c.list.forEach(function(t){ ul.appendChild(h("li","",t)); });
  var chip = $("changes"); chip.hidden = c.list.length === 0; chip.textContent = c.list.length + " تغییر";
  if(!c.list.length){ ul.hidden = true; chip.setAttribute("aria-expanded","false"); }
  $("go").disabled = busy || c.list.length === 0;
  ["reload","refreshcredit","pay"].forEach(function(id){ $(id).disabled = busy; });
  document.body.style.paddingBottom = ($("dock").offsetHeight + 24) + "px";
}

function api(method, path, body, cb){
  var x = new XMLHttpRequest();
  x.open(method, path); x.setRequestHeader("X-Token", TOKEN); x.setRequestHeader("Content-Type", "application/json");
  x.onload = function(){ var j; try{ j = JSON.parse(x.responseText); }catch(e){ j = {state:"error", message:"پاسخ نامعتبر از برنامه"}; } cb(j); };
  x.onerror = function(){ cb({state:"error", message:"ارتباط با برنامه قطع شد (برنامه بسته شده؟)"}); };
  x.send(body ? JSON.stringify(body) : null);
}

function bar(pct){
  var p = h("div", "progress" + (pct < 0 ? " ind" : "")), i = h("i");
  p.setAttribute("role","progressbar"); p.setAttribute("aria-valuemin","0"); p.setAttribute("aria-valuemax","100");
  if(pct >= 0){ i.style.width = Math.round(pct) + "%"; p.setAttribute("aria-valuenow", String(Math.round(pct))); }
  p.appendChild(i); return p;
}
function show(kind, text, pct, details){
  var o = $("out"); o.className = "alert " + kind; o.style.display = "block"; o.innerHTML = "";
  o.appendChild(h("pre","",text));
  if(pct !== undefined && pct !== null) o.appendChild(bar(pct));
  if(details){ var d = h("details"); d.appendChild(h("summary","","مراحل")); d.appendChild(h("pre","",details)); o.appendChild(d); }
  if(o.scrollIntoView) o.scrollIntoView({block:"nearest"});
}
function hideOut(){ $("out").style.display = "none"; }
function mealTitle(key){ for(var i=0;i<DATA.meals.length;i++) if(DATA.meals[i].key===key) return DATA.meals[i].title; return key; }
function resultsText(res){
  var t = [];
  res.forEach(function(r){
    t.push((r.ok ? "✓ " : "✗ ") + mealTitle(r.meal) + (r.message ? " · " + r.message : ""));
    (r.problems||[]).forEach(function(p){ t.push("   ! " + p); });
    (r.warnings||[]).forEach(function(p){ t.push("   ؟ " + p); });
  });
  return t.join("\n");
}
function setBusy(b){ busy = b; $("go").textContent = b ? "در حال انجام ..." : "ثبت نهایی"; refresh(); }
function pctOf(j){ return j.progress && j.progress.total ? j.progress.n / j.progress.total * 100 : -1; }

function collectChanges(){   // انتخاب‌های تغییرکرده، برای حفظ بعد از «دریافت مجدد»
  var m = {};
  rows.forEach(function(r){ var v = current(r); if(v !== r.initial) m[r.name] = v; });
  return m;
}
function fetchData(){
  api("GET", "/api/data", null, function(j){
    if(!j.meals){ show("err", "دریافت داده‌ی تازه ناموفق بود."); return; }
    var keep = manualReload ? collectChanges() : {};
    DATA = j; DATA.preselect = keep; dataVersion = j.version; build(); refresh();
    if(manualReload){ show("ok", "منو از سایت بروز شد." + (DATA.notice ? "\n" + DATA.notice : "")); }
    else if(DATA.notice) show("info", DATA.notice);
    manualReload = false; autoReload = false; setBusy(false);
  });
}

function startPoll(){
  if(poll) clearInterval(poll);
  poll = setInterval(function(){ api("GET", "/api/status", null, handleStatus); }, 1200);
}
function stopPoll(){ if(poll){ clearInterval(poll); poll = null; } $("stopwait").style.display = "none"; }
function tail(log, n){ var l = log ? log.split("\n") : []; return l.slice(-n).join("\n"); }

function handleStatus(j){
  var log = (j.log || []).join("\n"), stop = $("stopwait");
  if(j.data_version !== dataVersion){ dataVersion = j.data_version; stopPoll(); fetchData(); return; }
  if(j.job === "reloading"){ if(!autoReload) show("info", "در حال دریافت منو از سایت ...", pctOf(j)); return; }
  if(j.job === "paying"){ stop.style.display = ""; show("info", "پنجره‌ی پرداخت باز است؛ پرداخت را در صفحه‌ی بانک کامل کن.", -1, log); return; }
  if(j.job === "submitting"){ stop.style.display = "none"; show("info", "در حال ثبت روی سایت ...\n" + tail(log, 5), -1); return; }
  if(j.job === "paid"){
    stopPoll();
    if(j.paid && j.paid.after != null){ DATA.credit = j.paid.after; refresh(); }
    show("ok", "پرداخت موفق بود." + (j.paid && j.paid.after != null ? "\nاعتبار فعلی: " + fmt(j.paid.after) + " ریال" : "") + (j.paid && j.paid.trace ? "\nکد پیگیری: " + j.paid.trace : ""), null, log);
    $("payrow").className = "pay"; lastShort = 0; setBusy(false); return;
  }
  if(j.job === "payfailed"){ stopPoll(); show("err", "پرداخت ناموفق بود: " + ((j.pay && j.pay.message) || ""), null, log); setBusy(false); return; }
  if(j.job === "finished"){
    stopPoll();
    var head = j.ok ? "همه‌ی تغییرات ثبت و تأیید شد." : "بعضی موارد ثبت یا تأیید نشد:";
    if(j.paid && j.paid.after != null) head = "پرداخت موفق بود (اعتبار: " + fmt(j.paid.after) + " ریال).\n" + head;
    if(j.ok && DATA.exitWhenDone) head += "\nبرنامه تا چند ثانیه‌ی دیگر بسته می‌شود.";
    show(j.ok ? "ok" : "err", head + "\n" + resultsText(j.results || []), null, log);
    lastShort = 0; $("payrow").className = "pay";
    if(j.ok && DATA.canReload && !DATA.exitWhenDone){ autoReload = true; api("POST", "/api/reload", {}, function(){ startPoll(); }); }
    else setBusy(false);
    return;
  }
  if(j.job === "idle"){ stopPoll(); if(j.error) show("err", j.error); else if(log) show("info", log); setBusy(false); }
}

$("go").onclick = function(){
  var c = calc();
  if(!confirm("ثبت این تغییرات؟\n\n" + c.list.join("\n") + "\n\nجمع تغییر: " + sgn(c.delta) + " ریال")) return;
  var sels = rows.map(function(r){ return {meal: r.meal.key, date: r.day.date, value: current(r)}; });
  setBusy(true);
  api("POST", "/api/submit", {selections: sels}, function(j){
    if(j.state === "started"){ show("info", "شروع شد ...", -1); startPoll(); }
    else if(j.state === "insufficient"){
      DATA.credit = j.credit; lastShort = j.shortfall; refresh();
      show("info", "موجودی کافی نیست؛ کمبود: " + fmt(j.shortfall) + " ریال.\nانتخاب‌ها در pending.json ذخیره شد.");
      if(j.canPay){
        $("payamt").value = fmt(Math.max(10000, Math.ceil(j.shortfall / 1000) * 1000));
        $("payrow").className = "pay need"; $("payamt").focus();
      }
      setBusy(false);
    } else if(j.state === "nochange"){ show("info", "تغییری انتخاب نشده است."); setBusy(false); }
    else { show("err", j.message || "خطا"); setBusy(false); }
  });
};
$("pay").onclick = function(){
  var amt = parseAmt($("payamt").value);
  if(amt <= 0){ $("payamt").focus(); return; }
  var reserveAfter = lastShort > 0 && amt >= lastShort;
  if(!confirm("پرداخت " + fmt(amt) + " ریال" + (reserveAfter ? "\nبعد از پرداخت موفق، انتخاب‌ها خودکار ثبت می‌شوند." : "") + "؟")) return;
  api("POST", reserveAfter ? "/api/pay" : "/api/topup", {amount: amt}, function(j){
    if(j.state === "paying"){ busy = true; refresh(); show("info", "در حال باز کردن پنجره‌ی پرداخت ...", -1); startPoll(); }
    else show("err", j.message || "خطا");
  });
};
$("payamt").oninput = function(){ var n = parseAmt(this.value); this.value = n ? fmt(n) : ""; };
$("stopwait").onclick = function(){
  api("POST", "/api/cancel", {}, function(){ stopPoll(); show("info", "پرداخت لغو شد."); setBusy(false); });
};
$("refreshcredit").onclick = function(){
  var b = this; b.disabled = true;
  api("POST", "/api/credit", {}, function(j){
    b.disabled = false;
    if(j.state === "credit"){ DATA.credit = j.credit; refresh(); show("ok", "موجودی بروز شد: " + fmt(j.credit) + " ریال"); }
    else show("err", j.message || "خطا");
  });
};
$("reload").onclick = function(){
  manualReload = true; setBusy(true);
  api("POST", "/api/reload", {}, function(j){
    if(j.state === "reloading"){ show("info", "در حال دریافت منو از سایت ...", -1); startPoll(); }
    else { manualReload = false; show("err", j.message || "خطا"); setBusy(false); }
  });
};
$("paytoggle").onclick = function(){
  var r = $("payrow"), open = r.className.indexOf("open") < 0;
  r.className = "pay" + (open ? " open" : "") + (r.className.indexOf("need") >= 0 ? " need" : ""); this.setAttribute("aria-expanded", open ? "true" : "false"); refresh();
};
$("changes").onclick = function(){
  var ul = $("chg"); ul.hidden = !ul.hidden; this.setAttribute("aria-expanded", ul.hidden ? "false" : "true");
};
document.onkeydown = function(e){ if(e.key === "Escape"){ $("chg").hidden = true; $("changes").setAttribute("aria-expanded","false"); } };
window.onresize = function(){ document.body.style.paddingBottom = ($("dock").offsetHeight + 24) + "px"; };

if(DATA.canPay){ $("payrow").hidden = false; $("paytoggle").hidden = false; }
if(DATA.canReload) $("reload").style.display = "";
build(); refresh();
if(DATA.notice || preApplied > 0) show("info", (DATA.notice ? DATA.notice + "\n" : "") + (preApplied > 0 ? preApplied + " انتخاب ذخیره‌شده اعمال شد." : ""));
</script></body></html>
"""
