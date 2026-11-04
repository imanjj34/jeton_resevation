"""jeton_http.py - ورود (با کپچای SSIM) و شروع پرداخت اینترنتی در jeton.yazd.ac.ir، فقط با requests.

پنجره‌ی بانک را pay_window.py (pywebview) با کوکی‌های نشست باز می‌کند."""
from __future__ import annotations

import io
import logging
import re
import time
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import cv2
import numpy as np
import requests

BASE_URL = "https://jeton.yazd.ac.ir/"
HOME_PATH = "/master/default.aspx"
CROP = [(9, 77, 34, 94), (9, 98, 34, 115), (9, 119, 34, 136), (9, 139, 34, 156)]  # y1, x1, y2, x2
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
log = logging.getLogger("jeton_login")


def ssim(x: np.ndarray, y: np.ndarray, win: int = 7, data_range: float = 255.0) -> float:
    """SSIM با همان فرمول و پنجره‌ی ۷×۷ یکنواخت و کوواریانس نمونه‌ای scikit-image، فقط با numpy."""
    if x.shape != y.shape or min(x.shape) < win:
        raise ValueError("ابعاد تصاویر یکسان نیست یا از پنجره کوچک‌تر است")
    x = x.astype(np.float64)
    y = y.astype(np.float64)
    c1, c2 = (0.01 * data_range) ** 2, (0.03 * data_range) ** 2
    np_ = win * win
    cov_norm = np_ / (np_ - 1)

    def mean(a: np.ndarray) -> np.ndarray:
        return np.lib.stride_tricks.sliding_window_view(a, (win, win)).mean(axis=(2, 3))

    ux, uy = mean(x), mean(y)
    vx = cov_norm * (mean(x * x) - ux * ux)
    vy = cov_norm * (mean(y * y) - uy * uy)
    vxy = cov_norm * (mean(x * y) - ux * uy)
    s = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    return float(s.mean())


class LoginError(RuntimeError):
    def __init__(self, msg: str, captcha: str = "", url: str = ""):
        super().__init__(msg)
        self.captcha, self.url = captcha, url


class _Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action, self.inputs, self.captcha_src, self._ta = None, [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action") or ""
        elif tag == "input":
            self.inputs.append(a)
        elif tag == "textarea":
            self._ta = dict(a, type="textarea", value="")
            self.inputs.append(self._ta)
        elif tag == "img" and a.get("id") == "Img1":
            self.captcha_src = a.get("src")

    def handle_data(self, data):
        if self._ta is not None:
            self._ta["value"] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._ta = None


# ------------------------------------------------------------------ کپچا (همان الگوریتم SSIM)
def _gray(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:  # فرمت‌هایی مثل GIF
        from PIL import Image
        img = np.array(Image.open(io.BytesIO(data)).convert("L"))
    return img


_cache: dict = {}


def _load_dataset(path: str = "") -> dict:
    if "embedded" not in _cache:
        from captcha_data import CAPTCHA_DATA, IMAGE_SHAPE
        _cache["embedded"] = {
            digit: [np.asarray(values, dtype=np.uint8).reshape(IMAGE_SHAPE) for values in refs]
            for digit, refs in CAPTCHA_DATA.items()
        }
    return _cache["embedded"]


def read_captcha(img_bytes: bytes, dataset_path: str, threshold: float = 0.4) -> str:
    dataset = _load_dataset(dataset_path)
    _, binary = cv2.threshold(_gray(img_bytes), 160, 255, cv2.THRESH_BINARY)
    result = ""
    for y1, x1, y2, x2 in CROP:
        piece = binary[y1:y2, x1:x2]
        best, best_score = "?", 0.0
        for digit, refs in dataset.items():
            scores = []
            for ref in refs:
                try:
                    scores.append(ssim(piece, ref))
                except Exception:
                    continue
            if scores and np.mean(scores) > best_score:
                best, best_score = digit, float(np.mean(scores))
        result += best if best_score >= threshold else "?"
    return result


# ------------------------------------------------------------------ ورود
def _rsa_encryptor(pem: str):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pem = pem.strip()
    if "BEGIN" in pem:
        key = serialization.load_pem_public_key(pem.encode())
    else:  # JSEncrypt کلید بدون هدر را هم می‌پذیرد
        body = "\n".join(pem[i:i + 64] for i in range(0, len(pem), 64))
        try:
            key = serialization.load_pem_public_key(f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----".encode())
        except ValueError:
            key = serialization.load_pem_public_key(f"-----BEGIN RSA PUBLIC KEY-----\n{body}\n-----END RSA PUBLIC KEY-----".encode())
    return lambda text: base64.b64encode(key.encrypt(text.encode(), padding.PKCS1v15())).decode()


def _is_home(resp: requests.Response) -> bool:
    return urlparse(resp.url).path.lower() == HOME_PATH


def _attempt(s: requests.Session, username: str, password: str, dataset_path: str, threshold: float):
    """یک تلاش کامل با صفحه و کپچای تازه. خروجی: (کد کپچا، پاسخ POST یا None، موفق؟)"""
    page = s.get(BASE_URL, timeout=20)
    page.raise_for_status()
    if _is_home(page):  # نشست از قبل معتبر است
        return "", page, True
    form = _Form()
    form.feed(page.text)
    by_id = _ids(form)
    for need in ("txtUsernamePlain", "txtPasswordPlain", "txtCaptcha"):
        if need not in by_id:
            raise LoginError(f"فیلد {need} در صفحه‌ی ورود پیدا نشد", url=page.url)
    if not form.captcha_src:
        raise LoginError("تصویر کپچا (Img1) در صفحه‌ی ورود پیدا نشد", url=page.url)

    img = s.get(urljoin(page.url, form.captcha_src), headers={"Referer": page.url}, timeout=20)
    img.raise_for_status()
    code = read_captcha(img.content, dataset_path, threshold)
    log.info("کپچا: %s", code)
    if "?" in code:  # خوانده نشد؛ بدون ارسال، کپچای تازه می‌گیریم تا تلاش بی‌خود ثبت نشود
        return code, None, False

    name = lambda i: by_id.get(i, {}).get("name") or i
    payload = _payload(form, {})  # __VIEWSTATE، __VIEWSTATEGENERATOR، __EVENTVALIDATION، textareaها و ... خودکار می‌آیند
    # دقیقاً مثل EncriptData("login") در صفحه
    if by_id.get("loginSecurtxt", {}).get("value", "").strip().lower() == "true":
        encrypt = _rsa_encryptor(by_id["publicKeytxt"]["value"])
        user_val, pass_val = encrypt(username), encrypt(password)
        payload[name("txtUsernamePlain")] = payload[name("txtPasswordPlain")] = ""
    else:
        user_val, pass_val = username, password
        payload[name("txtUsernamePlain")], payload[name("txtPasswordPlain")] = username, password
    payload[name("txtUsername")], payload[name("txtPassword")] = user_val, pass_val
    payload[name("txtCaptcha")] = code
    btn = by_id.get("btnLogin", {})  # JS صفحه در نهایت روی همین دکمه‌ی submit کلیک می‌کند (نه btnEncript)
    payload[btn.get("name") or "btnLogin"] = btn.get("value") or ""

    resp = s.post(urljoin(page.url, form.action or ""), data=payload, headers={"Referer": page.url}, timeout=20)
    ok = _is_home(resp) or ("txtUsernamePlain" not in resp.text and "txtCaptcha" not in resp.text)
    return code, resp, ok


def http_login(username: str, password: str, dataset_path: str, attempts: int = 5, threshold: float = 0.4) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    code, resp = "", None
    for n in range(1, attempts + 1):
        log.info("تلاش ورود %d/%d", n, attempts)
        code, resp, ok = _attempt(s, username, password, dataset_path, threshold)
        if ok:
            return s
        time.sleep(0.3)
    raise LoginError(f"ورود بعد از {attempts} تلاش ناموفق بود", captcha=code, url=resp.url if resp else "")


# ------------------------------------------------------------------ پرداخت اینترنتی
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
