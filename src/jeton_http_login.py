"""jeton_http_login.py - ورود به jeton.yazd.ac.ir فقط با requests (بدون Selenium)."""
from __future__ import annotations

import io
import logging
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import cv2
import numpy as np
import requests
from skimage.metrics import structural_similarity as ssim

BASE_URL = "https://jeton.yazd.ac.ir/"
HOME_PATH = "/master/default.aspx"
CROP = [(9, 77, 34, 94), (9, 98, 34, 115), (9, 119, 34, 136), (9, 139, 34, 156)]  # y1, x1, y2, x2
THRESHOLD = 0.4
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
log = logging.getLogger("jeton_login")


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


def _load_dataset(path: str) -> dict:
    if path not in _cache:
        data = {}
        for digit in sorted(os.listdir(path)):
            folder = os.path.join(path, digit)
            if not os.path.isdir(folder):
                continue
            refs = [cv2.imdecode(np.fromfile(os.path.join(folder, f), np.uint8), cv2.IMREAD_GRAYSCALE)
                    for f in os.listdir(folder) if f.lower().endswith((".png", ".jpg", ".jpeg"))]
            data[digit] = [r for r in refs if r is not None]
        _cache[path] = data
    return _cache[path]


def read_captcha(img_bytes: bytes, dataset_path: str) -> str:
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
        result += best if best_score >= THRESHOLD else "?"
    return result


# ------------------------------------------------------------------ ورود
def _js_hint(html: str) -> str:
    tag = re.search(r"<input[^>]*btnEncript[^>]*>", html, re.I)
    lines = [l.strip()[:200] for l in html.splitlines()
             if re.search(r"encript|encrypt|Plain|CryptoJS|md5|sha\d*\(|rsa", l, re.I)]
    return "\n".join(([tag.group(0)] if tag else []) + lines[:12])


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


def _attempt(s: requests.Session, username: str, password: str, dataset_path: str):
    """یک تلاش کامل با صفحه و کپچای تازه. خروجی: (کد کپچا، پاسخ POST یا None، HTML صفحه‌ی ورود، موفق؟)"""
    page = s.get(BASE_URL, timeout=20)
    page.raise_for_status()
    if _is_home(page):  # نشست از قبل معتبر است
        return "", page, page.text, True
    form = _Form()
    form.feed(page.text)
    by_id = {i["id"]: i for i in form.inputs if i.get("id")}
    for need in ("txtUsernamePlain", "txtPasswordPlain", "txtCaptcha"):
        if need not in by_id:
            raise LoginError(f"فیلد {need} در صفحه‌ی ورود پیدا نشد", url=page.url)
    if not form.captcha_src:
        raise LoginError("تصویر کپچا (Img1) در صفحه‌ی ورود پیدا نشد", url=page.url)

    img = s.get(urljoin(page.url, form.captcha_src), headers={"Referer": page.url}, timeout=20)
    img.raise_for_status()
    code = read_captcha(img.content, dataset_path)
    log.info("کپچا: %s", code)
    if "?" in code:  # خوانده نشد؛ بدون ارسال، کپچای تازه می‌گیریم تا تلاش بی‌خود ثبت نشود
        return code, None, page.text, False

    name = lambda i: by_id.get(i, {}).get("name") or i
    payload = {}
    for i in form.inputs:  # __VIEWSTATE، __VIEWSTATEGENERATOR، __EVENTVALIDATION، textareaها و ... خودکار می‌آیند
        n, t = i.get("name"), (i.get("type") or "text").lower()
        if n and (t not in ("submit", "button", "image", "file", "checkbox", "radio") or "checked" in i):
            payload[n] = i.get("value") or ""
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
    return code, resp, page.text, ok


def http_login(username: str, password: str, dataset_path: str, attempts: int = 5) -> requests.Session:
    if not os.path.isdir(dataset_path):
        raise FileNotFoundError(f"پوشه‌ی دیتاست کپچا پیدا نشد: {dataset_path}")
    s = requests.Session()
    s.headers["User-Agent"] = UA
    code, resp, html = "", None, ""
    for n in range(1, attempts + 1):
        log.info("تلاش ورود %d/%d", n, attempts)
        code, resp, html, ok = _attempt(s, username, password, dataset_path)
        if ok:
            s.login_info = {"captcha": code, "url": resp.url, "attempt": n}
            return s
        time.sleep(0.3)
    log.warning("نکات JavaScript صفحه‌ی ورود (برای بررسی رمزنگاری سمت کاربر):\n%s", _js_hint(html))
    raise LoginError(f"ورود بعد از {attempts} تلاش ناموفق بود", captcha=code, url=resp.url if resp else "")