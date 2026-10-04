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

from web_select import SelectionServer
from reserve import PENDING_FILE, Choice, load_pending

MODES = ("reserve", "file", "pending")


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
        """
        super().__init__(None, None, relogin, pay_opener)
        self._login, self._fetch = login, fetch
        self._env = (env_user, env_pass) if env_user and env_pass else None
        self._dataset_ok, self._set_dataset = dataset_ok, set_dataset
        self.defaults = {"mode": default_mode, "week": default_week}
        self.stage = "login"      # login -> home -> select
        self.file_blob = None     # (بایت‌ها، content-type) آخرین فایل منو
        self.job.update(error=None, progress=None, file=None)

    # -- وضعیت برای صفحه
    def status(self) -> dict:
        return {**self.job, "stage": self.stage, "env_user": self._env[0] if self._env else None,
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
        self.job["progress"] = f"{n}/{total}"

    def _fail(self, text: str) -> None:
        self._log(text)
        self.job.update(job="idle", error=text, progress=None)

    # -- مرحله‌ی ۱: ورود
    def api_login(self, body: dict) -> dict:
        if self.job["job"] == "working" or self.stage != "login":
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
        self.job.update(job="working", log=[], error=None, progress=None, file=None)
        threading.Thread(target=self._login_job, args=(user, pw), daemon=True).start()
        return {"state": "working"}

    def _login_job(self, user: str, pw: str) -> None:
        self._log("در حال ورود به سامانه (مرورگر مخفی + تشخیص کپچا) ...")
        try:
            self.session = self._login(user, pw)
        except Exception as e:  # noqa: BLE001
            self._fail(f"ورود ناموفق: {e}")
            return
        self.stage = "home"
        self._log("ورود موفق ✓")
        self.job.update(job="idle")

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

    def _fetch_week(self, week: str):
        title = "هفته جاری" if week == "current" else "هفته بعد"
        self._log(f"در حال دریافت منوی {title} (غذاهای همه‌ی سلف‌ها) ...")
        return self._with_relogin(lambda: self._fetch(self.session, week, self._progress))

    def _start_job(self, mode: str, week: str, filename: str) -> None:
        try:
            preselect = {}
            if mode == "pending":
                pend = load_pending()
                dates = {c.date for c in pend}
                menu = None
                for wk in ("current", "next"):  # هفته‌ای که تاریخ‌های ذخیره‌شده در آن است
                    m = self._fetch_week(wk)
                    if dates <= {d.date for ms in m.meals for d in ms.days}:
                        menu = m
                        break
                if menu is None:
                    return self._fail("تاریخ‌های انتخاب‌های ذخیره‌شده در هفته‌ی جاری و بعد پیدا نشد.")
                preselect = choices_to_preselect(pend)
            else:
                menu = self._fetch_week(week)
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
                self.job.update(job="idle", progress=None, file={"path": os.path.abspath(filename), "name": filename})
                self._log(f"ذخیره شد: {os.path.abspath(filename)}")
                return

            self.menu, self.preselect = menu, preselect
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
label.row{display:table;width:100%;margin:8px 0;cursor:pointer}.rc{display:table-cell;width:26px;vertical-align:top;padding-top:2px}.tc{display:table-cell;vertical-align:top}input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:8px;font-size:15px;margin:4px 0 10px}
button{font-family:inherit;font-size:15px;padding:9px 22px;border:0;border-radius:6px;background:#2e7d32;color:#fff;cursor:pointer}
button:disabled{background:#aaa}button.sec{background:#757575}.hint{color:#666;font-size:12px}
#log{white-space:pre-wrap;line-height:1.8;font-size:14px;margin-top:10px}.err{background:#ffebee;border:1px solid #ef9a9a;padding:10px;border-radius:6px;margin:10px 0}
.ok{background:#e8f5e9;border:1px solid #a5d6a7;padding:10px;border-radius:6px;margin:10px 0}.steps{color:#888;font-size:13px;display:flex;gap:14px;flex-wrap:wrap}.steps b{color:#2e7d32}
</style></head><body>
<h1>سامانه‌ی تغذیه</h1>
<div class="steps" id="steps"></div>
<div id="err" class="err" style="display:none"></div>
<div id="view"></div>
<div id="log"></div>
<script>
var TOKEN = "__TOKEN__", T = (location.search.match(/[?&]t=([^&]+)/) || [0, TOKEN])[1], poll = null, S = null;
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
    c.appendChild(h("p","hint","با اطلاعات تنظیم‌شده در محیط (کاربر: " + st.env_user + ") وارد می‌شوم."));
  } else {
    c.appendChild(h("div","hint","نام کاربری")); u = h("input"); u.type="text"; c.appendChild(u);
    c.appendChild(h("div","hint","رمز عبور (فقط به همین برنامه‌ی محلی می‌رسد)")); p = h("input"); p.type="password"; c.appendChild(p);
  }
  if(st.need_dataset){
    c.appendChild(h("div","hint","مسیر پوشه‌ی دیتاست کپچا")); d = h("input"); d.type="text"; c.appendChild(d);
  }
  var b = h("button","","ورود"); c.appendChild(b); v.appendChild(c);
  b.onclick = function(){
    showErr(""); b.disabled = true;
    api("POST","/api/login", {useenv: !!st.env_user, username: u?u.value:"", password: p?p.value:"", dataset: d?d.value:""}, function(j){
      if(j.state==="working") startPoll(); else { showErr(j.message); b.disabled=false; }
    });
  };
}

function renderHome(st){
  setSteps(2);
  var v = document.getElementById("view"); v.innerHTML = "";
  var c = h("div","card"); c.appendChild(h("div","","چه کاری انجام شود؟"));
  c.appendChild(radio("mode","reserve","مشاهده‌ی منو، انتخاب غذا و ثبت رزرو", st.defaults.mode==="reserve"));
  c.appendChild(radio("mode","file","فقط دریافت منو و ساخت فایل", st.defaults.mode==="file"));
  if(st.has_pending) c.appendChild(radio("mode","pending","ادامه‌ی انتخاب‌های ذخیره‌شده (بعد از شارژ موجودی)", st.defaults.mode==="pending"));
  var weekBox = h("div"); weekBox.appendChild(h("div","","کدام هفته؟"));
  weekBox.appendChild(radio("week","current","هفته جاری", st.defaults.week==="current"));
  weekBox.appendChild(radio("week","next","هفته بعد (برای رزرو)", st.defaults.week==="next"));
  c.appendChild(weekBox);
  var fileBox = h("div"); fileBox.appendChild(h("div","hint","نام فایل (پسوند: .html یا .json یا .txt)"));
  var fn = h("input"); fn.type="text"; fn.value="menu.html"; fileBox.appendChild(fn); c.appendChild(fileBox);
  function sync(){ var m = val("mode"); weekBox.style.display = m==="pending" ? "none" : "block"; fileBox.style.display = m==="file" ? "block" : "none"; }
  var rs = document.getElementsByName("mode"); for(var i=0;i<rs.length;i++) rs[i].onchange = sync; sync();
  var b = h("button","","ادامه"); c.appendChild(b); v.appendChild(c);
  if(st.file){
    var ok = h("div","ok"); ok.appendChild(document.createTextNode("ذخیره شد: " + st.file.path + "  "));
    var a = h("a","","باز کردن در مرورگر"); a.href = "/file?t=" + encodeURIComponent(T); a.target = "_blank"; ok.appendChild(a); v.appendChild(ok);
  }
  b.onclick = function(){
    showErr(""); b.disabled = true;
    api("POST","/api/start",{mode: val("mode"), week: val("week"), filename: fn.value}, function(j){
      if(j.state==="working") startPoll(); else { showErr(j.message); b.disabled=false; }
    });
  };
}

function render(st){
  S = st;
  showErr(st.error);
  var lg = document.getElementById("log");
  lg.textContent = (st.log||[]).join("\n") + (st.progress ? ("\n" + st.progress) : "");
  if(st.stage==="select"){ location.reload(); return; }
  if(st.job==="working"){ setSteps(st.stage==="login" ? 1 : 3); document.getElementById("view").innerHTML = ""; return; }
  if(st.stage==="login") renderLogin(st); else renderHome(st);
}

function startPoll(){
  if(poll) clearInterval(poll);
  poll = setInterval(function(){
    api("GET","/api/status",null,function(st){
      render(st);
      if(st.job!=="working" || st.stage==="select"){ clearInterval(poll); }
    });
  }, 1000);
}

api("GET","/api/status",null,function(st){ render(st); if(st.job==="working") startPoll(); });
</script></body></html>
"""
