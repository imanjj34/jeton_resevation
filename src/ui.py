"""
ui.py - زبان بصری مشترکِ صفحه‌های برنامه (ویزارد، صفحه‌ی انتخاب غذا، فایل منو).

جهت طراحی: «کاشی و زعفران». مرکب نیلی روی کاغذِ سرد، فیروزه‌ای کاشی‌کاری یزد برای عمل‌ها،
زعفرانی فقط برای «انتخاب تو»، سبز برای «رزرو شده»، آجری فقط برای لغو و کمبود.
رنگ فقط معنا می‌رساند؛ تنها لحظه‌ی پررنگ صفحه، «ترازنامه‌ی موجودی» در نوار پایین است.
هیچ فونت یا فایل بیرونی لازم نیست (Vazirmatn اگر نصب باشد، وگرنه فونت سیستم).
"""

# پیش از رنگ‌آمیزی صفحه تم را می‌گذارد تا چشمک نزند (کوکی برای همه‌ی پورت‌های localhost مشترک است)
THEME_BOOT = """<script>(function(){try{var m=document.cookie.match(/(?:^|; )jt_theme=(light|dark)/),t=m?m[1]:(localStorage.getItem("jt_theme")||"");if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.setAttribute("data-theme",t)}catch(e){}})();</script>"""

THEME_TOGGLE_JS = """function toggleTheme(){var r=document.documentElement,t=r.getAttribute("data-theme")==="dark"?"light":"dark";r.setAttribute("data-theme",t);try{localStorage.setItem("jt_theme",t)}catch(e){}document.cookie="jt_theme="+t+"; path=/; max-age=31536000; samesite=lax"}"""

THEME_BUTTON = (
    '<button id="theme" class="icon" type="button" aria-label="تغییر تم" title="تغییر تم" onclick="toggleTheme()">'
    '<svg class="i-moon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
    '<svg class="i-sun" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    "</button>"
)

BASE_CSS = """
:root{
  --paper:#f1f4f7; --surface:#ffffff; --sunken:#e8edf2; --ink:#14213d; --muted:#586780; --line:#d3dbe5;
  --tile:#0b7285; --tile-ink:#ffffff; --tile-soft:#dcf1f4;
  --leaf:#23774b; --leaf-soft:#e0f3e8;
  --saffron:#c98a00; --saffron-soft:#fff2c9;
  --brick:#b3362f; --brick-soft:#fde8e6;
  --font:"Vazirmatn","Vazir","IRANSansX","IRANSans","Noto Sans Arabic","Segoe UI",Tahoma,sans-serif;
  --r:8px;
}
[data-theme="dark"]{
  --paper:#0d1526; --surface:#152038; --sunken:#0a1120; --ink:#e8edf6; --muted:#98a6bf; --line:#2a3858;
  --tile:#4cc3d4; --tile-ink:#05222a; --tile-soft:#123741;
  --leaf:#63d492; --leaf-soft:#163a29;
  --saffron:#f2b632; --saffron-soft:#40320f;
  --brick:#ff918a; --brick-soft:#4b201e;
}
*{box-sizing:border-box}
html{color-scheme:light dark}
[data-theme="light"]{color-scheme:light}[data-theme="dark"]{color-scheme:dark}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.75;
  -webkit-font-smoothing:antialiased}
h1,h2,h3,p{margin:0}
bdi,.num,input[inputmode]{font-variant-numeric:tabular-nums}
button,input{font-family:inherit;font-size:inherit;color:inherit}
:focus-visible{outline:3px solid var(--tile);outline-offset:2px;border-radius:4px}
.vh{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}

/* دکمه‌ها: سه سطح و یک خطرناک */
.btn{border:1px solid transparent;border-radius:var(--r);padding:8px 18px;font-weight:700;cursor:pointer;line-height:1.5;
  transition:background .15s,border-color .15s,color .15s}
.btn.primary{background:var(--tile);color:var(--tile-ink)}
.btn.primary:hover:not(:disabled){filter:brightness(1.08)}
.btn.secondary{background:var(--tile-soft);color:var(--tile);border-color:color-mix(in srgb,var(--tile) 35%,transparent)}
.btn.ghost{background:transparent;color:var(--muted);border-color:var(--line)}
.btn.ghost:hover:not(:disabled){color:var(--ink);border-color:var(--muted)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.icon{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:var(--r);border:1px solid var(--line);
  background:transparent;color:var(--muted);cursor:pointer}
.icon:hover{color:var(--ink);border-color:var(--muted)}
.i-sun{display:none}[data-theme="dark"] .i-sun{display:block}[data-theme="dark"] .i-moon{display:none}

/* ورودی‌ها */
input[type=text],input[type=password],input[inputmode]{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r);padding:8px 10px;width:100%}
input[type=text]:focus,input[type=password]:focus,input[inputmode]:focus{border-color:var(--tile)}
input[type=radio]{accent-color:var(--tile);margin:0}

/* پیام‌ها */
.alert{border:1px solid var(--line);border-inline-start-width:4px;border-radius:var(--r);padding:10px 14px;background:var(--surface)}
.alert.info{border-inline-start-color:var(--saffron)}
.alert.ok{border-inline-start-color:var(--leaf);background:var(--leaf-soft)}
.alert.err{border-inline-start-color:var(--brick);background:var(--brick-soft)}
.alert pre{margin:0;font:inherit;white-space:pre-wrap}
.alert details{margin-top:6px;color:var(--muted)}
.alert summary{cursor:pointer}

/* نوار پیشرفت (به‌جای کسر) */
.progress{height:8px;border-radius:99px;background:var(--sunken);overflow:hidden;margin-top:8px}
.progress>i{display:block;height:100%;background:var(--tile);border-radius:99px;transition:width .3s ease}
.progress.ind>i{width:35%;animation:slide 1.3s ease-in-out infinite alternate}
@keyframes slide{from{margin-inline-start:0}to{margin-inline-start:65%}}
@media (prefers-reduced-motion:reduce){.progress>i{transition:none}.progress.ind>i{animation:none;width:100%;opacity:.5}}
"""

# شبکه‌ی «روز × سلف» و سربرگ وعده‌ها؛ هم در صفحه‌ی انتخاب و هم در فایل منو استفاده می‌شود
GRID_CSS = """
.wrap{padding:0 clamp(12px,2.2vw,32px)}
header.top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding:22px 0 12px}
.eyebrow{display:block;color:var(--muted);font-size:13px}
h1{font-size:28px;line-height:1.3;font-weight:700}
.tools{display:flex;gap:8px;align-items:center}
#tabs{display:flex;gap:2px;position:sticky;top:0;z-index:6;background:var(--paper);border-bottom:1px solid var(--line)}
.tabbtn{background:none;border:0;border-bottom:3px solid transparent;margin-bottom:-1px;padding:10px 22px;font-weight:700;
  color:var(--muted);cursor:pointer}
.tabbtn:hover{color:var(--ink)}
.tabbtn.on{color:var(--ink);border-bottom-color:var(--tile)}
.tabbtn .n{display:inline-block;margin-inline-start:8px;min-width:20px;padding:0 6px;border-radius:99px;background:var(--saffron);
  color:#1b1500;font-size:12px;line-height:20px;text-align:center}
.gridwrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px;margin-top:14px}
table.grid{border-collapse:separate;border-spacing:0;min-width:100%;width:max-content}
.grid th,.grid td{padding:10px 12px;text-align:right;vertical-align:top;border-bottom:1px solid var(--line);
  border-inline-start:1px solid var(--line)}
.grid tbody tr:last-child td{border-bottom:0}
.grid th{background:var(--sunken);font-weight:700;white-space:nowrap}
th.dayh,td.day{position:sticky;right:0;z-index:2;min-width:116px;border-inline-start:0;background:var(--surface)}
th.dayh{background:var(--sunken)}
td.day strong{display:block;line-height:1.5}
.dt{display:block;color:var(--muted);font-size:13px}
tr.res td.day{box-shadow:inset -4px 0 0 var(--leaf)}
.badge{display:inline-block;margin-top:2px;padding:0 8px;border-radius:99px;background:var(--leaf-soft);color:var(--leaf);
  font-size:12px;font-weight:700}
td.cell{min-width:176px}
td.cell.empty{color:var(--line);text-align:center}
.pr{color:var(--muted);font-size:12px}
.tick{color:var(--leaf);font-weight:700;margin-inline-start:4px}
tr.off td{background:var(--sunken);color:var(--muted)}
td.msg{color:var(--muted)}
th.cancelh{text-align:center}
@media (max-width:640px){h1{font-size:22px}.tabbtn{padding:10px 14px}}
"""

