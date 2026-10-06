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
import threading
import time
import webbrowser

import requests

from food_menu import fallback_notice, fetch_menu, fetch_menu_with_fallback
from payment import CALLBACK_MARK, parse_callback
from reserve import PENDING_FILE, clear_pending, current_state, load_pending, price_delta, submit, summary_lines
from web_app import AppServer
from web_select import SelectionServer

# کتابخانه‌های سنگین (opencv/scikit-image/selenium) تنبل بارگذاری می‌شوند تا اولین پرسش فوراً نمایش داده شود؛
# بارگذاری‌شان در پس‌زمینه انجام می‌شود (preload_heavy) و هر تابعی که لازم دارد _load_heavy() را صدا می‌زند.
cv2 = np = ssim = webdriver = By = WebDriverWait = Service = TimeoutException = None
_heavy_lock = threading.Lock()


def _load_heavy() -> None:
    global cv2, np, ssim, webdriver, By, WebDriverWait, Service, TimeoutException
    with _heavy_lock:
        if cv2 is not None:
            return
        import numpy as _np
        from selenium import webdriver as _webdriver
        from selenium.common.exceptions import TimeoutException as _Timeout
        from selenium.webdriver.chrome.service import Service as _Service
        from selenium.webdriver.common.by import By as _By
        from selenium.webdriver.support.ui import WebDriverWait as _Wait
        from skimage.metrics import structural_similarity as _ssim
        import cv2 as _cv2
        np, webdriver, TimeoutException, Service, By, WebDriverWait, ssim = _np, _webdriver, _Timeout, _Service, _By, _Wait, _ssim
        cv2 = _cv2  # آخر؛ نشانه‌ی آماده بودن


def preload_heavy() -> None:
    threading.Thread(target=_load_heavy, daemon=True).start()


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
CLOSE_PAYMENT_WINDOW_WHEN_DONE = True  # بعد از نتیجه‌ی پرداخت، پنجره‌ی بانک خودکار بسته شود؟
EXIT_AFTER_SUCCESS = False              # بعد از ثبت موفق، خودِ برنامه (سرور محلی) بسته شود؟ False = باز می‌ماند تا Ctrl+C

DATASET_PATH = os.environ.get("JETON_CAPTCHA_DATASET", r"C:\Users\iman\Documents\project\image vision\dataset")
EAGER_LOAD = True          # صفحه‌ی لاگین را بدون انتظار برای همه‌ی منابع باز کن (سریع‌تر). اگر مشکل داشت False کن
CAPTCHA_THRESHOLD = 0.4
LOGIN_RETRIES = 5
CHROMEDRIVER_PATH = os.environ.get("JETON_CHROMEDRIVER")  # اختیاری: مسیر chromedriver.exe دستی


# ------------------------------------------------- تشخیص کپچا (همان الگوریتم SSIM قبلی)
_dataset_cache: dict = {}
_dataset_lock = threading.Lock()


def _load_dataset(path: str) -> dict:
    """تصاویر مرجع فقط یک بار از دیسک خوانده می‌شوند (قبلاً برای هر رقم و هر تلاش دوباره خوانده می‌شدند)."""
    with _dataset_lock:
        if path not in _dataset_cache:
            data = {}
            for digit in os.listdir(path):
                digit_path = os.path.join(path, digit)
                if not os.path.isdir(digit_path):
                    continue
                refs = []
                for file in os.listdir(digit_path):
                    if file.lower().endswith((".png", ".jpg", ".jpeg")):
                        ref = cv2.imread(os.path.join(digit_path, file), cv2.IMREAD_GRAYSCALE)
                        if ref is not None:
                            refs.append(ref)
                data[digit] = refs
            _dataset_cache[path] = data
        return _dataset_cache[path]


def recognize(driver) -> str:
    _load_heavy()
    crop_coords = [(9, 77, 34, 94), (9, 98, 34, 115), (9, 119, 34, 136), (9, 139, 34, 156)]
    try:
        WebDriverWait(driver, 10, poll_frequency=0.1).until(lambda d: d.execute_script(
            "var i=document.getElementById('Img1');return !!i && i.complete && i.naturalWidth>0;"))
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
        dataset = _load_dataset(DATASET_PATH)

        result = ""
        for input_img in digit_images:
            best_digit, best_score = "?", 0
            for digit, refs in dataset.items():
                scores = []
                for ref in refs:
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
    _load_heavy()
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
        WebDriverWait(driver, 5, poll_frequency=0.1).until(lambda d: d.current_url == HOME_URL)
        return True
    except TimeoutException:
        return False


def _new_driver(headless: bool = False, eager: bool = False) -> webdriver.Chrome:
    _load_heavy()
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-gpu")
    if eager:
        options.page_load_strategy = "eager"  # فقط برای لاگین؛ پنجره‌ی پرداخت کامل لود می‌شود
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
        time.sleep(0.2)
    raise RuntimeError("لاگین ناموفق (نام کاربری/رمز یا تشخیص کپچا را بررسی کن)")


def _cookies_to_session(driver, session: requests.Session) -> None:
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))


def get_session(username: str, password: str, headless: bool | None = None) -> requests.Session:
    """با سلنیوم لاگین می‌کند (پیش‌فرض: مخفی)، کوکی‌ها را به requests.Session می‌دهد و مرورگر را می‌بندد."""
    driver = _new_driver(HEADLESS_LOGIN if headless is None else headless, eager=EAGER_LOAD)
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
        _load_heavy()
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
    _load_heavy()
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


# ------------------------------------------------- ورود و دریافت منو در پس‌زمینه
class Prefetch:
    """
    ورود (و در صورت لزوم دریافت منوی هفته‌ی پیش‌فرض) را همان لحظه در پس‌زمینه شروع می‌کند تا همزمان با
    پرسیدن بقیه‌ی سؤال‌ها انجام شود. اگر هفته‌ی بعد هنوز غذایی نداشته باشد، بلافاصله منوی هفته‌ی جاری را هم
    می‌گیرد. اگر کاربر هفته‌ی دیگری بخواهد، منوی پیش‌دریافت‌شده کنار گذاشته می‌شود.
    """

    def __init__(self, username: str, password: str, week: str | None = None):
        self.week, self.session = week, None
        self.menus: dict = {}       # هفته -> منو (فقط چیزهایی که در پس‌زمینه دریافت شده)
        self.error: Exception | None = None
        self.progress = ""
        self.login_done, self.menu_done = threading.Event(), threading.Event()
        self.fallback_started = threading.Event()  # هفته‌ی بعد خالی بود و هفته‌ی جاری در حال دریافت است
        threading.Thread(target=self._run, args=(username, password), daemon=True).start()

    def _run(self, username: str, password: str) -> None:
        try:
            self.session = get_session(username, password)
        except Exception as e:  # noqa: BLE001
            self.error = e
            self.login_done.set()
            self.menu_done.set()
            return
        self.login_done.set()
        prog = lambda n, t: setattr(self, "progress", f"{n}/{t}")  # noqa: E731
        if self.week:
            try:
                self.menus[self.week] = fetch_menu(self.session, self.week, all_selves=True, on_progress=prog)
                if self.week == "next" and not self.menus["next"].has_food:  # هفته بعد هنوز خالی است: فوراً هفته جاری
                    self.fallback_started.set()
                    self.menus["current"] = fetch_menu(self.session, "current", all_selves=True, on_progress=prog)
            except Exception:  # noqa: BLE001  (بعداً در پیش‌زمینه دوباره امتحان می‌شود)
                pass
        self.menu_done.set()

    def wait(self, week: str | None):
        """(session، منوی آماده یا None، پیام اطلاع‌رسانی یا None). اگر ورود شکست خورده باشد همان خطا بالا می‌آید."""
        if not self.login_done.is_set():
            print("در حال تکمیل ورود به سامانه ...")
        self.login_done.wait()
        if self.error:
            raise self.error
        if not week or week not in ("next", "current") or not self.week:
            return self.session, None, None
        if week != self.week and not (week == "current" and self.fallback_started.is_set()):
            return self.session, None, None  # این هفته پیش‌دریافت نمی‌شود؛ مستقیم گرفته می‌شود
        if not self.menu_done.is_set():
            print("در حال تکمیل دریافت منو ...")
        self.menu_done.wait()
        menu = self.menus.get(week)
        if menu is not None and not (week == "next" and not menu.has_food):
            return self.session, menu, None
        current = self.menus.get("current")
        if week == "next" and menu is not None and current is not None and current.has_food:
            return self.session, current, fallback_notice(menu, current)
        return self.session, menu, None


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
    width = 30
    filled = int(width * n / total) if total else width
    print(f"\r  [{'█' * filled}{'░' * (width - filled)}] {int(100 * n / total) if total else 100}٪", end="", flush=True)


def report(results: list[dict]) -> None:
    print()
    for r in results:
        print(f"{'✓' if r['ok'] else '✗'} {r['meal']}" + (f" - پیام سایت: {r['message']}" if r.get("message") else ""))
        for pr in r["problems"]:
            print(f"    ! {pr}")
        for w in r.get("warnings", []):
            print(f"    ؟ {w}")
        if r.get("credit") is not None:
            print(f"    موجودی پس از ذخیره: {r['credit']:,} ریال")
    if all(r["ok"] for r in results):
        clear_pending()
        print("\nهمه‌ی تغییرات ثبت و تأیید شد.")
    else:
        print("\nبعضی موارد ثبت نشد؛ پیام‌های بالا را ببین و صفحه‌ی سایت را هم چک کن.")


# ------------------------------------------------------------------ حالت‌ها
def ask_file_options() -> tuple[str, bool]:
    ext = [".html", ".json", ".txt"][choose("\nفرمت خروجی؟", ["HTML (صفحه‌ی وب، خواناترین)", "JSON", "متن ساده"]) - 1]
    out = ask("نام فایل خروجی", f"menu{ext}")
    if not os.path.splitext(out)[1]:
        out += ext
    open_after = ext == ".html" and ask("بعد از ساخت، در مرورگر باز شود؟ (y/n)", "y").lower().startswith("y")
    return out, open_after


def _menu_or_fetch(session, week: str, menu=None):
    """منوی آماده را برمی‌گرداند یا (با جایگزینی هفته جاری اگر هفته بعد خالی بود) همین حالا می‌گیرد."""
    if menu is None:
        print("\nدر حال دریافت برنامه غذایی همه‌ی سلف‌ها ...")
        menu, notice = fetch_menu_with_fallback(session, week, all_selves=True, on_progress=show_progress)
        print()
        if notice:
            print(notice)
    return menu


def mode_menu_file(session, week: str, out: str, open_after: bool, menu=None) -> None:
    menu = _menu_or_fetch(session, week, menu)
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


def mode_reserve(session, relogin, username: str, password: str, week: str, menu=None) -> None:
    menu = _menu_or_fetch(session, week, menu)
    if not menu.has_food:
        print(f"\nبرای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است.")
        return
    # انتخاب در صفحه‌ی وب انجام می‌شود و بعد از «ثبت نهایی» روی سایت اعمال می‌شود
    SelectionServer(session, menu, relogin,
                    pay_opener=lambda sess, amount: open_payment(sess, amount, username, password),
                    refetch=lambda sess, wk, on_progress: fetch_menu(sess, wk, all_selves=True, on_progress=on_progress),
                    week_key=week, close_pay_window=CLOSE_PAYMENT_WINDOW_WHEN_DONE, exit_when_done=EXIT_AFTER_SUCCESS).run()


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
    preload_heavy()  # همزمان با پرسش‌ها بارگذاری می‌شود
    print("=== سامانه‌ی تغذیه ===\n")
    username, password = login_prompt()
    pending = load_pending()

    # از همین لحظه ورود (و دریافت منوی هفته‌ی پیش‌فرض) در پس‌زمینه شروع می‌شود؛ بقیه‌ی سؤال‌ها همزمان پرسیده می‌شود
    prefetch = Prefetch(username, password, DEFAULT_WEEK if DEFAULT_MODE in ("reserve", "file") else None)

    options = ["مشاهده‌ی منو، انتخاب غذا (در صفحه‌ی وب) و ثبت رزرو", "فقط دریافت منو و ساخت فایل"]
    if pending:
        options.append(f"ثبت انتخاب‌های ذخیره‌شده ({len(pending)} مورد، بعد از شارژ موجودی)")
    default = MODE_KEYS.index(DEFAULT_MODE) + 1 if MODE_KEYS.index(DEFAULT_MODE) < len(options) else 1
    mode = MODE_KEYS[choose("\nچه کاری انجام شود؟", options, default) - 1]
    week = ask_week() if mode in ("reserve", "file") else None
    file_opts = ask_file_options() if mode == "file" else None

    session, menu, notice = prefetch.wait(week)  # فقط اگر ورود هنوز تمام نشده باشد منتظر می‌ماند
    if notice:
        print("\n" + notice)
    relogin = lambda: get_session(username, password)
    if mode == "reserve":
        mode_reserve(session, relogin, username, password, week, menu)
    elif mode == "file":
        mode_menu_file(session, week, *file_opts, menu=menu)
    else:
        mode_pending(session, relogin)


def web_main() -> None:
    """از همان اول Chrome باز می‌شود و ورود، انتخاب کار، دریافت منو و انتخاب غذا همه در صفحه‌ی وب انجام می‌شود."""
    preload_heavy()  # همزمان با باز شدن صفحه بارگذاری می‌شود
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
        close_pay_window=CLOSE_PAYMENT_WINDOW_WHEN_DONE, exit_when_done=EXIT_AFTER_SUCCESS,
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
