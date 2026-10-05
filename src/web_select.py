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

from food_menu import WeekMenu
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
    def __init__(self, session, menu: WeekMenu, relogin, pay_opener=None):
        """
        pay_opener(session, amount) -> شیئی با close() و wait_for_result(stop) که پنجره‌ی پرداخت اینترنتی را
        باز می‌کند و بعد از برگشت از بانک نتیجه را برمی‌گرداند (اختیاری؛ بدون آن بخش پرداخت پنهان است).
        """
        self.session, self.menu, self._relogin = session, menu, relogin
        self.pay_opener = pay_opener
        self.preselect: dict = {}
        self.notice: str | None = None  # مثلاً «هفته بعد هنوز غذایی ندارد؛ هفته جاری نمایش داده شد»
        self.pay_browser = None
        self.pay_opening = False  # از لحظه‌ی کلیک تا باز شدن پنجره؛ مانع دو بار باز شدن می‌شود
        self.pending_choices: list[Choice] = []
        self.shortfall = 0
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.job = {"job": "idle", "log": [], "results": None, "ok": None, "paid": None, "pay": None}
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
        return self.job["job"] in ("submitting", "paying") or self.pay_opening or self.pay_browser is not None

    def _finish_soon(self) -> None:
        if self.httpd:
            threading.Timer(4.0, self.httpd.shutdown).start()

    def _close_pay(self) -> None:
        if self.pay_browser is not None:
            try:
                self.pay_browser.close()
            finally:
                self.pay_browser = None

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
        self._close_pay()
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

    def api_cancel(self) -> dict:
        self.stop.set()
        self._close_pay()
        if self.job["job"] == "paying":
            self.job.update(job="idle")
        return {"state": "cancelled"}

    # -- HTTP
    def page(self) -> bytes:
        d = build_data(self.menu, self.preselect)
        d["canPay"] = self.pay_opener is not None
        d["notice"] = self.notice
        data = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
        return PAGE.replace("__DATA__", data).replace("__TOKEN__", self.token).encode("utf-8")

    def status(self) -> dict:
        return self.job

    def extra_get(self, path: str):
        """قلاب برای زیرکلاس‌ها: (بدنه، content-type) یا None."""
        return None

    def routes(self) -> dict:
        return {"/api/submit": self.api_submit, "/api/pay": self.api_pay, "/api/topup": self.api_topup,
                "/api/credit": lambda body: self.api_credit(), "/api/cancel": lambda body: self.api_cancel()}

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
            self.httpd.server_close()


# ------------------------------------------------------------------ صفحه (ES5 تا همه‌جا کار کند)
PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>انتخاب غذا</title>
<style>
body{font-family:Vazirmatn,Tahoma,sans-serif;max-width:960px;margin:0 auto;padding:12px 12px 230px;color:#222;background:#fff}
h1{font-size:20px;margin:8px 0}
#tabs{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 0;position:sticky;top:0;background:#fff;padding:6px 0;z-index:5;border-bottom:2px solid #2e7d32}
.tabbtn{background:#eceff1;color:#333;border:1px solid #cfd8dc;padding:7px 18px;border-radius:8px 8px 0 0;font-size:15px}
.tabbtn.on{background:#2e7d32;color:#fff;border-color:#2e7d32}.tabbtn .n{background:#ffb300;color:#222;border-radius:9px;padding:0 7px;font-size:12px;margin-right:6px}
.panel{margin-top:10px}
table{width:100%;border-collapse:collapse}td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:right}
td.day{white-space:nowrap;font-weight:bold;width:1%;background:#f6f6f6}td.day small{font-weight:normal;color:#666}
.badge{display:inline-block;background:#e8f5e9;color:#1b5e20;border-radius:4px;padding:0 6px;font-size:12px;margin-top:4px}
tr.off td{color:#999;background:#fafafa}tr.locked td.opts{background:#fafafa;color:#555}
.selfname{font-weight:bold;margin:8px 0 2px;color:#444}.selfname:first-child{margin-top:0}
label.opt{display:table;width:100%;box-sizing:border-box;padding:3px 4px;border-radius:5px;cursor:pointer}.rc{display:table-cell;width:26px;vertical-align:top;padding-top:2px}.tc{display:table-cell;vertical-align:top}label.opt:hover{background:#f3f3f3}
label.opt.chosen{background:#fff3cd}label.opt input{margin:0}
.price{color:#777;font-size:12px}.cur{font-weight:bold}.tick{color:#2e7d32;font-weight:bold}
.none{color:#a33}
#bar{position:fixed;right:0;left:0;bottom:0;background:#fff;border-top:2px solid #2e7d32;padding:10px 14px;box-shadow:0 -2px 8px rgba(0,0,0,.12)}
#bar .in{max-width:960px;margin:0 auto}
#nums span{display:inline-block;margin-left:18px}#nums b{font-size:16px}
.bad{color:#c62828}.good{color:#1b5e20}
#chg{margin:6px 0;padding:0 18px;font-size:13px;max-height:64px;overflow:auto;color:#444}
button{font-family:inherit;font-size:15px;padding:8px 18px;border:0;border-radius:6px;background:#2e7d32;color:#fff;cursor:pointer}
button:disabled{background:#aaa;cursor:not-allowed}button.sec{background:#757575}
#topup{margin:8px 0;padding:6px 10px;border:1px solid #bbdefb;border-radius:6px;background:#f5faff}#topup summary{cursor:pointer;font-weight:bold;color:#1565c0}.topbox{margin-top:8px}.topbox .hint{color:#666;font-size:12px;margin-top:4px}
#payrow{display:none;margin:6px 0;font-size:14px}#payrow .hint{color:#666;font-size:12px;margin-top:2px}button.pay{background:#1565c0;padding:5px 14px;font-size:14px}
button.small{font-size:12px;padding:3px 10px}#numsrow{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
#out{margin:10px 0;padding:10px;border-radius:6px;display:none;white-space:pre-wrap;line-height:1.8}
#out.ok{background:#e8f5e9;border:1px solid #a5d6a7}#out.err{background:#ffebee;border:1px solid #ef9a9a}#out.info{background:#fff8e1;border:1px solid #ffe082}
</style></head><body>
<h1>انتخاب غذا - هفته <bdi id="week"></bdi></h1>
<p style="color:#666;font-size:13px">برای هر وعده در هر روز فقط یک گزینه می‌توانی انتخاب کنی. غذای پررنگ با ✓ سبز، رزرو فعلی توست. هیچ چیز تا زدن «ثبت نهایی» روی سایت اعمال نمی‌شود.</p>
<details id="topup" style="display:none"><summary>افزایش اعتبار / آزمایش پرداخت اینترنتی (مستقل از انتخاب غذا)</summary>
 <div class="topbox">مبلغ <input id="topamt" type="text" size="12" value="10,000"> ریال
 <button id="topgo" class="pay">رفتن به درگاه پرداخت</button>
 <div class="hint">یک پنجره‌ی مرورگر جدا باز می‌شود و مبلغ در صفحه‌ی «افزایش اعتبار اینترنتی» پر می‌شود؛ کارت و رمز را فقط خودت در صفحه‌ی بانک وارد می‌کنی. بعد از برگشت از بانک، نتیجه‌ی تراکنش و اعتبار جدید همین‌جا اعلام می‌شود (رزروی انجام نمی‌شود).</div></div>
</details>
<div id="out"></div>
<div id="tabs"></div>
<div id="meals"></div>
<div id="bar"><div class="in">
 <div id="numsrow"><span id="nums"></span> <button id="refreshcredit" class="sec small" title="خواندن دوباره‌ی موجودی از سایت">بروزرسانی موجودی</button></div>
 <ul id="chg"></ul>
 <div id="payrow">پرداخت اینترنتی: مبلغ <input id="payamt" type="text" size="12"> ریال
  <button id="pay" class="pay">رفتن به درگاه پرداخت</button>
  <div class="hint">یک پنجره‌ی مرورگر جدا باز می‌شود و مبلغ در صفحه‌ی «افزایش اعتبار» پر می‌شود؛ کارت و رمز را فقط خودت در صفحه‌ی بانک وارد می‌کنی. بعد از برگشت از بانک و تأیید پرداخت، اعتبار یک بار بررسی می‌شود و در صورت کافی بودن، همین غذاها خودکار رزرو می‌شوند. اگر پرداخت را جای دیگری انجام دادی، «بروزرسانی موجودی» و بعد «ثبت نهایی» را بزن.</div></div>
 <button id="go" disabled>ثبت نهایی در سایت</button>
 <button id="stopwait" class="sec" style="display:none">لغو پرداخت</button>
</div></div>
<script>
var DATA = __DATA__;
var TOKEN = "__TOKEN__";
var rows = [];       // {meal, day, name, initial, prices:{value:price}, labels:{value:text}}
var busy = false, poll = null, preApplied = 0;

function h(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function fmt(n){ return String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function sgn(n){ return (n<0?"-":(n>0?"+":"")) + fmt(n); }

function build(){
  document.getElementById("week").textContent = DATA.week || "";
  var root = document.getElementById("meals"), tabs = document.getElementById("tabs");
  DATA.meals.forEach(function(meal, mi){
    var panel = h("div", "panel"), btn = h("button", "tabbtn");
    panel.id = "panel_" + mi; panel.style.display = "none";
    btn.type = "button"; btn.id = "tab_" + mi; btn.setAttribute("data-title", meal.title); btn.textContent = meal.title;
    btn.onclick = (function(i){ return function(){ showTab(i); }; })(mi);
    tabs.appendChild(btn); root.appendChild(panel);
    var table = h("table"), tb = h("tbody");
    meal.days.forEach(function(day){
      var tr = h("tr"), dayTd = h("td", "day"), opts = h("td", "opts");
      dayTd.appendChild(document.createTextNode(day.weekday));
      dayTd.appendChild(h("br")); dayTd.appendChild(h("small", "", day.date));
      if(day.reserved){ dayTd.appendChild(h("br")); dayTd.appendChild(h("span", "badge", "رزرو شده")); }
      tr.appendChild(dayTd); tr.appendChild(opts); tb.appendChild(tr);
      if(!day.active){ tr.className="off"; opts.textContent="—"; return; }
      if(day.locked){
        tr.className="locked";
        opts.textContent = day.reserved ? ("قفل‌شده - رزرو: " + (day.curFood||"")) : "قفل‌شده";
        return;
      }
      var name = meal.key + "|" + day.date;
      var info = {meal: meal, day: day, name: name, initial: day.reserved ? "cur" : "none", prices: {}, labels: {}, tr: tr};
      var hasCur = false;
      function radio(value, labelNode, checked, cls){
        var l = h("label", "opt " + (cls||"")), i = h("input");
        i.type="radio"; i.name=name; i.value=value; i.checked=checked;
        i.onchange = refresh;
        var rc=h("span","rc"), tc=h("span","tc"); rc.appendChild(i); tc.appendChild(labelNode); l.appendChild(rc); l.appendChild(tc); opts.appendChild(l); return l;
      }
      // اول گزینه‌ی «رزرو نشود»، بعد غذاهای هر سلف
      var noneLabel = h("span", "none", day.reserved ? "لغو رزرو (رزرو نشود)" : "رزرو نشود");
      day.selves.forEach(function(s){ s.foods.forEach(function(f){ if(f.cur) hasCur = true; }); });
      info.labels["none"] = day.reserved ? "لغو رزرو" : "رزرو نشود";
      radio("none", noneLabel, !day.reserved, "");
      if(day.reserved && !hasCur){
        info.prices["cur"] = day.amount;
        radio("cur", h("span", "cur", "رزرو فعلی بدون تغییر: " + (day.curFood||"")), true, "");
      }
      day.selves.forEach(function(s){
        opts.appendChild(h("div", "selfname", s.name));
        s.foods.forEach(function(f){
          var value = s.id + "|" + f.id, span = h("span");
          span.appendChild(document.createTextNode("- " + f.name + " "));
          span.appendChild(h("span", "price", fmt(f.price)));
          if(f.cur){ span.className="cur"; span.appendChild(document.createTextNode(" ")); span.appendChild(h("span","tick","✓")); info.initial = value; }
          info.prices[value] = f.price; info.labels[value] = s.name + " / " + f.name;
          radio(value, span, f.cur, "");
        });
      });
      var pv = DATA.preselect && DATA.preselect[meal.key + "|" + day.date];
      if(pv){
        var els = opts.getElementsByTagName("input"), has = false, k;  // جدول هنوز به صفحه وصل نشده؛ داخل همین ردیف جست‌وجو می‌کنیم
        for(k=0;k<els.length;k++) if(els[k].value===pv) has = true;
        if(has){ for(k=0;k<els.length;k++) els[k].checked = (els[k].value===pv); preApplied++; }
      }
      rows.push(info);
    });
    table.appendChild(tb); panel.appendChild(table);
  });
  // تب اولی که روزِ قابل‌ویرایش دارد باز می‌شود
  var first = 0, i, j;
  for(i=0;i<DATA.meals.length;i++){
    var ok = false;
    for(j=0;j<DATA.meals[i].days.length;j++){ var d = DATA.meals[i].days[j]; if(d.active && !d.locked && d.selves.length) ok = true; }
    if(ok){ first = i; break; }
  }
  showTab(first);
}

function showTab(k){
  for(var i=0;i<DATA.meals.length;i++){
    document.getElementById("panel_" + i).style.display = (i===k) ? "block" : "none";
    document.getElementById("tab_" + i).className = "tabbtn" + (i===k ? " on" : "");
  }
}

function current(info){
  var els = document.getElementsByName(info.name);
  for(var i=0;i<els.length;i++) if(els[i].checked) return els[i].value;
  return info.initial;
}

function calc(){
  var delta=0, list=[], byMeal = {};
  rows.forEach(function(r){
    var v = current(r), old = r.day.reserved ? r.day.amount : 0, nw;
    var labels = r.tr.getElementsByTagName("label");
    for(var i=0;i<labels.length;i++) labels[i].className = "opt" + (labels[i].getElementsByTagName("input")[0].checked && v!==r.initial ? " chosen" : "");
    if(v===r.initial) return;
    nw = (v==="none") ? 0 : r.prices[v];
    delta += nw - old;
    byMeal[r.meal.key] = (byMeal[r.meal.key] || 0) + 1;
    list.push(r.day.weekday + " " + r.day.date + " - " + r.meal.title + ": " + (v==="none" ? "لغو رزرو" : r.labels[v]));
  });
  return {delta: delta, list: list, byMeal: byMeal};
}

function refresh(){
  var c = calc(), credit = DATA.credit, after = credit - c.delta, short = c.delta - credit;
  var nums = document.getElementById("nums");
  nums.innerHTML = "";
  function item(label, val, cls){ var s=h("span"); s.appendChild(document.createTextNode(label+": ")); var b=h("b",cls||"",val); s.appendChild(b); nums.appendChild(s); }
  item("جمع تغییر", sgn(c.delta) + " ریال", "");
  item("موجودی فعلی", fmt(credit) + " ریال", "");
  item("پس از ثبت", (after<0?"-":"") + fmt(after) + " ریال", after<0 ? "bad":"good");
  if(short>0) item("کمبود", fmt(short) + " ریال", "bad");
  DATA.meals.forEach(function(m, i){  // شمارنده‌ی تغییرات هر سربرگ (تغییرِ تب‌های بسته هم دیده شود)
    var b = document.getElementById("tab_" + i), n = c.byMeal[m.key] || 0;
    b.textContent = m.title;
    if(n){ b.appendChild(h("span", "n", String(n))); }
  });
  var ul = document.getElementById("chg"); ul.innerHTML="";
  c.list.forEach(function(t){ ul.appendChild(h("li","",t)); });
  document.getElementById("go").disabled = busy || c.list.length===0;
}

function api(method, path, body, cb){
  var x = new XMLHttpRequest();
  x.open(method, path); x.setRequestHeader("X-Token", TOKEN); x.setRequestHeader("Content-Type", "application/json");
  x.onload = function(){ var j; try{ j=JSON.parse(x.responseText); }catch(e){ j={state:"error", message:"پاسخ نامعتبر از برنامه"}; } cb(j); };
  x.onerror = function(){ cb({state:"error", message:"ارتباط با برنامه قطع شد (برنامه بسته شده؟)"}); };
  x.send(body ? JSON.stringify(body) : null);
}

function show(kind, text){ var o=document.getElementById("out"); o.className=kind; o.textContent=text; o.style.display="block"; window.scrollTo(0,0); }

function resultsText(res){
  var t=[];
  res.forEach(function(r){
    t.push((r.ok?"✓ ":"✗ ") + r.meal + (r.message ? " - پیام سایت: "+r.message : ""));
    (r.problems||[]).forEach(function(p){ t.push("    ! "+p); });
    if(r.credit!=null) t.push("    موجودی پس از ذخیره: " + fmt(r.credit) + " ریال");
  });
  return t.join("\n");
}

function setBusy(b){ busy=b; document.getElementById("go").textContent = b ? "در حال کار روی سایت ..." : "ثبت نهایی در سایت"; refresh(); }

function startPoll(){
  if(poll) clearInterval(poll);
  poll = setInterval(function(){
    api("GET", "/api/status", null, function(j){
      var log = (j.log||[]).join("\n"), stopBtn = document.getElementById("stopwait");
      if(j.job==="paying"){
        stopBtn.style.display="inline-block";
        show("info", "پنجره‌ی پرداخت باز است؛ پرداخت را در صفحه‌ی بانک کامل کن. بعد از برگشت به سایت، خودکار ادامه می‌دهم.\n\n" + log);
      }
      else if(j.job==="submitting"){ stopBtn.style.display="none"; show("info", "در حال اعمال تغییرات روی سایت (مرحله به مرحله) ...\n\n" + log); }
      else if(j.job==="paid"){
        clearInterval(poll); stopBtn.style.display="none";
        if(j.paid && j.paid.after!=null){ DATA.credit = j.paid.after; refresh(); }
        show("ok", "پرداخت موفق بود." + (j.paid && j.paid.after!=null ? "\nاعتبار فعلی: " + fmt(j.paid.after) + " ریال" : "") + (j.paid && j.paid.trace ? "\nکد پیگیری: " + j.paid.trace : "") + "\n\n" + log);
        setBusy(false);
      }
      else if(j.job==="payfailed"){
        clearInterval(poll); stopBtn.style.display="none";
        show("err", "پرداخت ناموفق بود: " + ((j.pay && j.pay.message) || "") + "\n\n" + log);
        setBusy(false);
      }
      else if(j.job==="finished"){
        clearInterval(poll); stopBtn.style.display="none";
        var pre = (j.paid && j.paid.after!=null) ? ("پرداخت موفق بود (اعتبار فعلی: " + fmt(j.paid.after) + " ریال). سپس:\n") : "";
        show(j.ok?"ok":"err", pre + (j.ok ? "همه‌ی تغییرات ثبت و تأیید شد. این صفحه و برنامه تا چند ثانیه‌ی دیگر بسته می‌شوند.\n" : "بعضی موارد ثبت نشد:\n") + resultsText(j.results||[]) + "\n\nمراحل انجام‌شده:\n" + log);
        setBusy(false);
      } else if(j.job==="idle"){
        clearInterval(poll); stopBtn.style.display="none";
        if(log) show("info", log);
        setBusy(false);
      }
    });
  }, 1500);
}

document.getElementById("go").onclick = function(){
  var c = calc();
  if(!confirm("این تغییرات روی سایت اصلی اعمال می‌شود:\n\n" + c.list.join("\n") + "\n\nجمع تغییر اعتبار: " + sgn(c.delta) + " ریال\nادامه می‌دهی؟")) return;
  var sels = rows.map(function(r){ return {meal: r.meal.key, date: r.day.date, value: current(r)}; });
  setBusy(true);
  api("POST", "/api/submit", {selections: sels}, function(j){
    if(j.state==="started"){
      show("info", "شروع شد ...");
      startPoll();
    } else if(j.state==="insufficient"){
      DATA.credit = j.credit; refresh();
      show("info", "موجودی کافی نیست؛ کمبود: " + fmt(j.shortfall) + " ریال.\nانتخاب‌ها در فایل pending.json ذخیره شد." + (j.canPay ? "\nمی‌توانی از «پرداخت اینترنتی» پایین صفحه پرداخت کنی (بعد از پرداخت موفق، خودکار رزرو می‌شود)، یا بعد از شارژ از راه دیگر «بروزرسانی موجودی» و دوباره «ثبت نهایی» را بزن." : "\nبعد از شارژ، «بروزرسانی موجودی» و دوباره «ثبت نهایی» را بزن."));
      if(j.canPay){
        var amt = Math.max(10000, Math.ceil(j.shortfall/1000)*1000);
        document.getElementById("payamt").value = fmt(amt);
        document.getElementById("payrow").style.display = "block";
      }
      setBusy(false);
    } else if(j.state==="nochange"){ show("info", "تغییری انتخاب نشده است."); setBusy(false); }
    else { show("err", j.message || "خطا"); setBusy(false); }
  });
};
document.getElementById("topgo").onclick = function(){
  var amt = parseInt(String(document.getElementById("topamt").value).replace(/[^0-9]/g,""), 10) || 0;
  if(amt<=0){ alert("مبلغ را وارد کن."); return; }
  if(!confirm("مبلغ " + fmt(amt) + " ریال به درگاه پرداخت فرستاده می‌شود.\nپرداخت را خودت در پنجره‌ی بانک کامل می‌کنی.\nادامه می‌دهی؟")) return;
  api("POST", "/api/topup", {amount: amt}, function(j){
    if(j.state==="paying"){ busy=true; refresh(); show("info","در حال باز کردن پنجره‌ی پرداخت ..."); startPoll(); }
    else show("err", j.message || "خطا");
  });
};
document.getElementById("pay").onclick = function(){
  var amt = parseInt(String(document.getElementById("payamt").value).replace(/[^0-9]/g,""), 10) || 0;
  if(!confirm("مبلغ " + fmt(amt) + " ریال به درگاه پرداخت فرستاده می‌شود.\nپرداخت را خودت در پنجره‌ی بانک کامل می‌کنی؛ بعد از پرداخت موفق، همین غذاها خودکار رزرو می‌شوند.\nادامه می‌دهی؟")) return;
  api("POST", "/api/pay", {amount: amt}, function(j){
    if(j.state==="paying"){ document.getElementById("payrow").style.display="none"; busy=true; refresh(); show("info","در حال باز کردن پنجره‌ی پرداخت ..."); startPoll(); }
    else show("err", j.message || "خطا");
  });
};
document.getElementById("stopwait").onclick = function(){
  api("POST", "/api/cancel", {}, function(){ if(poll) clearInterval(poll); document.getElementById("stopwait").style.display="none"; show("info","پرداخت لغو شد و پنجره بسته شد. اگر قبلاً پرداخت کرده‌ای، «بروزرسانی موجودی» را بزن. انتخاب‌های ذخیره‌شده در pending.json مانده است."); setBusy(false); });
};

document.getElementById("refreshcredit").onclick = function(){
  var b = this; b.disabled = true;
  api("POST", "/api/credit", {}, function(j){
    b.disabled = false;
    if(j.state==="credit"){ DATA.credit = j.credit; refresh(); show("ok", "موجودی بروز شد: " + fmt(j.credit) + " ریال"); }
    else show("err", j.message || "خطا");
  });
};
if(DATA.canPay) document.getElementById("topup").style.display="block";
build(); refresh();
if(DATA.notice || preApplied>0) show("info", (DATA.notice ? DATA.notice + "\n" : "") + (preApplied>0 ? ("انتخاب‌های ذخیره‌شده‌ی قبلی (" + preApplied + " مورد) اعمال شد؛ بررسی کن و «ثبت نهایی» را بزن.") : ""));
</script></body></html>
"""