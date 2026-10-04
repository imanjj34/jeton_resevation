"""
web_select.py - انتخاب غذا داخل یک صفحه‌ی HTML و اعمال آن روی سایت اصلی.

برنامه یک سرور محلی (فقط 127.0.0.1) باز می‌کند و صفحه را در مرورگر نشان می‌دهد. هر وعده در هر روز
یک رادیوباتن دارد، پس حداکثر یک غذا انتخاب می‌شود. بعد از انتخاب همه‌ی غذاها، دکمه‌ی «ثبت نهایی»
انتخاب‌ها را به همین برنامه می‌فرستد و برنامه آن‌ها را روی سایت اصلی اعمال می‌کند.

    srv = SelectionServer(session, menu, relogin)
    srv.run()          # صفحه را باز می‌کند و تا پایان کار (یا Ctrl+C) منتظر می‌ماند
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from food_menu import WeekMenu
from reserve import (Choice, _is_noop, clear_pending, current_state, find_day, find_meal, price_delta,
                     save_pending, submit, summary_lines, wait_and_submit)


# ----------------------------------------------------------------- داده‌ی صفحه
def build_data(menu: WeekMenu) -> dict:
    meals = []
    for m in menu.meals:
        days = []
        for d in m.days:
            cur = d.selected_food if d.reserved else None
            days.append({
                "date": d.date, "weekday": d.weekday, "active": d.active, "locked": d.locked,
                "reserved": d.reserved, "amount": d.amount or 0, "curFood": cur.name if cur else None,
                "selves": [{"id": s.id, "name": s.name,
                            "foods": [{"id": f.id, "name": f.name, "price": f.price or 0,
                                       "cur": bool(d.reserved and s.selected and f.selected)} for f in s.foods]}
                           for s in d.selves if s.foods],
            })
        meals.append({"key": m.key, "title": m.title, "days": days})
    return {"week": menu.week_range, "credit": menu.credit or 0, "meals": meals}


def selections_to_choices(menu: WeekMenu, selections: list[dict]) -> list[Choice]:
    """انتخاب‌های صفحه -> Choice. نام و قیمت فقط از داده‌ی خود برنامه گرفته می‌شود، نه از صفحه."""
    choices = []
    seen = set()
    for sel in selections:
        key = (sel.get("meal"), sel.get("date"))
        if key in seen:
            raise ValueError("برای یک وعده در یک روز بیش از یک انتخاب فرستاده شد")
        seen.add(key)
        day, meal = find_day(menu, *key), find_meal(menu, key[0])
        if day is None or meal is None or not day.active or day.locked:
            raise ValueError(f"{key[1]} / {key[0]} قابل ویرایش نیست")
        value = str(sel.get("value", "none"))
        if value == "cur":
            continue
        if value == "none":
            if day.reserved:
                choices.append(Choice(day.date, meal.key, False, meal_title=meal.title, weekday=day.weekday))
            continue
        self_id, _, food_id = value.partition("|")
        s = next((x for x in day.selves if x.id == self_id), None)
        f = next((x for x in (s.foods if s else []) if x.id == food_id), None)
        if f is None:
            raise ValueError(f"گزینه‌ی انتخاب‌شده برای {day.date} / {meal.key} معتبر نیست")
        ch = Choice(day.date, meal.key, True, s.id, f.id, meal.title, day.weekday, s.name, f.name, f.price or 0)
        if not _is_noop(day, ch):
            choices.append(ch)
    return choices


# ------------------------------------------------------------------ سرور
class SelectionServer:
    def __init__(self, session, menu: WeekMenu, relogin, default_wait_min: int = 10):
        self.session, self.menu, self._relogin = session, menu, relogin
        self.default_wait_min = default_wait_min
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.job = {"job": "idle", "log": [], "results": None, "ok": None}
        self.httpd: ThreadingHTTPServer | None = None

    # -- کمکی‌ها
    def relogin(self):
        self.session = self._relogin()
        return self.session

    def _log(self, text: str) -> None:
        print(text)
        self.job["log"] = (self.job["log"] + [text])[-300:]

    def _with_relogin(self, fn):
        try:
            return fn()
        except PermissionError:
            self.relogin()
            return fn()

    def _finish_soon(self) -> None:
        if self.httpd:
            threading.Timer(4.0, self.httpd.shutdown).start()

    # -- API
    def api_submit(self, body: dict) -> dict:
        if self.job["job"] in ("waiting", "submitting"):
            return {"state": "error", "message": "یک عملیات در حال انجام است؛ صبر کن تمام شود (یا انتظار را لغو کن)."}
        wait_min = float(body.get("wait") or 0)
        with self.lock:
            choices = selections_to_choices(self.menu, body.get("selections", []))
            if not choices:
                return {"state": "nochange"}
            fresh = self._with_relogin(lambda: current_state(self.session, choices))
            delta, credit = price_delta(fresh, choices), fresh.credit or 0
            base = {"lines": summary_lines(fresh, choices), "delta": delta, "credit": credit}

            if delta <= credit:
                self.job.update(job="submitting", log=[], results=None, ok=None)
                threading.Thread(target=self._submit_job, args=(choices,), daemon=True).start()
                return {"state": "started", **base}

            save_pending(choices)
            waiting = wait_min > 0
            if waiting:
                self.stop.clear()
                self.job.update(job="waiting", log=[], results=None, ok=None)
                threading.Thread(target=self._wait_job, args=(choices, wait_min), daemon=True).start()
            return {"state": "insufficient", "shortfall": delta - credit, "waiting": waiting, **base}

    def _submit_job(self, choices: list[Choice]) -> None:
        """ثبت مرحله‌به‌مرحله روی سایت؛ پیشرفت زنده از طریق self.job['log'] به صفحه می‌رسد."""
        try:
            results = self._with_relogin(lambda: submit(self.session, choices, on_step=self._log))
        except Exception as e:  # noqa: BLE001
            self._log(f"خطا: {type(e).__name__}: {e}")
            results = [{"meal": "-", "ok": False, "message": None, "problems": [f"{type(e).__name__}: {e}"]}]
        self._finish(results)

    def _finish(self, results) -> None:
        ok = all(r["ok"] for r in results)
        self.job.update(job="finished", results=results, ok=ok)
        if ok:
            clear_pending()
            self._finish_soon()

    def _wait_job(self, choices: list[Choice], minutes: float) -> None:
        results = None
        try:
            results = wait_and_submit(self.session, choices, self.relogin, minutes, log=self._log, stop=self.stop)
        except Exception as e:  # noqa: BLE001 - خطا را به صفحه نشان می‌دهیم
            self._log(f"خطا: {e}")
        if results is None:
            self.job.update(job="idle")
            return
        self._finish(results)

    def api_cancel(self) -> dict:
        self.stop.set()
        self.job.update(job="idle")
        return {"state": "cancelled"}

    # -- HTTP
    def page(self) -> bytes:
        data = json.dumps(build_data(self.menu), ensure_ascii=False).replace("</", "<\\/")
        html = (PAGE.replace("__DATA__", data).replace("__TOKEN__", self.token)
                .replace("__WAIT__", str(self.default_wait_min)))
        return html.encode("utf-8")

    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # بی‌صدا
                pass

            def _ok_host(self) -> bool:
                return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

            def _ok_token(self) -> bool:
                return secrets.compare_digest(self.headers.get("X-Token", ""), srv.token)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self):
                u = urlparse(self.path)
                if not self._ok_host():
                    return self._send(403, b"forbidden", "text/plain")
                if u.path == "/" and secrets.compare_digest(parse_qs(u.query).get("t", [""])[0], srv.token):
                    return self._send(200, srv.page(), "text/html; charset=utf-8")
                if u.path == "/api/status" and self._ok_token():
                    return self._json(srv.job)
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if not (self._ok_host() and self._ok_token()):
                    return self._send(403, b"forbidden", "text/plain")
                try:
                    n = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if self.path == "/api/submit":
                        return self._json(srv.api_submit(body))
                    if self.path == "/api/cancel":
                        return self._json(srv.api_cancel())
                    self._send(404, b"not found", "text/plain")
                except Exception as e:  # noqa: BLE001
                    self._json({"state": "error", "message": f"{type(e).__name__}: {e}"})

        return Handler

    def start(self) -> str:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.token}"

    def run(self, open_browser: bool = True) -> None:
        url = self.start()
        print(f"\nصفحه‌ی انتخاب غذا: {url}")
        if open_browser:
            webbrowser.open(url)
        print("بعد از انتخاب همه‌ی غذاها در صفحه روی «ثبت نهایی» بزن. توقف برنامه: Ctrl+C")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nمتوقف شد.")
        finally:
            self.stop.set()
            self.httpd.server_close()


# ------------------------------------------------------------------ صفحه (ES5 تا همه‌جا کار کند)
PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>انتخاب غذا</title>
<style>
body{font-family:Vazirmatn,Tahoma,sans-serif;max-width:960px;margin:0 auto;padding:12px 12px 230px;color:#222;background:#fff}
h1{font-size:20px;margin:8px 0}h2{font-size:17px;margin:26px 0 8px;border-right:4px solid #2e7d32;padding-right:8px}
table{width:100%;border-collapse:collapse}td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:right}
td.day{white-space:nowrap;font-weight:bold;width:1%;background:#f6f6f6}td.day small{font-weight:normal;color:#666}
.badge{display:inline-block;background:#e8f5e9;color:#1b5e20;border-radius:4px;padding:0 6px;font-size:12px;margin-top:4px}
tr.off td{color:#999;background:#fafafa}tr.locked td.opts{background:#fafafa;color:#555}
.selfname{font-weight:bold;margin:8px 0 2px;color:#444}.selfname:first-child{margin-top:0}
label.opt{display:table;width:100%;box-sizing:border-box;padding:3px 4px;border-radius:5px;cursor:pointer}.rc{display:table-cell;width:26px;vertical-align:top;padding-top:2px}.tc{display:table-cell;vertical-align:top}label.opt:hover{background:#f3f3f3}
label.opt.chosen{background:#fff3cd}label.opt input{margin:0}
.price{color:#777;font-size:12px}.cur{font-weight:bold}.tick{color:#2e7d32;font-weight:bold}
.none{color:#a33}
#bar{position:fixed;right:0;left:0;bottom:0;background:#fff;border-top:2px solid #2e7d32;padding:10px 14px;box-shadow:0 -2px 8px rgba(0,0,0,.12)}
#bar .in{max-width:960px;margin:0 auto}
#nums span{display:inline-block;margin-left:18px}#nums b{font-size:16px}
.bad{color:#c62828}.good{color:#1b5e20}
#chg{margin:6px 0;padding:0 18px;font-size:13px;max-height:64px;overflow:auto;color:#444}
button{font-family:inherit;font-size:15px;padding:8px 18px;border:0;border-radius:6px;background:#2e7d32;color:#fff;cursor:pointer}
button:disabled{background:#aaa;cursor:not-allowed}button.sec{background:#757575}
#waitrow{display:none;margin:6px 0;font-size:14px}#waitrow input{width:60px}
#out{margin:10px 0;padding:10px;border-radius:6px;display:none;white-space:pre-wrap;line-height:1.8}
#out.ok{background:#e8f5e9;border:1px solid #a5d6a7}#out.err{background:#ffebee;border:1px solid #ef9a9a}#out.info{background:#fff8e1;border:1px solid #ffe082}
</style></head><body>
<h1>انتخاب غذا - هفته <bdi id="week"></bdi></h1>
<p style="color:#666;font-size:13px">برای هر وعده در هر روز فقط یک گزینه می‌توانی انتخاب کنی. غذای پررنگ با ✓ سبز، رزرو فعلی توست. هیچ چیز تا زدن «ثبت نهایی» روی سایت اعمال نمی‌شود.</p>
<div id="out"></div>
<div id="meals"></div>
<div id="bar"><div class="in">
 <div id="nums"></div>
 <ul id="chg"></ul>
 <div id="waitrow">موجودی کافی نیست. اگر خواستی هر <input id="wait" type="number" min="0" value="__WAIT__"> دقیقه موجودی چک شود و به محض کافی شدن، همین انتخاب‌ها خودکار رزرو شوند (0 = فقط ذخیره کن).</div>
 <button id="go" disabled>ثبت نهایی در سایت</button>
 <button id="stopwait" class="sec" style="display:none">لغو انتظار</button>
</div></div>
<script>
var DATA = __DATA__;
var TOKEN = "__TOKEN__";
var rows = [];       // {meal, day, name, initial, prices:{value:price}, labels:{value:text}}
var busy = false, poll = null;

function h(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function fmt(n){ return String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function sgn(n){ return (n<0?"-":(n>0?"+":"")) + fmt(n); }

function build(){
  document.getElementById("week").textContent = DATA.week || "";
  var root = document.getElementById("meals");
  DATA.meals.forEach(function(meal){
    root.appendChild(h("h2", "", meal.title));
    var table = h("table"), tb = h("tbody");
    meal.days.forEach(function(day){
      var tr = h("tr"), dayTd = h("td", "day"), opts = h("td", "opts");
      dayTd.appendChild(document.createTextNode(day.weekday));
      dayTd.appendChild(h("br")); dayTd.appendChild(h("small", "", day.date));
      if(day.reserved){ dayTd.appendChild(h("br")); dayTd.appendChild(h("span", "badge", "رزرو شده")); }
      tr.appendChild(dayTd); tr.appendChild(opts); tb.appendChild(tr);
      if(!day.active){ tr.className="off"; opts.textContent="—"; return; }
      if(day.locked){
        tr.className="locked";
        opts.textContent = day.reserved ? ("قفل‌شده - رزرو: " + (day.curFood||"")) : "قفل‌شده";
        return;
      }
      var name = meal.key + "|" + day.date;
      var info = {meal: meal, day: day, name: name, initial: day.reserved ? "cur" : "none", prices: {}, labels: {}, tr: tr};
      var hasCur = false;
      function radio(value, labelNode, checked, cls){
        var l = h("label", "opt " + (cls||"")), i = h("input");
        i.type="radio"; i.name=name; i.value=value; i.checked=checked;
        i.onchange = refresh;
        var rc=h("span","rc"), tc=h("span","tc"); rc.appendChild(i); tc.appendChild(labelNode); l.appendChild(rc); l.appendChild(tc); opts.appendChild(l); return l;
      }
      // اول گزینه‌ی «رزرو نشود»، بعد غذاهای هر سلف
      var noneLabel = h("span", "none", day.reserved ? "لغو رزرو (رزرو نشود)" : "رزرو نشود");
      day.selves.forEach(function(s){ s.foods.forEach(function(f){ if(f.cur) hasCur = true; }); });
      info.labels["none"] = day.reserved ? "لغو رزرو" : "رزرو نشود";
      radio("none", noneLabel, !day.reserved, "");
      if(day.reserved && !hasCur){
        info.prices["cur"] = day.amount;
        radio("cur", h("span", "cur", "رزرو فعلی بدون تغییر: " + (day.curFood||"")), true, "");
      }
      day.selves.forEach(function(s){
        opts.appendChild(h("div", "selfname", s.name));
        s.foods.forEach(function(f){
          var value = s.id + "|" + f.id, span = h("span");
          span.appendChild(document.createTextNode("- " + f.name + " "));
          span.appendChild(h("span", "price", fmt(f.price)));
          if(f.cur){ span.className="cur"; span.appendChild(document.createTextNode(" ")); span.appendChild(h("span","tick","✓")); info.initial = value; }
          info.prices[value] = f.price; info.labels[value] = s.name + " / " + f.name;
          radio(value, span, f.cur, "");
        });
      });
      rows.push(info);
    });
    table.appendChild(tb); root.appendChild(table);
  });
}

function current(info){
  var els = document.getElementsByName(info.name);
  for(var i=0;i<els.length;i++) if(els[i].checked) return els[i].value;
  return info.initial;
}

function calc(){
  var delta=0, list=[];
  rows.forEach(function(r){
    var v = current(r), old = r.day.reserved ? r.day.amount : 0, nw;
    var labels = r.tr.getElementsByTagName("label");
    for(var i=0;i<labels.length;i++) labels[i].className = "opt" + (labels[i].getElementsByTagName("input")[0].checked && v!==r.initial ? " chosen" : "");
    if(v===r.initial) return;
    nw = (v==="none") ? 0 : r.prices[v];
    delta += nw - old;
    list.push(r.day.weekday + " " + r.day.date + " - " + r.meal.title + ": " + (v==="none" ? "لغو رزرو" : r.labels[v]));
  });
  return {delta: delta, list: list};
}

function refresh(){
  var c = calc(), credit = DATA.credit, after = credit - c.delta, short = c.delta - credit;
  var nums = document.getElementById("nums");
  nums.innerHTML = "";
  function item(label, val, cls){ var s=h("span"); s.appendChild(document.createTextNode(label+": ")); var b=h("b",cls||"",val); s.appendChild(b); nums.appendChild(s); }
  item("جمع تغییر", sgn(c.delta) + " ریال", "");
  item("موجودی فعلی", fmt(credit) + " ریال", "");
  item("پس از ثبت", (after<0?"-":"") + fmt(after) + " ریال", after<0 ? "bad":"good");
  if(short>0) item("کمبود", fmt(short) + " ریال", "bad");
  var ul = document.getElementById("chg"); ul.innerHTML="";
  c.list.forEach(function(t){ ul.appendChild(h("li","",t)); });
  document.getElementById("waitrow").style.display = short>0 ? "block" : "none";
  document.getElementById("go").disabled = busy || c.list.length===0;
}

function api(method, path, body, cb){
  var x = new XMLHttpRequest();
  x.open(method, path); x.setRequestHeader("X-Token", TOKEN); x.setRequestHeader("Content-Type", "application/json");
  x.onload = function(){ var j; try{ j=JSON.parse(x.responseText); }catch(e){ j={state:"error", message:"پاسخ نامعتبر از برنامه"}; } cb(j); };
  x.onerror = function(){ cb({state:"error", message:"ارتباط با برنامه قطع شد (برنامه بسته شده؟)"}); };
  x.send(body ? JSON.stringify(body) : null);
}

function show(kind, text){ var o=document.getElementById("out"); o.className=kind; o.textContent=text; o.style.display="block"; window.scrollTo(0,0); }

function resultsText(res){
  var t=[];
  res.forEach(function(r){
    t.push((r.ok?"✓ ":"✗ ") + r.meal + (r.message ? " - پیام سایت: "+r.message : ""));
    (r.problems||[]).forEach(function(p){ t.push("    ! "+p); });
    if(r.credit!=null) t.push("    موجودی پس از ذخیره: " + fmt(r.credit) + " ریال");
  });
  return t.join("\n");
}

function setBusy(b){ busy=b; document.getElementById("go").textContent = b ? "در حال کار روی سایت ..." : "ثبت نهایی در سایت"; refresh(); }

function startPoll(){
  if(poll) clearInterval(poll);
  poll = setInterval(function(){
    api("GET", "/api/status", null, function(j){
      var log = (j.log||[]).join("\n");
      if(j.job==="submitting"){ show("info", "در حال اعمال تغییرات روی سایت (مرحله به مرحله) ...\n\n" + log); }
      else if(j.job==="waiting"){
        document.getElementById("stopwait").style.display="inline-block";
        show("info", "در انتظار شارژ موجودی (یا در حال ثبت) ...\n\n" + log);
      }
      else if(j.job==="finished"){
        clearInterval(poll); document.getElementById("stopwait").style.display="none";
        show(j.ok?"ok":"err", (j.ok ? "همه‌ی تغییرات ثبت و تأیید شد. این صفحه و برنامه تا چند ثانیه‌ی دیگر بسته می‌شوند.\n" : "بعضی موارد ثبت نشد:\n") + resultsText(j.results||[]) + "\n\nمراحل انجام‌شده:\n" + log);
        setBusy(false);
      } else if(j.job==="idle"){ clearInterval(poll); document.getElementById("stopwait").style.display="none"; setBusy(false); }
    });
  }, 1500);
}

document.getElementById("go").onclick = function(){
  var c = calc();
  if(!confirm("این تغییرات روی سایت اصلی اعمال می‌شود:\n\n" + c.list.join("\n") + "\n\nجمع تغییر اعتبار: " + sgn(c.delta) + " ریال\nادامه می‌دهی؟")) return;
  var sels = rows.map(function(r){ return {meal: r.meal.key, date: r.day.date, value: current(r)}; });
  setBusy(true);
  api("POST", "/api/submit", {selections: sels, wait: parseFloat(document.getElementById("wait").value)||0}, function(j){
    if(j.state==="started"){
      show("info", "شروع شد ...");
      startPoll();
    } else if(j.state==="insufficient"){
      show("info", "موجودی کافی نیست؛ کمبود: " + fmt(j.shortfall) + " ریال.\nانتخاب‌ها در فایل pending.json ذخیره شد." + (j.waiting ? "\nمنتظر شارژ می‌مانم و به محض کافی شدن، همین غذاها را رزرو می‌کنم. این صفحه و برنامه را باز نگه دار." : "\nبعداً از گزینه‌ی «ادامه‌ی انتخاب‌های ذخیره‌شده» در برنامه ادامه بده."));
      if(j.waiting) startPoll(); else setBusy(false);
    } else if(j.state==="nochange"){ show("info", "تغییری انتخاب نشده است."); setBusy(false); }
    else { show("err", j.message || "خطا"); setBusy(false); }
  });
};
document.getElementById("stopwait").onclick = function(){
  api("POST", "/api/cancel", {}, function(){ if(poll) clearInterval(poll); document.getElementById("stopwait").style.display="none"; show("info","انتظار لغو شد. انتخاب‌ها در pending.json مانده است."); setBusy(false); });
};

build(); refresh();
</script></body></html>
"""
