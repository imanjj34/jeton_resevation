"""
web_app.py - حالت «همه‌چیز در صفحه‌ی وب»: از همان اول Chrome باز می‌شود و مرحله‌به‌مرحله

    ۱) ورود (نام کاربری/رمز)  ←  ۲) انتخاب کار و هفته  ←  ۳) دریافت منو  ←  ۴) صفحه‌ی انتخاب غذا و ثبت

به‌جای پرسش در ترمینال. بعد از مرحله‌ی ۳ همان صفحه‌ی انتخاب غذا (web_select.py) نمایش داده می‌شود.
رمز فقط به همین برنامه‌ی محلی (127.0.0.1) می‌رسد و در حافظه می‌ماند؛ در هیچ فایلی ذخیره نمی‌شود.
"""
from __future__ import annotations

import json
import os
import threading
import time

from food_menu import fallback_notice
from web_select import SelectionServer
from reserve import PENDING_FILE, Choice, load_pending

MODES = ("reserve", "file", "pending")
PREFETCH_MAX_AGE = 600  # ثانیه؛ منوی پیش‌دریافت‌شده‌ی کهنه‌تر از این دور ریخته می‌شود


def choices_to_preselect(choices: list[Choice]) -> dict:
    """انتخاب‌های ذخیره‌شده -> مقدار اولیه‌ی رادیوباتن‌های صفحه‌ی انتخاب."""
    return {f"{c.meal}|{c.date}": (f"{c.self_id}|{c.food_id}" if c.reserve else "none") for c in choices}


class AppServer(SelectionServer):
    def __init__(self, login, fetch, relogin, pay_opener=None, env_user=None, env_pass=None,
                 dataset_ok=None, set_dataset=None, default_mode="reserve", default_week="next"):
        """
        login(username, password) -> requests.Session        (ورود و کپچا)
        fetch(session, week, on_progress) -> WeekMenu         (دریافت منو با غذاهای همه‌ی سلف‌ها)
        relogin() -> requests.Session                         (ورود دوباره با همان اطلاعات)
        dataset_ok() -> bool / set_dataset(path)              (پوشه‌ی دیتاست کپچا)

        ورود در پس‌زمینه انجام می‌شود و صفحه همان لحظه مرحله‌ی «انتخاب کار» را نشان می‌دهد (بدون انتظار و بدون تأیید).
        اگر نام کاربری/رمز در محیط تنظیم باشد، ورود حتی قبل از باز شدن صفحه شروع می‌شود.
        بعد از ورود، منوی هفته‌ی پیش‌فرض هم در پس‌زمینه دریافت می‌شود تا موقع «ادامه» آماده باشد.
        """
        super().__init__(None, None, relogin, pay_opener)
        self._login, self._fetch = login, fetch
        self._env = (env_user, env_pass) if env_user and env_pass else None
        self._dataset_ok, self._set_dataset = dataset_ok, set_dataset
        self.defaults = {"mode": default_mode, "week": default_week}
        self.stage = "login"      # login (نیاز به اطلاعات) -> home (ورود شروع شده) -> select
        self.file_blob = None     # (بایت‌ها، content-type) آخرین فایل منو
        self.bg = {"login": "idle", "login_error": None, "prefetch": "idle", "progress": None, "week": None, "fallback": False}
        self._login_done, self._pf_done = threading.Event(), threading.Event()
        self._login_error = None
        self._pf_menus, self._pf_week, self._pf_time = {}, None, 0.0
        self.job.update(error=None, progress=None, file=None)

    # -- وضعیت برای صفحه
    def status(self) -> dict:
        return {**self.job, "stage": self.stage, "bg": self.bg, "env_user": self._env[0] if self._env else None,
                "need_dataset": bool(self._dataset_ok and not self._dataset_ok()),
                "has_pending": bool(load_pending()), "defaults": self.defaults}

    def routes(self) -> dict:
        return {**super().routes(), "/api/login": self.api_login, "/api/start": self.api_start}

    def extra_get(self, path: str):
        return self.file_blob if path == "/file" else None

    def page(self) -> bytes:
        if self.menu is not None:
            return super().page()
        return WIZARD.replace("__TOKEN__", self.token).encode("utf-8")

    def _progress(self, n: int, total: int) -> None:
        self.bg["progress"] = f"{n}/{total}"
        self.job["progress"] = f"{n}/{total}"

    def _fail(self, text: str) -> None:
        self._log(text)
        self.job.update(job="idle", error=text, progress=None)

    def run(self, launch: bool = True) -> None:
        # با اطلاعات محیط، ورود قبل از باز شدن صفحه شروع می‌شود (نیازی به کلیک نیست)
        if self._env and not (self._dataset_ok and not self._dataset_ok()):
            self.begin_login(*self._env)
        super().run(launch)

    # -- مرحله‌ی ۱: ورود (در پس‌زمینه؛ صفحه منتظرش نمی‌ماند)
    def begin_login(self, user: str, pw: str) -> None:
        self._login_done.clear()
        self._pf_done.clear()
        self._login_error, self._pf_menus, self._pf_week = None, {}, None
        self.bg.update(login="working", login_error=None, prefetch="idle", progress=None, week=None, fallback=False)
        self.stage = "home"
        threading.Thread(target=self._login_job, args=(user, pw), daemon=True).start()

    def api_login(self, body: dict) -> dict:
        if self.stage != "login" or self.bg["login"] == "working":
            return {"state": "error", "message": "ورود در حال انجام است یا قبلاً انجام شده."}
        if self._env and body.get("useenv"):
            user, pw = self._env
        else:
            user, pw = str(body.get("username", "")).strip(), str(body.get("password", ""))
        if not user or not pw:
            return {"state": "error", "message": "نام کاربری و رمز را وارد کن."}
        dataset = str(body.get("dataset", "")).strip()
        if dataset:
            if not os.path.isdir(dataset):
                return {"state": "error", "message": "این پوشه‌ی دیتاست کپچا وجود ندارد."}
            self._set_dataset(dataset)
        elif self._dataset_ok and not self._dataset_ok():
            return {"state": "error", "message": "مسیر پوشه‌ی دیتاست کپچا را وارد کن."}
        self.job.update(error=None)
        self.begin_login(user, pw)
        return {"state": "working", "stage": self.stage}

    def _login_job(self, user: str, pw: str) -> None:
        self._log("در حال ورود به سامانه (مرورگر مخفی + تشخیص کپچا) ...")
        try:
            self.session = self._login(user, pw)
        except Exception as e:  # noqa: BLE001
            self._login_error = f"ورود ناموفق: {e}"
            self.bg.update(login="error", login_error=self._login_error)
            self.stage = "login"  # دوباره فرم ورود با پیام خطا
            self._log(self._login_error)
            self._login_done.set()
            self._pf_done.set()
            return
        self.bg["login"] = "done"
        self._log("ورود موفق ✓")
        self._login_done.set()
        # منوی هفته‌ی پیش‌فرض را همین حالا (همزمان با تصمیم‌گیری کاربر) دریافت کن؛
        # اگر هفته‌ی بعد هنوز غذایی نداشت، بلافاصله هفته‌ی جاری را هم بگیر
        if self.defaults["mode"] in ("reserve", "file"):
            week = self.defaults["week"]
            self._pf_week = week
            self.bg.update(prefetch="working", week=week, progress=None)
            try:
                menu = self._fetch(self.session, week, self._progress)
                self._pf_menus[week] = menu
                if week == "next" and not menu.has_food:
                    self.bg.update(fallback=True, progress=None)
                    self._log("هفته بعد هنوز غذایی ندارد؛ در حال دریافت هفته جاری ...")
                    self._pf_menus["current"] = self._fetch(self.session, "current", self._progress)
                self._pf_time = time.time()
                self.bg["prefetch"] = "done"
            except Exception:  # noqa: BLE001  (بعداً در پیش‌زمینه دوباره امتحان می‌شود)
                self.bg["prefetch"] = "error"
        self._pf_done.set()

    # -- مرحله‌ی ۲ و ۳: انتخاب کار، دریافت منو
    def api_start(self, body: dict) -> dict:
        if self.stage != "home" or self.job["job"] == "working":
            return {"state": "error", "message": "الان نمی‌شود شروع کرد."}
        mode, week = body.get("mode"), body.get("week", "next")
        if mode not in MODES or week not in ("current", "next"):
            return {"state": "error", "message": "گزینه‌ی نامعتبر."}
        if mode == "pending" and not load_pending():
            return {"state": "error", "message": f"فایل {PENDING_FILE} خالی است یا وجود ندارد."}
        name = os.path.basename(str(body.get("filename") or "menu.html")) or "menu.html"
        self.job.update(job="working", log=[], error=None, progress=None, file=None)
        threading.Thread(target=self._start_job, args=(mode, week, name), daemon=True).start()
        return {"state": "working"}

    def _title(self, week: str) -> str:
        return "هفته جاری" if week == "current" else "هفته بعد"

    def _take_prefetched(self, week: str):
        """منوی پیش‌دریافت‌شده‌ی همین هفته (اگر تازه باشد). فقط یک بار مصرف می‌شود."""
        # هفته‌ی جاری فقط وقتی «پیش‌دریافت» حساب می‌شود که جایگزینِ هفته‌ی بعدِ خالی شروع شده باشد
        if week != self._pf_week and not (week == "current" and self._pf_week == "next" and self.bg.get("fallback")):
            return None
        if not self._pf_done.is_set():
            self._log(f"منوی {self._title(week)} در پس‌زمینه در حال دریافت است؛ منتظر تکمیلش می‌مانم ...")
        self._pf_done.wait()
        menu = self._pf_menus.pop(week, None)
        if menu is not None and time.time() - self._pf_time < PREFETCH_MAX_AGE:
            self._log(f"منوی {self._title(week)} آماده بود ✓")
            return menu
        return None

    def _get_week(self, week: str):
        menu = self._take_prefetched(week)
        if menu is None:
            self._log(f"در حال دریافت منوی {self._title(week)} (غذاهای همه‌ی سلف‌ها) ...")
            menu = self._with_relogin(lambda: self._fetch(self.session, week, self._progress))
        return menu

    def _fetch_week(self, week: str, fallback: bool = True):
        """(منو، پیام). اگر هفته‌ی بعد هنوز غذایی ندارد، فوراً منوی هفته‌ی جاری را می‌دهد (با پیام اطلاع‌رسانی)."""
        menu = self._get_week(week)
        if fallback and week == "next" and not menu.has_food:
            self._log("هفته بعد هنوز غذایی ندارد؛ منوی هفته جاری را می‌گیرم ...")
            current = self._get_week("current")
            if current.has_food:
                return current, fallback_notice(menu, current)
        return menu, None

    def _start_job(self, mode: str, week: str, filename: str) -> None:
        try:
            if not self._login_done.is_set():
                self._log("منتظر پایان ورود ...")
            self._login_done.wait()
            if self._login_error:
                return self._fail(self._login_error)
            preselect, notice = {}, None
            if mode == "pending":
                pend = load_pending()
                dates = {c.date for c in pend}
                menu = None
                for wk in ("current", "next"):  # هفته‌ای که تاریخ‌های ذخیره‌شده در آن است
                    m, _ = self._fetch_week(wk, fallback=False)
                    if dates <= {d.date for ms in m.meals for d in ms.days}:
                        menu = m
                        break
                if menu is None:
                    return self._fail("تاریخ‌های انتخاب‌های ذخیره‌شده در هفته‌ی جاری و بعد پیدا نشد.")
                preselect = choices_to_preselect(pend)
            else:
                menu, notice = self._fetch_week(week)
                if notice:
                    self._log(notice)
            self._log(f"هفته: {menu.week_range}")
            if not menu.has_food:
                return self._fail(f"برای این هفته ({menu.week_range}) هنوز غذایی ثبت نشده است."
                                  + (f" ({menu.message})" if menu.message else ""))

            if mode == "file":
                ext = os.path.splitext(filename)[1].lower()
                if ext == ".json":
                    content, ctype = json.dumps(menu.to_dict(), ensure_ascii=False, indent=2), "application/json; charset=utf-8"
                elif ext == ".txt":
                    content, ctype = menu.to_text(), "text/plain; charset=utf-8"
                else:
                    if not ext:
                        filename += ".html"
                    content, ctype = menu.to_html(), "text/html; charset=utf-8"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(content)
                self.file_blob = (content.encode("utf-8"), ctype)
                self.job.update(job="idle", progress=None, file={"path": os.path.abspath(filename), "name": filename, "notice": notice})
                self._log(f"ذخیره شد: {os.path.abspath(filename)}")
                return

            self.menu, self.preselect, self.notice = menu, preselect, notice
            self.stage = "select"
            self.job.update(job="idle", progress=None)
        except Exception as e:  # noqa: BLE001
            self._fail(f"خطا: {type(e).__name__}: {e}")


WIZARD = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>سامانه‌ی تغذیه</title>
<style>
body{font-family:Vazirmatn,Tahoma,sans-serif;max-width:560px;margin:40px auto;padding:0 14px;color:#222}
h1{font-size:21px}.card{border:1px solid #ccc;border-radius:10px;padding:16px 18px;margin:14px 0;background:#fafafa}
label.row{display:table;width:100%;margin:8px 0;cursor:pointer}.rc{display:table-cell;width:26px;vertical-align:top;padding-top:2px}.tc{display:table-cell;vertical-align:top}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:8px;font-size:15px;margin:4px 0 10px}
button{font-family:inherit;font-size:15px;padding:9px 22px;border:0;border-radius:6px;background:#2e7d32;color:#fff;cursor:pointer}
button:disabled{background:#aaa}.hint{color:#666;font-size:12px}
#bg{margin:8px 0;padding:8px 12px;border-radius:6px;background:#fff8e1;border:1px solid #ffe082;font-size:13px;display:none}#bg.done{background:#e8f5e9;border-color:#a5d6a7}
#log{white-space:pre-wrap;line-height:1.8;font-size:14px;margin-top:10px}.err{background:#ffebee;border:1px solid #ef9a9a;padding:10px;border-radius:6px;margin:10px 0}
.ok{background:#e8f5e9;border:1px solid #a5d6a7;padding:10px;border-radius:6px;margin:10px 0}
.steps{color:#888;font-size:13px;display:flex;gap:14px;flex-wrap:wrap}.steps b{color:#2e7d32}
</style></head><body>
<h1>سامانه‌ی تغذیه</h1>
<div class="steps" id="steps"></div>
<div id="bg"></div>
<div id="err" class="err" style="display:none"></div>
<div id="view"></div>
<div id="log"></div>
<script>
var TOKEN = "__TOKEN__", T = (location.search.match(/[?&]t=([^&]+)/) || [0, TOKEN])[1], shown = null, last = null;
function h(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function api(method, path, body, cb){
  var x = new XMLHttpRequest(); x.open(method, path); x.setRequestHeader("X-Token", TOKEN); x.setRequestHeader("Content-Type","application/json");
  x.onload = function(){ var j; try{ j=JSON.parse(x.responseText); }catch(e){ j={state:"error", message:"پاسخ نامعتبر"}; } cb(j); };
  x.onerror = function(){ cb({state:"error", message:"ارتباط با برنامه قطع شد (برنامه بسته شده؟)"}); };
  x.send(body ? JSON.stringify(body) : null);
}
function radio(name, value, text, checked){
  var l = h("label","row"), i = h("input"); i.type="radio"; i.name=name; i.value=value; i.checked=checked;
  var rc = h("span","rc"), tc = h("span","tc",text); rc.appendChild(i); l.appendChild(rc); l.appendChild(tc); return l;
}
function val(name){ var e=document.getElementsByName(name); for(var i=0;i<e.length;i++) if(e[i].checked) return e[i].value; return null; }
function showErr(t){ var e=document.getElementById("err"); e.style.display = t ? "block" : "none"; e.textContent = t || ""; }
function setSteps(n){
  var names = ["۱) ورود", "۲) انتخاب کار", "۳) دریافت منو", "۴) انتخاب غذا"], box = document.getElementById("steps");
  box.innerHTML = "";
  for(var i=0;i<names.length;i++) box.appendChild(h(i+1===n ? "b" : "span", "", names[i]));
}

function renderLogin(st){
  setSteps(1);
  var v = document.getElementById("view"); v.innerHTML = "";
  var c = h("div","card"); c.appendChild(h("div","","ورود به سامانه"));
  var u, p, d;
  if(st.env_user){
    c.appendChild(h("p","hint","اطلاعات ورود در محیط تنظیم شده است (کاربر: " + st.env_user + ")."));
  } else {
    c.appendChild(h("div","hint","نام کاربری")); u = h("input"); u.type="text"; c.appendChild(u);
    c.appendChild(h("div","hint","رمز عبور (فقط به همین برنامه‌ی محلی می‌رسد)")); p = h("input"); p.type="password"; c.appendChild(p);
  }
  if(st.need_dataset){
    c.appendChild(h("div","hint","مسیر پوشه‌ی دیتاست کپچا")); d = h("input"); d.type="text"; c.appendChild(d);
  }
  var b = h("button","", st.env_user ? "ورود دوباره" : "ورود"); c.appendChild(b); v.appendChild(c);
  function go(){
    showErr(""); b.disabled = true;
    api("POST","/api/login", {useenv: !!st.env_user, username: u?u.value:"", password: p?p.value:"", dataset: d?d.value:""}, function(j){
      if(j.state==="working"){ shown = null; tick(); }  // بلافاصله مرحله‌ی بعد؛ ورود در پس‌زمینه ادامه دارد
      else { showErr(j.message); b.disabled=false; }
    });
  }
  b.onclick = go;
  if(p) p.onkeydown = function(e){ if(e.keyCode===13) go(); };
}

function renderHome(st){
  setSteps(2);
  var v = document.getElementById("view"); v.innerHTML = "";
  var dm = last ? last.mode : st.defaults.mode, dw = last ? last.week : st.defaults.week, df = last ? last.filename : "menu.html";
  var c = h("div","card"); c.appendChild(h("div","","چه کاری انجام شود؟"));
  c.appendChild(radio("mode","reserve","مشاهده‌ی منو، انتخاب غذا و ثبت رزرو", dm==="reserve"));
  c.appendChild(radio("mode","file","فقط دریافت منو و ساخت فایل", dm==="file"));
  if(st.has_pending) c.appendChild(radio("mode","pending","ادامه‌ی انتخاب‌های ذخیره‌شده (بعد از شارژ موجودی)", dm==="pending"));
  var weekBox = h("div"); weekBox.appendChild(h("div","","کدام هفته؟"));
  weekBox.appendChild(radio("week","current","هفته جاری", dw==="current"));
  weekBox.appendChild(radio("week","next","هفته بعد (برای رزرو)", dw==="next"));
  c.appendChild(weekBox);
  var fileBox = h("div"); fileBox.appendChild(h("div","hint","نام فایل (پسوند: .html یا .json یا .txt)"));
  var fn = h("input"); fn.type="text"; fn.value=df; fileBox.appendChild(fn); c.appendChild(fileBox);
  function sync(){ var m = val("mode"); weekBox.style.display = m==="pending" ? "none" : "block"; fileBox.style.display = m==="file" ? "block" : "none"; }
  var rs = document.getElementsByName("mode"); for(var i=0;i<rs.length;i++) rs[i].onchange = sync; sync();
  var b = h("button","","ادامه"); c.appendChild(b); v.appendChild(c);
  if(st.file){
    var ok = h("div","ok"); ok.appendChild(document.createTextNode("ذخیره شد: " + st.file.path + "  "));
    var a = h("a","","باز کردن در مرورگر"); a.href = "/file?t=" + encodeURIComponent(T); a.target = "_blank"; ok.appendChild(a);
    if(st.file.notice) ok.appendChild(h("div","hint",st.file.notice));
    v.appendChild(ok);
  }
  b.onclick = function(){
    showErr(""); b.disabled = true;
    last = {mode: val("mode"), week: val("week"), filename: fn.value};
    api("POST","/api/start", last, function(j){
      if(j.state==="working"){ shown = null; tick(); } else { showErr(j.message); b.disabled=false; }
    });
  };
}

function banner(st){
  var b = document.getElementById("bg"), g = st.bg || {}, t = "", done = false;
  if(st.stage==="home" || st.job==="working"){
    if(g.login==="working") t = "در حال ورود به سامانه در پس‌زمینه ... می‌توانی همین حالا انتخاب‌هایت را بزنی.";
    else if(g.login==="done"){
      done = true; t = "ورود موفق ✓";
      if(g.prefetch==="working") t += g.fallback
          ? " — هفته بعد هنوز غذایی ندارد؛ در حال دریافت منوی هفته جاری" + (g.progress ? " (" + g.progress + ")" : "") + " ..."
          : " — در حال دریافت منوی " + (g.week==="next" ? "هفته بعد" : "هفته جاری") + " در پس‌زمینه" + (g.progress ? " (" + g.progress + ")" : "") + " ...";
      else if(g.prefetch==="done") t += g.fallback ? " — هفته بعد هنوز غذایی ندارد؛ منوی هفته جاری آماده است ✓" : " — منو آماده است ✓";
    }
  }
  b.style.display = t ? "block" : "none"; b.className = done ? "done" : ""; b.textContent = t;
}

function render(st){
  showErr(st.error || (st.stage==="login" ? (st.bg||{}).login_error : null));
  banner(st);
  document.getElementById("log").textContent = st.job==="working" ? ((st.log||[]).join("\n") + (st.progress ? ("\n" + st.progress) : "")) : "";
  if(st.stage==="select"){ location.reload(); return false; }
  var want = st.job==="working" ? "busy" : st.stage;
  if(want!==shown){  // فرم فقط وقتی مرحله عوض شد دوباره ساخته می‌شود (انتخاب‌های کاربر پاک نشود)
    shown = want;
    if(want==="busy"){ document.getElementById("view").innerHTML = ""; setSteps(3); }
    else if(want==="login") renderLogin(st); else renderHome(st);
  }
  return true;
}

var timer = null;
function tick(){
  api("GET","/api/status",null,function(st){ if(!render(st) && timer){ clearInterval(timer); timer = null; } });
}
timer = setInterval(tick, 1000); tick();
</script></body></html>
"""
