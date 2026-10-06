"""
payment.py - خواندن نتیجه‌ی بازگشت از بانک (صفحه‌ی ePay_CallBack.aspx).

بعد از پرداخت در صفحه‌ی بانک، مرورگر به ePay_CallBack.aspx برمی‌گردد و این صفحه نتیجه‌ی تراکنش،
پیام سامانه، شماره فاکتور، کد پیگیری و اعتبار قبلی/فعلی را نشان می‌دهد.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

CALLBACK_MARK = "ePay_CallBack"


def _int(text: str) -> Optional[int]:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_callback(html: str) -> dict:
    """نتیجه‌ی تراکنش از صفحه‌ی بازگشت. ok فقط وقتی True است که نتیجه دقیقاً «موفق» باشد («ناموفق» نه)."""
    soup = BeautifulSoup(html, "lxml")

    def text(i: str) -> str:
        el = soup.find(id=i)
        return " ".join(el.get_text(" ", strip=True).replace("\u200c", "").split()) if el else ""

    result = text("cphMain_lblResult")
    return {
        "ok": result == "موفق",  # در صفحه‌ی بازگشت فیلد نتیجه دقیقاً «موفق» (یا «ناموفق») است
        "result": result,
        "message": text("cphMain_lblMessage"),
        "order_id": text("cphMain_lblOrderId"),
        "trace": text("cphMain_lblTraceNumber"),
        "previous_credit": _int(text("cphMain_lblPreviousCredit")),
        "current_credit": _int(text("cphMain_lblCurrentCredit")),
    }
