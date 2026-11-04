"""http_payment.py - پرداخت اینترنتی با requests؛ پنجره‌ی بانک را pay_window.py (pywebview) با کوکی‌های نشست باز می‌کند."""
from __future__ import annotations

import re
from html import escape
from urllib.parse import urljoin

import requests

from jeton_http_login import _Form

AMOUNT_ID = "cphMain_txtPaymentAmount"
BANK_ID = "cphMain_dataLstBank_imgBankLogo_0"
PAY_BTN_ID = "cphMain_btnPay"
AMOUNT_LABEL_ID = "cphMain_lblPaymentAmount_"
ALERT_ID = "cphMain_alertBoxValue"
SKIP_TYPES = ("submit", "button", "image", "file", "checkbox", "radio")


class PaymentError(RuntimeError):
    pass


def _ids(form: _Form) -> dict:
    return {i["id"]: i for i in form.inputs if i.get("id")}


def _payload(form: _Form, extra: dict) -> dict:
    data = {}
    for i in form.inputs:
        n, t = i.get("name"), (i.get("type") or "text").lower()
        if n and (t not in SKIP_TYPES or "checked" in i):
            data[n] = i.get("value") or ""
    data.update(extra)
    return data


def _alert(html: str) -> str:
    m = re.search(r'id="%s"[^>]*>(.*?)<' % ALERT_ID, html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def start_bank_payment(s: requests.Session, amount: int, epay_url: str) -> requests.Response:
    """مبلغ را ثبت، دکمه‌ی درگاه و سپس دکمه‌ی «پرداخت» را می‌زند. پاسخ نهایی (هدایت به بانک) برگردانده می‌شود."""
    page = s.get(epay_url, timeout=20)
    page.raise_for_status()
    form = _Form()
    form.feed(page.text)
    by_id = _ids(form)
    if AMOUNT_ID not in by_id:
        if "txtUsernamePlain" in page.text:
            raise PaymentError("نشست ورود منقضی شده؛ دوباره وارد برنامه شو")
        raise PaymentError("صفحه‌ی پرداخت باز نشد")

    bank = by_id[BANK_ID]
    data = _payload(form, {by_id[AMOUNT_ID]["name"]: f"{amount:,}",
                           bank["name"] + ".x": "1", bank["name"] + ".y": "1"})
    conf = s.post(urljoin(page.url, form.action or ""), data=data, headers={"Referer": page.url}, timeout=20)
    conf.raise_for_status()

    cform = _Form()
    cform.feed(conf.text)
    cby = _ids(cform)
    if PAY_BTN_ID not in cby:
        raise PaymentError(_alert(conf.text) or "مرحله‌ی تأیید مبلغ نمایش داده نشد")
    m = re.search(r'id="%s"[^>]*>([^<]*)<' % AMOUNT_LABEL_ID, conf.text)
    shown = re.sub(r"\D", "", m.group(1)) if m else ""
    if shown != str(amount):
        raise PaymentError(f"مبلغ صفحه‌ی تأیید ({shown or '؟'}) با مبلغ خواسته‌شده ({amount}) یکی نیست")

    btn = cby[PAY_BTN_ID]
    data = _payload(cform, {btn["name"]: btn.get("value") or ""})
    return s.post(urljoin(conf.url, cform.action or ""), data=data, headers={"Referer": conf.url},
                  timeout=20, allow_redirects=False)


def bank_target(resp: requests.Response) -> dict:
    """مقصد بانک برای pay_window.py: آدرس هدایت (kind=url) یا صفحه‌ی HTML فرم بانک (kind=html)."""
    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
        return {"kind": "url", "url": urljoin(resp.url, resp.headers["Location"])}
    if resp.status_code >= 400:
        raise PaymentError(f"درگاه پاسخ {resp.status_code} داد")
    base = f'<base href="{escape(resp.url, quote=True)}">'
    html = re.sub(r"(?i)(<head[^>]*>)", lambda m: m.group(1) + base, resp.text, count=1)
    if html == resp.text:
        html = base + resp.text
    return {"kind": "html", "html": html, "base": resp.url}
