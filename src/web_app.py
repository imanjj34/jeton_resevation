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
from config import cfg
from ui import THEME_BUTTON, THEME_TOGGLE_JS, base_css, theme_boot
from web_select import SelectionServer
from reserve import Choice, load_pending

MODES = ("reserve", "file", "pending")


def choices_to_preselect(choices: list[Choice]) -> dict:
    """انتخاب‌های ذخیره‌شده -> مقدار اولیه‌ی رادیوباتن‌های صفحه‌ی انتخاب."""
    return {f"{c.meal}|{c.date}": (f"{c.self_id}|{c.food_id}" if c.reserve else "none") for c in choices}


class AppServer(SelectionServer):
    def __init__(self, login, fetch, relogin, pay_opener=None, env_user=None, env_pass=None,
                 dataset_ok=None, set_dataset=None, default_mode="reserve", default_week="next",
                 close_pay_window=False, exit_when_done=False):
        """
        login(username, password) -> requests.Session        (ورود و کپچا)
        fetch(session, week, on_progress) -> WeekMenu         (دریافت منو با غذاهای همه‌ی سلف‌ها)
        relogin() -> requests.Session                         (ورود دوباره با همان اطلاعات)
        dataset_ok() -> bool / set_dataset(path)              (پوشه‌ی دیتاست کپچا)

        ورود در پس‌زمینه انجام می‌شود و صفحه همان لحظه مرحله‌ی «انتخاب کار» را نشان می‌دهد (بدون انتظار و بدون تأیید).
        اگر نام کاربری/رمز در محیط تنظیم باشد، ورود حتی قبل از باز شدن صفحه شروع می‌شود.
        بعد از ورود، منوی هفته‌ی پیش‌فرض هم در پس‌زمینه دریافت می‌شود تا موقع «ادامه» آماده باشد.
        """
        super().__init__(None, None, relogin, pay_opener, refetch=fetch,
                         close_pay_window=close_pay_window, exit_when_done=exit_when_done)
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
        html = (WIZARD.replace("__THEME_BOOT__", theme_boot()).replace("__BASE_CSS__", base_css())
                .replace("__THEME_JS__", THEME_TOGGLE_JS).replace("__THEME_BUTTON__", THEME_BUTTON)
                .replace("__TOKEN__", self.token))
        return html.encode("utf-8")

    def _progress(self, n: int, total: int) -> None:
        self.bg["progress"] = {"n": n, "total": total}   # صفحه آن را نوار پیشرفت نشان می‌دهد
        self.job["progress"] = {"n": n, "total": total}

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
            return {"state": "error", "message": f"فایل {cfg.pending_file} خالی است یا وجود ندارد."}
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
        if menu is not None and time.time() - self._pf_time < cfg.prefetch_max_age:
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
                        menu, week = m, wk
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

            self.menu, self.preselect, self.notice, self.week_key = menu, preselect, notice, week
            self.stage = "select"
            self.job.update(job="idle", progress=None)
        except Exception as e:  # noqa: BLE001
            self._fail(f"خطا: {type(e).__name__}: {e}")


WIZARD = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>سامانه‌ی تغذیه</title>
__THEME_BOOT__
<style>
__BASE_CSS__
.shell{max-width:620px;margin:0 auto;padding:28px 16px 60px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px}
.tools{display:flex;gap:8px;align-items:center}
h1{font-size:1.625rem;font-weight:700;line-height:1.3}
.steps{display:flex;gap:0;list-style:none;margin:0 0 18px;padding:0}
.steps li{flex:1;padding:8px 0 10px;border-bottom:3px solid var(--line);color:var(--muted);font-size:0.812rem;text-align:right}
.steps li b{display:block;font-size:0.75rem;font-weight:700;color:var(--muted)}
.steps li.done{border-bottom-color:var(--leaf);color:var(--ink)}.steps li.done b{color:var(--leaf)}
.steps li.on{border-bottom-color:var(--tile);color:var(--ink);font-weight:700}.steps li.on b{color:var(--tile)}
#bg{display:none;margin:0 0 14px}
#err{display:none;margin:0 0 14px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}
.panel h2{font-size:1.125rem;margin-bottom:14px}
.field{display:block;margin:0 0 14px}
.field>span{display:block;font-size:0.812rem;color:var(--muted);margin-bottom:4px}
.list{display:grid;gap:8px;margin:0 0 16px}
.opt{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r);cursor:pointer}
.opt:hover{border-color:var(--muted)}
.opt:has(input:checked){border-color:var(--tile);background:var(--tile-soft)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--r);overflow:hidden;margin:0 0 16px}
.seg label{position:relative;cursor:pointer}
.seg span{display:block;padding:8px 18px;color:var(--muted);font-weight:700}
.seg input:checked+span{background:var(--tile);color:var(--tile-ink)}
.seg input:focus-visible+span{outline:3px solid var(--tile);outline-offset:-3px}
.label{font-size:0.812rem;color:var(--muted);margin:0 0 6px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.working h2{margin-bottom:6px}
.working p{color:var(--muted);font-size:0.875rem;min-height:1.6em}
.result{margin-top:14px}
</style></head><body>
<div class="shell">
<header class="top"><h1>سامانه‌ی تغذیه</h1><div class="tools">__THEME_BUTTON__</div></header>
<ol class="steps" id="steps"></ol>
<div id="bg" class="alert info" role="status"><div id="bgText"></div><div id="bgBar" class="progress" hidden><i></i></div></div>
<div id="err" class="alert err" role="alert"></div>
<div id="view"></div>
</div>
<script>
var TOKEN = "__TOKEN__", T = (location.search.match(/[?&]t=([^&]+)/) || [0, TOKEN])[1], shown = null, last = null, timer = null;
__THEME_JS__
function h(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function $(id){ return document.getElementById(id); }
function api(method, path, body, cb){
  var x = new XMLHttpRequest(); x.open(method, path); x.setRequestHeader("X-Token", TOKEN); x.setRequestHeader("Content-Type","application/json");
  x.onload = function(){ var j; try{ j=JSON.parse(x.responseText); }catch(e){ j={state:"error", message:"پاسخ نامعتبر"}; } cb(j); };
  x.onerror = function(){ cb({state:"error", message:"ارتباط با برنامه قطع شد (برنامه بسته شده؟)"}); };
  x.send(body ? JSON.stringify(body) : null);
}
function pct(p){ return p && p.total ? p.n / p.total * 100 : -1; }
function setBar(el, p){        // یک عنصر ثابت؛ فقط حالتش عوض می‌شود (انیمیشن با هر به‌روزرسانی از اول شروع نمی‌شود)
  if(p === null || p === undefined){ el.hidden = true; return; }
  el.hidden = false;
  var i = el.firstChild;
  if(p < 0){ if(el.className.indexOf("ind") < 0) el.className = "progress ind"; i.style.width = ""; el.removeAttribute("aria-valuenow"); }
  else { if(el.className.indexOf("ind") >= 0) el.className = "progress"; i.style.width = Math.round(p) + "%"; el.setAttribute("aria-valuenow", String(Math.round(p))); }
}
function mkBar(){ var b = h("div","progress"); b.setAttribute("role","progressbar"); b.setAttribute("aria-valuemin","0"); b.setAttribute("aria-valuemax","100"); b.appendChild(h("i")); return b; }
function field(label, input){ var l = h("label","field"); l.appendChild(h("span","",label)); l.appendChild(input); return l; }
function val(name){ var e=document.getElementsByName(name); for(var i=0;i<e.length;i++) if(e[i].checked) return e[i].value; return null; }
function showErr(t){ var e = $("err"); e.style.display = t ? "block" : "none"; e.textContent = t || ""; }
function setSteps(n){
  var names = ["ورود", "انتخاب کار", "دریافت منو", "انتخاب غذا"], box = $("steps"); box.innerHTML = "";
  for(var i=0;i<names.length;i++){
    var li = h("li", i+1 < n ? "done" : (i+1 === n ? "on" : "")); li.appendChild(h("b","", String(i+1))); li.appendChild(document.createTextNode(names[i])); box.appendChild(li);
  }
}
function radioRow(name, value, text, checked){
  var l = h("label","opt"), i = h("input"); i.type = "radio"; i.name = name; i.value = value; i.checked = checked;
  l.appendChild(i); l.appendChild(h("span","",text)); return l;
}
function segItem(name, value, text, checked){
  var l = h("label"), i = h("input","vh"); i.type = "radio"; i.name = name; i.value = value; i.checked = checked;
  l.appendChild(i); l.appendChild(h("span","",text)); return l;
}

function renderLogin(st){
  setSteps(1);
  var v = $("view"); v.innerHTML = "";
  var c = h("div","panel"); c.appendChild(h("h2","","ورود"));
  var u, p, d;
  if(st.env_user){ c.appendChild(h("p","label","کاربر: " + st.env_user)); }
  else {
    u = h("input"); u.type = "text"; u.autocomplete = "username"; c.appendChild(field("نام کاربری", u));
    p = h("input"); p.type = "password"; p.autocomplete = "current-password"; c.appendChild(field("رمز عبور", p));
  }
  if(st.need_dataset){ d = h("input"); d.type = "text"; c.appendChild(field("پوشه‌ی دیتاست کپچا", d)); }
  var b = h("button","btn primary", st.env_user ? "ورود دوباره" : "ورود"); b.type = "button"; c.appendChild(b); v.appendChild(c);
  function go(){
    showErr(""); b.disabled = true;
    api("POST","/api/login", {useenv: !!st.env_user, username: u?u.value:"", password: p?p.value:"", dataset: d?d.value:""}, function(j){
      if(j.state === "working"){ shown = null; tick(); }   // ورود در پس‌زمینه ادامه دارد؛ مرحله‌ی بعد همین حالا باز می‌شود
      else { showErr(j.message); b.disabled = false; }
    });
  }
  b.onclick = go;
  if(p) p.onkeydown = function(e){ if(e.keyCode === 13) go(); };
  if(u) u.focus();
}

function renderHome(st){
  setSteps(2);
  var v = $("view"); v.innerHTML = "";
  var dm = last ? last.mode : st.defaults.mode, dw = last ? last.week : st.defaults.week, df = last ? last.filename : "menu.html";
  var c = h("div","panel"); c.appendChild(h("h2","","چه کاری انجام شود؟"));
  var list = h("div","list");
  list.appendChild(radioRow("mode","reserve","مشاهده، انتخاب غذا و ثبت رزرو", dm === "reserve"));
  list.appendChild(radioRow("mode","file","فقط ساخت فایل منو", dm === "file"));
  if(st.has_pending) list.appendChild(radioRow("mode","pending","ادامه‌ی انتخاب‌های ذخیره‌شده", dm === "pending"));
  c.appendChild(list);
  var weekBox = h("div"); weekBox.appendChild(h("div","label","هفته"));
  var seg = h("div","seg"); seg.appendChild(segItem("week","current","هفته جاری", dw === "current")); seg.appendChild(segItem("week","next","هفته بعد", dw === "next"));
  weekBox.appendChild(seg); c.appendChild(weekBox);
  var fn = h("input"); fn.type = "text"; fn.value = df; fn.placeholder = "menu.html";
  var fileBox = field("نام فایل (html / json / txt)", fn); c.appendChild(fileBox);
  function sync(){ var m = val("mode"); weekBox.style.display = m === "pending" ? "none" : "block"; fileBox.style.display = m === "file" ? "block" : "none"; }
  var rs = document.getElementsByName("mode"); for(var i=0;i<rs.length;i++) rs[i].onchange = sync; sync();
  var b = h("button","btn primary","ادامه"); b.type = "button"; c.appendChild(b); v.appendChild(c);
  if(st.file){
    var ok = h("div","alert ok result"); ok.appendChild(document.createTextNode("ذخیره شد: " + st.file.path + "  "));
    var a = h("a","","باز کردن"); a.href = "/file?t=" + encodeURIComponent(T); a.target = "_blank"; ok.appendChild(a);
    if(st.file.notice) ok.appendChild(h("div","",st.file.notice));
    v.appendChild(ok);
  }
  b.onclick = function(){
    showErr(""); b.disabled = true;
    last = {mode: val("mode"), week: val("week"), filename: fn.value};
    api("POST","/api/start", last, function(j){
      if(j.state === "working"){ shown = null; tick(); } else { showErr(j.message); b.disabled = false; }
    });
  };
}

function renderBusy(st){
  setSteps(3);
  var v = $("view"); v.innerHTML = "";
  var c = h("div","panel working"); c.id = "busy"; c.appendChild(h("h2","","در حال دریافت منو"));
  var pp = h("p",""); pp.id = "busyText"; c.appendChild(pp);
  var bb = mkBar(); bb.id = "busyBar"; c.appendChild(bb); setBar(bb, -1); v.appendChild(c);
}

function banner(st){
  var b = $("bg"), g = st.bg || {}, t = "", p = null, ok = false;
  if(st.stage === "home" || st.job === "working"){
    if(g.login === "working"){ t = "در حال ورود در پس‌زمینه ..."; p = -1; }
    else if(g.login === "done"){
      ok = true; t = "ورود شد";
      var w = g.week === "next" ? "هفته بعد" : "هفته جاری";
      if(g.prefetch === "working"){ t += g.fallback ? " · هفته بعد خالی است؛ در حال دریافت هفته جاری" : " · در حال دریافت منوی " + w; p = pct(g.progress); ok = false; }
      else if(g.prefetch === "done") t += g.fallback ? " · هفته بعد خالی است؛ منوی هفته جاری آماده است" : " · منوی " + w + " آماده است";
    }
  }
  b.style.display = t ? "block" : "none";
  var cls = "alert " + (ok ? "ok" : "info"); if(b.className !== cls) b.className = cls;
  if($("bgText").textContent !== t) $("bgText").textContent = t;
  setBar($("bgBar"), st.job === "working" ? null : p);   // هنگام «دریافت منو» نوار فقط در کارت اصلی می‌آید
}

function render(st){
  showErr(st.error || (st.stage === "login" ? (st.bg || {}).login_error : null));
  banner(st);
  if(st.stage === "select"){ location.reload(); return false; }
  var want = st.job === "working" ? "busy" : st.stage;
  if(want !== shown){   // فرم فقط وقتی مرحله عوض شد ساخته می‌شود (انتخاب‌های کاربر پاک نشود)
    shown = want;
    if(want === "busy") renderBusy(st); else if(want === "login") renderLogin(st); else renderHome(st);
  }
  if(want === "busy"){
    var c = $("busy");
    if(c){ var tx = (st.log || []).slice(-1)[0] || ""; if($("busyText").textContent !== tx) $("busyText").textContent = tx; setBar($("busyBar"), pct(st.progress)); }
  }
  return true;
}
function tick(){ api("GET","/api/status",null,function(st){ if(!render(st) && timer){ clearInterval(timer); timer = null; } }); }
timer = setInterval(tick, 1000); tick();
</script></body></html>
"""
