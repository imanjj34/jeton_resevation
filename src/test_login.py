import logging
import os
import sys

from jeton_http_login import LoginError, http_login

RESERVE_URL = "https://jeton.yazd.ac.ir/Reservation/Reservation.aspx"

logging.basicConfig(level=logging.INFO, format="%(message)s")
user, password = os.environ["JETON_USER"], os.environ["JETON_PASS"]
dataset = os.environ.get("JETON_CAPTCHA_DATASET", "dataset")

try:
    session = http_login(user, password, dataset)
except LoginError as e:
    print(f"\nوضعیت ورود: ناموفق | آخرین کپچا: {e.captcha or '-'} | آدرس نهایی: {e.url or '-'}")
    sys.exit(1)

info = session.login_info
print(f"\nکپچا: {info['captcha']}\nوضعیت ورود: موفق (تلاش {info['attempt']}) | آدرس نهایی: {info['url']}")
r = session.get(RESERVE_URL, timeout=20)
ok = "txtUsernamePlain" not in r.text and "reservation" in r.url.lower()
print(f"صفحه‌ی رزرو: {'باز شد' if ok else 'به صفحه‌ی ورود برگشت'} | {r.url}")
