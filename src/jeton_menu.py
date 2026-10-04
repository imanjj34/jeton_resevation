"""
jeton_menu.py - لاگین با Selenium (+ کپچا)، سپس همه‌ی کارها با requests.

    python jeton_menu.py              # پرسش‌ها در ترمینال (پیش‌فرض: DEFAULT_UI)
    python jeton_menu.py --ui web     # از همان اول Chrome باز می‌شود و همه‌چیز مرحله‌به‌مرحله در صفحه‌ی وب انجام می‌شود
    python jeton_menu.py --headed     # مرورگر لاگین/کپچا دیده شود (برای اشکال‌یابی)

پیش‌فرض‌ها (ویرایش در بلوک «تنظیمات پیش‌فرض» پایین): مشاهده و رزرو، هفته‌ی بعد، مرورگر لاگین مخفی (headless).
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import getpass
import os
import time
import webbrowser

import cv2
import numpy as np
import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from skimage.metrics import structural_similarity as ssim

from food_menu import fetch_menu
from payment import CALLBACK_MARK, parse_callback
from reserve import PENDING_FILE, clear_pending, current_state, load_pending, price_delta, submit, summary_lines
from web_app import AppServer
from web_select import SelectionServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://jeton.yazd.ac.ir/"
HOME_URL = BASE_URL + "Master/Default.aspx"
EPAY_URL = BASE_URL + "ePay/ePay_Payment.aspx"

# ----------------------------------------------------------------- تنظیمات پیش‌فرض
DEFAULT_UI = os.environ.get("JETON_UI", "terminal")  # "terminal" یا "web" (با --ui هم عوض می‌شود)
DEFAULT_MODE = "reserve"   # reserve = مشاهده و رزرو | file = فقط منو و فایل | pending = انتخاب‌های ذخیره‌شده
DEFAULT_WEEK = "next"      # current = هفته جاری | next = هفته بعد
HEADLESS_LOGIN = True      # مرورگر لاگین/کپچا مخفی باشد. پنجره‌ی پرداخت همیشه دیده می‌شود (کارت را خودت وارد می‌کنی)

DATASET_PATH = os.environ.get("JETON_CAPTCHA_DATASET", r"C:\Users\iman\Documents\project\image vision\dataset")
CAPTCHA_THRESHOLD = 0.4
LOGIN_RETRIES = 5
CHROMEDRIVER_PATH = os.environ.get("JETON_CHROMEDRIVER")  # اختیاری: مسیر chromedriver.exe دستی


# ------------------------------------------------- تشخیص کپچا (بدون تغییر)
def recognize(driver) -> str:
    crop_coords = [(9, 77, 34, 94), (9, 98, 34, 115), (9, 119, 34, 136), (9, 139, 34, 156)]
    try:
        img = driver.find_element("id", "Img1")
        img_base64 = driver.execute_script(
            """
            var canvas = document.createElement('canvas');
            canvas.width = arguments[0].naturalWidth;
            canvas.height = arguments[0].naturalHeight;
            canvas.getContext('2d').drawImage(arguments[0], 0, 0);
            return canvas.toDataURL('image/png').split(',')[1];
            """,
            img,
        )
        image = cv2.imdecode(np.frombuffer(base64.b64decode(img_base64), np.uint8), cv2.IMREAD_GRAYSCALE)
        _, binary = cv2.threshold(image, 160, 255, cv2.THRESH_BINARY)
        digit_images = [binary[y1:y2, x1:x2] for y1, x1, y2, x2 in crop_coords]

        result = ""
        for input_img in digit_images:
            best_digit, best_score = "?", 0
            for digit in os.listdir(DATASET_PATH):
                digit_path = os.path.join(DATASET_PATH, digit)
                if not os.path.isdir(digit_path):
                    continue
                scores = []
                for file in os.listdir(digit_path):
                    if file.lower().endswith((".png", ".jpg", ".jpeg")):
                        ref = cv2.imread(os.path.join(digit_path, file), cv2.IMREAD_GRAYSCALE)
                        if ref is not None:
                            try:
                                scores.append(ssim(input_img, ref))
                            except Exception:
                                continue
                if scores and np.mean(scores) > best_score:
                    best_score, best_digit = np.mean(scores), digit
            result += best_digit if best_score >= CAPTCHA_THRESHOLD else "?"
        return result
    except Exception as e:
        logger.error(f"خطا در تشخیص کپچا: {e}")
        return "????"


# ------------------------------------------------------- لاگین (سلنیوم)
def _try_login(driver, username: str, password: str) -> bool:
    driver.get(BASE_URL)
    driver.execute_script("arguments[0].value = arguments[1];", driver.find_element(By.ID, "txtUsernamePlain"), username)
    driver.execute_script("arguments[0].value = arguments[1];", driver.find_element(By.ID, "txtPasswordPlain"), password)
    captcha = recognize(driver).strip()
    logger.info(f"کپچا: {captcha}")
    captcha_input = driver.find_element(By.ID, "txtCaptcha")
    captcha_input.clear()
    captcha_input.send_keys(captcha)
    driver.find_element(By.ID, "btnEncript").click()
    try:
        WebDriverWait(driver, 5).until(lambda d: d.current_url == HOME_URL)
        return True
    except TimeoutException:
        return False


def _new_driver(headless: bool = False) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
    return webdriver.Chrome(service=Service(CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else None, options=options)


def _driver_login(driver, username: str, password: str) -> None:
    for attempt in range(1, LOGIN_RETRIES + 1):
        logger.info(f"تلاش لاگین {attempt}/{LOGIN_RETRIES}")
        if _try_login(driver, username, password):
            return
        time.sleep(0.5)
    raise RuntimeError("لاگین ناموفق (نام کاربری/رمز یا تشخیص کپچا را بررسی کن)")


def _cookies_to_session(driver, session: requests.Session) -> None:
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))


def get_session(username: str, password: str, headless: bool | None = None) -> requests.Session:
    """با سلنیوم لاگین می‌کند (پیش‌فرض: مخفی)، کوکی‌ها را به requests.Session می‌دهد و مرورگر را می‌بندد."""
    driver = _new_driver(HEADLESS_LOGIN if headless is None else headless)
    try:
        _driver_login(driver, username, password)
        session = requests.Session()
        session.headers["User-Agent"] = driver.execute_script("return navigator.userAgent;")
        _cookies_to_session(driver, session)
        return session
    finally:
        driver.quit()


# ------------------------------------------------------------ پرداخت اینترنتی
class PaymentBrowser:
    """پنجره‌ی مرورگری که کاربر در آن پرداخت را در صفحه‌ی بانک انجام می‌دهد."""

    def __init__(self, driver):
        self.driver = driver

    def wait_for_result(self, stop, timeout_sec: float = 1800, poll_sec: float = 1.0):
        """
        منتظر می‌ماند تا مرورگر از بانک به ePay_CallBack.aspx برگردد و نتیجه را از همان صفحه می‌خواند.
        فقط آدرس پنجره را می‌بیند (محلی است)؛ هیچ درخواستی به سایت/بانک نمی‌زند.
        خروجی: دیکشنری نتیجه | {"closed": True} اگر پنجره بسته شد | None اگر stop خورد یا مهلت تمام شد.
        """
        waited = 0.0
        while not stop.is_set() and waited < timeout_sec:
            try:
                url = self.driver.current_url
            except Exception:  # noqa: BLE001  (پنجره را کاربر بسته است)
                return {"closed": True}
            if CALLBACK_MARK in url:
                try:
                    WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(By.ID, "cphMain_lblResult"))
                except Exception:  # noqa: BLE001
                    pass
                return parse_callback(self.driver.page_source)
            stop.wait(poll_sec)
            waited += poll_sec
        return None

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001
            pass


def _page_alert(driver) -> str:
    return (driver.execute_script(
        "var e=document.getElementById('cphMain_alertBoxValue');return e?e.textContent:'';") or "").strip()


def open_payment(session: requests.Session, amount: int, username: str, password: str) -> PaymentBrowser:
    """
    یک پنجره‌ی Chrome باز می‌کند (با همان کوکی‌های لاگین) و مثل کاربر جلو می‌رود:
      ۱) صفحه‌ی «افزایش اعتبار اینترنتی»: مبلغ را می‌نویسد و روی درگاه کلیک می‌کند.
      ۲) صفحه‌ی «تأیید پرداخت»: اگر مبلغِ نشان‌داده‌شده با مبلغ خواسته‌شده یکی بود، «پرداخت» را می‌زند.
      ۳) از اینجا به بعد (کارت و رمز در صفحه‌ی بانک) فقط کار خود کاربر است؛ اطلاعات کارت از این برنامه عبور نمی‌کند.
    پنجره باز می‌ماند تا PaymentBrowser.close().
    """
    driver = _new_driver()
    try:
        driver.get(BASE_URL)
        for c in session.cookies:
            driver.add_cookie({"name": c.name, "value": c.value, "path": c.path or "/", "secure": bool(c.secure)})
        driver.get(EPAY_URL)
        if not driver.find_elements(By.ID, "cphMain_txtPaymentAmount"):
            # کوکی‌ها پذیرفته نشد؛ در همین پنجره لاگین می‌کنیم و کوکی‌های جدید را به session هم می‌دهیم
            _driver_login(driver, username, password)
            _cookies_to_session(driver, session)
            driver.get(EPAY_URL)
        WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.ID, "cphMain_txtPaymentAmount"))
        box = driver.find_element(By.ID, "cphMain_txtPaymentAmount")
        driver.execute_script("arguments[0].value = arguments[1];", box, f"{amount:,}")
        driver.find_element(By.ID, "cphMain_dataLstBank_imgBankLogo_0").click()

        # صفحه‌ی تأیید (با ajax عوض می‌شود): یا دکمه‌ی «پرداخت» می‌آید یا پیام خطا
        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.ID, "cphMain_btnPay") or _page_alert(d))
        if not driver.find_elements(By.ID, "cphMain_btnPay"):
            raise RuntimeError(f"سایت مبلغ را نپذیرفت: {_page_alert(driver)}")
        shown = "".join(ch for ch in driver.find_element(By.ID, "cphMain_lblPaymentAmount_").text if ch.isdigit())
        if shown != str(amount):
            driver.find_element(By.ID, "cphMain_btnCancel").click()
            raise RuntimeError(f"مبلغ صفحه‌ی تأیید ({shown or '؟'}) با مبلغ خواسته‌شده ({amount}) یکی نیست؛ انصراف زده شد")
        driver.find_element(By.ID, "cphMain_btnPay").click()
        return PaymentBrowser(driver)
    except Exception:
        driver.quit()
        raise


# ------------------------------------------------------------ پرسش‌ها
def ask(prompt: str, default: str = "") -> str:
    ans = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return ans or default


def choose(prompt: str, options: list[str], default: int = 1) -> int:
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        ans = input(f"انتخاب [{default}]: ").strip() or str(default)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans)
        print("عدد معتبر وارد کن.")


def login_prompt() -> tuple[str, str]:
    global DATASET_PATH
    username = os.environ.get("JETON_USER") or ask("نام کاربری")
    password = os.environ.get("JETON_PASS") or getpass.getpass("رمز عبور (هنگام تایپ نمایش داده نمی‌شود): ")
    if not os.path.isdir(DATASET_PATH):
        print(f"پوشه‌ی دیتاست کپچا پیدا نشد: {DATASET_PATH}")
        DATASET_PATH = ask("مسیر پوشه‌ی دیتاست کپچا")
    return username, password


def ask_week() -> str:
    weeks = ["current", "next"]
    return weeks[choose("\nکدام هفته؟", ["هفته جاری", "هفته بعد (برای رزرو)"], weeks.index(DEFAULT_WEEK) + 1) - 1]


def show_progress(n: int, total: int) -> None:
    print(f"\r  {n}/{total}", end="", flush=True)


def report(results: list[dict]) -> None:
    print()
    for r in results:
        print(f"{'✓' if r['ok'] else '✗'} {r['meal']}" + (f" - پیام سایت: {r['message']}" if r.get("message") else ""))
        for pr in r["problems"]:
            print(f"    ! {pr}")
        if r.get("credit") is not None:
            print(f"    موجودی پس از ذخیره: {r['credit']:,} ریال")
    if all(r["ok"] for r in results):
        clear_pending()
        print("\nهمه‌ی تغییرات ثبت و تأیید شد.")
    else:
        print("\nبعضی موارد ثبت نشد؛ پیام‌های بالا را ببین و صفحه‌ی سایت را هم چک کن.")


# ------------------------------------------------------------------ حالت‌ها
def mode_menu_file(session) -> None:
    week = ask_week()
    ext = [".html", ".json", ".txt"][choose("\nفرمت خروجی؟", ["HTML (صفحه‌ی وب، خواناترین)", "JSON", "متن ساده"]) - 1]
    out = ask("نام فایل خروجی", f"menu{ext}")
    if not os.path.splitext(out)[1]:
        out += ext
    open_after = ext == ".html" and ask("بعد از ساخت، در مرورگر باز شود؟ (y/n)", "y").lower().startswith("y")

    print("\nدر حال دریافت برنامه غذایی همه‌ی سلف‌ها ...")
    menu = fetch_menu(session, week, all_selves=True, on_progress=show_progress)
    print()
    if not menu.has_food:
        print(f"\nبرای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است." + (f"\n{menu.message}" if menu.message else ""))
        return

    ext = os.path.splitext(out)[1].lower()
    content = (json.dumps(menu.to_dict(), ensure_ascii=False, indent=2) if ext == ".json"
               else menu.to_text() if ext == ".txt" else menu.to_html())
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\nذخیره شد: {os.path.abspath(out)}")
    if open_after:
        webbrowser.open("file://" + os.path.abspath(out))


def mode_reserve(session, relogin, username: str, password: str) -> None:
    week = ask_week()
    print("\nدر حال دریافت برنامه غذایی همه‌ی سلف‌ها ...")
    menu = fetch_menu(session, week, all_selves=True, on_progress=show_progress)
    print()
    if not menu.has_food:
        print(f"\nبرای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است.")
        return
    # انتخاب در صفحه‌ی وب انجام می‌شود و بعد از «ثبت نهایی» روی سایت اعمال می‌شود
    SelectionServer(session, menu, relogin,
                    pay_opener=lambda sess, amount: open_payment(sess, amount, username, password)).run()


def mode_pending(session, relogin) -> None:
    """انتخاب‌های ذخیره‌شده را (فقط یک بار موجودی را چک می‌کند) در صورت کافی بودن اعتبار ثبت می‌کند."""
    choices = load_pending()
    try:
        menu = current_state(session, choices)
    except LookupError as e:
        print(f"\n{e}\nاین انتخاب‌ها دیگر معتبر نیستند؛ فایل {PENDING_FILE} را حذف می‌کنم.")
        clear_pending()
        return
    delta, credit = price_delta(menu, choices), menu.credit or 0
    print("\nانتخاب‌های ذخیره‌شده:\n" + "\n".join(summary_lines(menu, choices)))
    print(f"\nجمع تغییر: {delta:+,} ریال  |  موجودی: {credit:,} ریال")
    if delta > credit:
        print(f"\nموجودی هنوز کافی نیست؛ کمبود: {delta - credit:,} ریال.\n"
              "بعد از افزایش اعتبار (مثلاً از «پرداخت اینترنتی» در صفحه‌ی انتخاب غذا) دوباره همین گزینه را اجرا کن.")
        return
    if ask("\nثبت شود؟ (y/n)", "y").lower().startswith("y"):
        report(submit(session, choices, on_step=print))


MODE_KEYS = ["reserve", "file", "pending"]


def terminal_main() -> None:
    print("=== سامانه‌ی تغذیه ===\n")
    username, password = login_prompt()
    pending = load_pending()
    options = ["مشاهده‌ی منو، انتخاب غذا (در صفحه‌ی وب) و ثبت رزرو", "فقط دریافت منو و ساخت فایل"]
    if pending:
        options.append(f"ثبت انتخاب‌های ذخیره‌شده ({len(pending)} مورد، بعد از شارژ موجودی)")
    default = MODE_KEYS.index(DEFAULT_MODE) + 1 if MODE_KEYS.index(DEFAULT_MODE) < len(options) else 1
    mode = MODE_KEYS[choose("\nچه کاری انجام شود؟", options, default) - 1]

    print("\nدر حال ورود به سامانه ...")
    session = get_session(username, password)
    relogin = lambda: get_session(username, password)
    if mode == "reserve":
        mode_reserve(session, relogin, username, password)
    elif mode == "file":
        mode_menu_file(session)
    else:
        mode_pending(session, relogin)


def web_main() -> None:
    """از همان اول Chrome باز می‌شود و ورود، انتخاب کار، دریافت منو و انتخاب غذا همه در صفحه‌ی وب انجام می‌شود."""
    creds: dict = {}

    def login(u: str, p: str) -> requests.Session:
        s = get_session(u, p)
        creds.update(u=u, p=p)  # فقط در حافظه، برای ورود دوباره و پرداخت
        return s

    def set_dataset(path: str) -> None:
        global DATASET_PATH
        DATASET_PATH = path

    AppServer(
        login=login,
        fetch=lambda session, week, on_progress: fetch_menu(session, week, all_selves=True, on_progress=on_progress),
        relogin=lambda: get_session(creds["u"], creds["p"]),
        pay_opener=lambda sess, amount: open_payment(sess, amount, creds["u"], creds["p"]),
        env_user=os.environ.get("JETON_USER"), env_pass=os.environ.get("JETON_PASS"),
        dataset_ok=lambda: os.path.isdir(DATASET_PATH), set_dataset=set_dataset,
        default_mode=DEFAULT_MODE, default_week=DEFAULT_WEEK,
    ).run()


def main() -> None:
    global HEADLESS_LOGIN
    ap = argparse.ArgumentParser(description="دریافت منو و رزرو غذا")
    ap.add_argument("--ui", choices=("terminal", "web"), default=DEFAULT_UI, help="پرسش‌ها در ترمینال یا در صفحه‌ی وب")
    ap.add_argument("--headed", action="store_true", help="مرورگر لاگین/کپچا مخفی نباشد")
    args = ap.parse_args()
    if args.headed:
        HEADLESS_LOGIN = False
    web_main() if args.ui == "web" else terminal_main()


if __name__ == "__main__":
    main()
