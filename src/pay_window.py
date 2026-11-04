"""
pay_window.py - پنجره‌ی پرداخت با pywebview؛ در یک پردازش جدا اجرا می‌شود (GUI باید روی thread اصلی باشد).

    python pay_window.py spec.json

spec.json: {"title", "inject_url", "callback_mark", "close_on_success", "close_delay", "cookies": [{"name","value","path"}],
            "bank": {"kind": "url", "url"} | {"kind": "html", "html", "base"}}

مراحل:
 ۱) صفحه‌ای از خود سایت (inject_url) باز می‌شود تا بتوان کوکی‌های نشست برنامه را با JavaScript گذاشت.
 ۲) اگر کوکی‌ها ثبت نشدند، بانک باز نمی‌شود (وگرنه بعد از پرداخت، صفحه‌ی بازگشت سایت بدون نشست می‌شکند).
 ۳) صفحه‌ی بانک باز می‌شود؛ کاربر کارت و رمز را فقط همین‌جا وارد می‌کند.
 ۴) وقتی مرورگر به ePay_CallBack برگشت، نتیجه خوانده و چاپ می‌شود. اگر نتیجه «موفق» بود و close_on_success روشن باشد،
    پنجره بعد از چند ثانیه خودش بسته می‌شود؛ در شکست باز می‌ماند تا کاربر خطا را ببیند.

خروجی: هر خط یک JSON با کلید event (cookies | result | error | closed).
"""
from __future__ import annotations

import json
import os
import sys
import re
import threading
import time
from typing import Optional

CALLBACK_MARK = "ePay_CallBack"


def _int(text: str) -> Optional[int]:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_callback(html: str) -> dict:
    """نتیجه‌ی تراکنش از صفحه‌ی بازگشت (ePay_CallBack.aspx). ok فقط وقتی True است که نتیجه دقیقاً «موفق» باشد («ناموفق» نه)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    def text(i: str) -> str:
        el = soup.find(id=i)
        return " ".join(el.get_text(" ", strip=True).replace("\u200c", "").split()) if el else ""

    result = text("cphMain_lblResult")
    return {
        "ok": result == "موفق",
        "result": result,
        "message": text("cphMain_lblMessage"),
        "order_id": text("cphMain_lblOrderId"),
        "trace": text("cphMain_lblTraceNumber"),
        "previous_credit": _int(text("cphMain_lblPreviousCredit")),
        "current_credit": _int(text("cphMain_lblCurrentCredit")),
    }


def emit(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def run(spec: dict) -> int:
    try:
        import webview
    except ImportError:
        emit(event="error", message="pywebview نصب نیست: pip install pywebview")
        return 2

    cookies, bank, mark = spec.get("cookies", []), spec["bank"], spec.get("callback_mark", CALLBACK_MARK)
    state = {"step": "cookies", "reported": False}
    holder: dict = {}

    def inject() -> list[str]:
        win = holder["w"]
        for c in cookies:
            line = f'{c["name"]}={c["value"]}; path={c.get("path") or "/"}'
            win.evaluate_js("document.cookie = " + json.dumps(line))
        have = win.evaluate_js("document.cookie") or ""
        return [c["name"] for c in cookies if c["name"] + "=" not in have]

    def read_callback() -> None:
        win = holder["w"]
        html = win.evaluate_js("document.documentElement.outerHTML") or ""
        if "Server Error" in html or "NullReferenceException" in html:
            emit(event="result", ok=False, result="", message="صفحه‌ی بازگشت سایت خطا داد (Server Error)")
            return
        r = parse_callback(html)
        if not r["result"]:
            emit(event="result", ok=False, result="", message="صفحه‌ی بازگشت نتیجه‌ای نشان نداد (ورود دوباره لازم شده؟)")
        else:
            emit(event="result", **r)
            if r["ok"] and spec.get("close_on_success"):
                def close_later() -> None:
                    time.sleep(float(spec.get("close_delay", 2)))   # کاربر لحظه‌ای صفحه‌ی موفق را ببیند
                    emit(event="closing", reason="success")
                    holder["w"].destroy()
                threading.Thread(target=close_later, daemon=True).start()

    def on_loaded() -> None:
        try:
            if state["step"] == "cookies":
                state["step"] = "bank"
                missing = inject()
                if missing:
                    emit(event="error", message="کوکی‌های نشست در پنجره ثبت نشد: " + ", ".join(missing) + "؛ بانک باز نشد")
                    holder["w"].destroy()
                    return
                emit(event="cookies", ok=True, names=[c["name"] for c in cookies])
                if bank["kind"] == "url":
                    holder["w"].load_url(bank["url"])
                else:
                    holder["w"].load_html(bank["html"], bank.get("base") or spec["inject_url"])
                return
            url = holder["w"].get_current_url() or ""
            if mark in url and not state["reported"]:
                state["reported"] = True
                read_callback()
        except Exception as e:  # noqa: BLE001
            emit(event="error", message=f"{type(e).__name__}: {e}")

    win = webview.create_window(spec.get("title") or "پرداخت اینترنتی", url=spec["inject_url"], width=980, height=760)
    holder["w"] = win
    win.events.loaded += on_loaded
    win.events.closed += lambda: emit(event="closed")
    webview.start(private_mode=True, gui=os.environ.get("JETON_PAYWIN_GUI") or None)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    sys.stdout.reconfigure(encoding="utf-8")
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        raise SystemExit(run(json.load(f)))
