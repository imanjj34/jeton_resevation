"""
jeton_menu.py - لاگین با Selenium (+ کپچا)، سپس همه‌ی کارها با requests.

    python jeton_menu.py
برنامه خودش همه‌چیز را می‌پرسد (نام کاربری/رمز اگر در JETON_USER و JETON_PASS تنظیم نشده باشد،
هفته، فرمت و نام فایل). غذاهای همه‌ی سلف‌ها استخراج می‌شود.
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


def collect_settings() -> dict:
    global DATASET_PATH
    print("=== دریافت برنامه غذایی ===\n")
    username = os.environ.get("JETON_USER") or ask("نام کاربری")
    password = os.environ.get("JETON_PASS") or getpass.getpass("رمز عبور (هنگام تایپ نمایش داده نمی‌شود): ")
    if not os.path.isdir(DATASET_PATH):
        print(f"پوشه‌ی دیتاست کپچا پیدا نشد: {DATASET_PATH}")
        DATASET_PATH = ask("مسیر پوشه‌ی دیتاست کپچا")
    week = ["current", "next"][choose("\nکدام هفته؟", ["هفته جاری", "هفته بعد (برای رزرو)"]) - 1]
    ext = [".html", ".json", ".txt"][choose("\nفرمت خروجی؟", ["HTML (صفحه‌ی وب، خواناترین)", "JSON", "متن ساده"]) - 1]
    out = ask("نام فایل خروجی", f"menu{ext}")
    if not os.path.splitext(out)[1]:
        out += ext
    open_after = ext == ".html" and ask("بعد از ساخت، در مرورگر باز شود؟ (y/n)", "y").lower().startswith("y")
    return dict(username=username, password=password, week=week, out=out, open_after=open_after)


def main() -> None:
    cfg = collect_settings()
    print("\nدر حال ورود به سامانه ...")
    session = get_session(cfg["username"], cfg["password"])

    print("در حال دریافت برنامه غذایی همه‌ی سلف‌ها ...")
    menu = fetch_menu(session, cfg["week"], all_selves=True,
                      on_progress=lambda n, total: print(f"\r  {n}/{total}", end="", flush=True))
    print()

    if not menu.has_food:
        print(f"\nبرای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است." + (f"\n{menu.message}" if menu.message else ""))
        return

    ext = os.path.splitext(cfg["out"])[1].lower()
    content = (json.dumps(menu.to_dict(), ensure_ascii=False, indent=2) if ext == ".json"
               else menu.to_text() if ext == ".txt" else menu.to_html())
    with open(cfg["out"], "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\nذخیره شد: {os.path.abspath(cfg['out'])}")
    if cfg["open_after"]:
        webbrowser.open("file://" + os.path.abspath(cfg["out"]))


if __name__ == "__main__":
    main()