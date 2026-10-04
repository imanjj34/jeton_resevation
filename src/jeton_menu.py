"""
jeton_menu.py - لاگین با Selenium (+ کپچا)، سپس همه‌ی کارها با requests.

    python jeton_menu.py
برنامه خودش همه‌چیز را می‌پرسد: دریافت منو و ساخت فایل، یا انتخاب غذا و ثبت رزرو (با بررسی
موجودی؛ اگر کم بود انتخاب‌ها ذخیره می‌شوند و بعد از شارژ خودکار رزرو می‌شوند).
"""
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
from reserve import (PENDING_FILE, clear_pending, current_state, load_pending, price_delta, summary_lines,
                     wait_and_submit)
from web_select import SelectionServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://jeton.yazd.ac.ir/"
HOME_URL = BASE_URL + "Master/Default.aspx"

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


def get_session(username: str, password: str, headless: bool = False) -> requests.Session:
    """با سلنیوم لاگین می‌کند، کوکی‌ها را به requests.Session می‌دهد و مرورگر را می‌بندد."""
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    if headless:
        options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else None, options=options)
    try:
        for attempt in range(1, LOGIN_RETRIES + 1):
            logger.info(f"تلاش لاگین {attempt}/{LOGIN_RETRIES}")
            if _try_login(driver, username, password):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("لاگین ناموفق (نام کاربری/رمز یا تشخیص کپچا را بررسی کن)")

        session = requests.Session()
        session.headers["User-Agent"] = driver.execute_script("return navigator.userAgent;")
        for c in driver.get_cookies():
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        return session
    finally:
        driver.quit()


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
    return ["current", "next"][choose("\nکدام هفته؟", ["هفته جاری", "هفته بعد (برای رزرو)"]) - 1]


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


def wait_flow(session, choices, relogin) -> None:
    minutes = float(ask("هر چند دقیقه موجودی چک شود تا خودکار رزرو شود؟ (0 = خروج)", "10") or 0)
    if minutes <= 0:
        print(f"انتخاب‌ها در {PENDING_FILE} ذخیره است؛ بعداً از گزینه‌ی «ادامه‌ی انتخاب‌های ذخیره‌شده» ادامه بده.")
        return
    print("در حال انتظار ... (توقف: Ctrl+C؛ انتخاب‌ها در فایل می‌مانند)")
    try:
        report(wait_and_submit(session, choices, relogin, minutes))
    except KeyboardInterrupt:
        print(f"\nمتوقف شد. انتخاب‌ها در {PENDING_FILE} ذخیره است.")


def mode_reserve(session, relogin) -> None:
    week = ask_week()
    print("\nدر حال دریافت برنامه غذایی همه‌ی سلف‌ها ...")
    menu = fetch_menu(session, week, all_selves=True, on_progress=show_progress)
    print()
    if not menu.has_food:
        print(f"\nبرای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است.")
        return
    # انتخاب در صفحه‌ی وب انجام می‌شود و بعد از «ثبت نهایی» روی سایت اعمال می‌شود
    SelectionServer(session, menu, relogin).run()


def mode_pending(session, relogin) -> None:
    choices = load_pending()
    try:
        menu = current_state(session, choices)
        print("\nانتخاب‌های ذخیره‌شده:\n" + "\n".join(summary_lines(menu, choices)))
        print(f"\nلازم: {price_delta(menu, choices):,}  |  موجودی: {(menu.credit or 0):,} ریال")
    except LookupError as e:
        print(f"\n{e}\nاین انتخاب‌ها دیگر معتبر نیستند؛ فایل {PENDING_FILE} را حذف می‌کنم.")
        clear_pending()
        return
    wait_flow(session, choices, relogin)


def main() -> None:
    print("=== سامانه‌ی تغذیه ===\n")
    username, password = login_prompt()
    pending = load_pending()
    options = ["فقط دریافت منو و ساخت فایل", "انتخاب غذا (در صفحه‌ی وب) و ثبت رزرو"]
    if pending:
        options.append(f"ادامه‌ی انتخاب‌های ذخیره‌شده ({len(pending)} مورد، منتظر موجودی)")
    mode = choose("\nچه کاری انجام شود؟", options)

    print("\nدر حال ورود به سامانه ...")
    session = get_session(username, password)
    relogin = lambda: get_session(username, password)
    if mode == 1:
        mode_menu_file(session)
    elif mode == 2:
        mode_reserve(session, relogin)
    else:
        mode_pending(session, relogin)


if __name__ == "__main__":
    main()
