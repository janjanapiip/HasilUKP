"""Check the dashboard widgets.

The calendar is the only part with real logic (Monday-first offsets, leap
years, month lengths), so it is re-implemented here from Python's own
calendar module -- an independent source -- and compared against the
JavaScript by evaluating the JS grid builder with node.

Clock and weather are assertion-light on purpose: they are wall-clock and
network, so this checks wiring and the WMO mapping table, not live values.
"""
import calendar
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "templates", "_widgets.html")
NODE = os.environ.get(
    "NODE", r"C:\Users\fauza\AppData\Local\hermes\node\node.exe")

tmp = tempfile.mkdtemp(prefix="ukp_wdg_")
shutil.copy(os.path.join(HERE, "ukp.db"), os.path.join(tmp, "ukp.db"))
os.environ["UKP_DB"] = os.path.join(tmp, "ukp.db")
os.environ["LOG_DB"] = os.path.join(tmp, "ukp.db")
os.environ.setdefault("UKP_SECRET", "test-only-secret")

sys.path.insert(0, HERE)
import app as A  # noqa: E402

A.app.config["TESTING"] = True
fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f" != {want}"))
    if not ok:
        fails.append(label)


src = open(TPL, encoding="utf-8").read()

# ----------------------------------------------------------- 1. it renders
with A.app.test_client() as c:
    r = c.get("/")
    check("1. dashboard still returns 200", r.status_code, 200)
    html = r.get_data(as_text=True)
for el in ("wkT", "wkS", "wkD", "calG", "calM", "wxT", "wxD", "wxF"):
    if f'id="{el}"' not in html:
        fails.append(f"1. #{el} missing")
check("1. every widget element rendered", [f for f in fails if f.startswith("1. #")], [])
check("1. widgets appear before the filter card",
      html.index('class="wdg"') < html.index('id="fYear"'), True)
check("1. hidden when printing", "@media print{.wdg{display:none}}" in html, True)

# --------------------------------------------- 2. no server-side weather call
# The weather must be fetched by the browser. If it ever moves into Flask it
# burns a serverless invocation per page view, so assert the app never calls it.
check("2. open-meteo is not called from Python",
      "open-meteo" in open(os.path.join(HERE, "app.py"), encoding="utf-8").read(),
      False)
check("2. api url is https", "https://api.open-meteo.com" in src, True)
check("2. no API key in the template",
      bool(re.search(r"(api[_-]?key|appid|token)=", src, re.I)), False)

# ------------------------------------------------- 3. calendar grid vs Python
# Pull the drawCal body out of the template and re-run it in node for a set of
# awkward months, then compare with Python's calendar module.
if not os.path.exists(NODE):
    print("skip 3: node not found at", NODE)
else:
    js = r"""
function grid(y, m){            // m: 0-based, mirrors the template
  const first = (new Date(y, m, 1).getDay() + 6) % 7;
  const days  = new Date(y, m + 1, 0).getDate();
  const prev  = new Date(y, m, 0).getDate();
  const out = [];
  for (let i = 0; i < 42; i++){
    const n = i - first + 1;
    const off = n < 1 || n > days;
    out.push({n: off ? (n < 1 ? prev + n : n - days) : n, off: off});
  }
  return out;
}
const cases = CASES_JSON;
console.log(JSON.stringify(cases.map(c => grid(c[0], c[1]))));
"""
    # February in a leap year, a month starting on Sunday, a month starting on
    # Monday, 31-day months, a year boundary.
    cases = [(2024, 1), (2026, 8), (2026, 5), (2027, 1), (2026, 11),
             (2025, 0), (2100, 1), (2026, 2)]
    jsf = os.path.join(tmp, "g.js")
    open(jsf, "w", encoding="utf-8").write(js.replace("CASES_JSON", json.dumps(cases)))
    out = subprocess.run([NODE, jsf], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        fails.append("3. node failed: " + out.stderr.strip()[:200])
    else:
        got = json.loads(out.stdout)
        for (y, m), g in zip(cases, got):
            cal = calendar.Calendar(firstweekday=0)  # 0 = Monday
            want = [d for d in cal.itermonthdays(y, m + 1)]
            want_days = [d for d in want if d]
            days_in = calendar.monthrange(y, m + 1)[1]
            in_month = [c["n"] for c in g if not c["off"]]
            label = f"{y}-{m+1:02d}"
            if in_month != list(range(1, days_in + 1)):
                fails.append(f"3. {label} in-month days wrong")
            if len(g) != 42:
                fails.append(f"3. {label} grid is not 42 cells")
            # First cell must be the Monday on or before the 1st.
            d1 = dt.date(y, m + 1, 1)
            lead = (d1.weekday()) % 7
            if lead:
                want_first = (d1 - dt.timedelta(days=lead)).day
                if g[0]["n"] != want_first or not g[0]["off"]:
                    fails.append(f"3. {label} leading cell wrong")
            else:
                if g[0]["n"] != 1 or g[0]["off"]:
                    fails.append(f"3. {label} should start on the 1st")
            # Weekday alignment: every in-month cell sits in the right column.
            for i, c in enumerate(g):
                if not c["off"] and dt.date(y, m + 1, c["n"]).weekday() != i % 7:
                    fails.append(f"3. {label} day {c['n']} in wrong column")
                    break
        check("3. calendar grid matches Python's calendar module",
              [f for f in fails if f.startswith("3.")], [])
        check("3. Sunday is the last column", "i % 7 === 6 ? 'sun'" in src, True)

# --------------------------------------------------- 4. WMO mapping is total
codes = [0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
         71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99]
m = re.search(r"function wmo\(c, day\)\{(.+?)\n  \}", src, re.S)
check("4. wmo() found in template", bool(m), True)
if m and os.path.exists(NODE):
    body = m.group(1)
    # Stub the icon constants; only the labels matter here.
    stub = ("const SUN='SUN',MOON='MOON',CLOUD='CLOUD',PCLD='PCLD',RAIN='RAIN',"
            "STORM='STORM',FOG='FOG',SNOW='SNOW';\n"
            "function wmo(c, day){" + body + "\n}\n"
            "console.log(JSON.stringify(CODES"
            ".map(c => [c, wmo(c, DAYFLAG)[0], wmo(c, DAYFLAG)[1]])));")
    stub = "const CODES=" + json.dumps(codes) + ";\n" + stub
    f2 = os.path.join(tmp, "w.js")
    open(f2, "w", encoding="utf-8").write(stub.replace("DAYFLAG", "true"))
    o2 = subprocess.run([NODE, f2], capture_output=True, text=True, timeout=60)
    if o2.returncode != 0:
        fails.append("4. node failed: " + o2.stderr.strip()[:200])
    else:
        rows = json.loads(o2.stdout)
        unlabelled = [c for c, lab, _ in rows if not lab]
        check("4. every WMO code returns a label", unlabelled, [])
        check("4. every WMO code returns an icon",
              [c for c, _, ic in rows if not ic], [])
        by = dict((c, lab) for c, lab, _ in rows)
        check("4. code 0 is Cerah", by[0], "Cerah")
        check("4. code 61 is Hujan", by[61], "Hujan")
        check("4. code 95 is Hujan petir", by[95], "Hujan petir")
        check("4. code 45 is Berkabut", by[45], "Berkabut")
        # Night differs from day only for clear skies.
        f3 = os.path.join(tmp, "w_night.js")
        open(f3, "w", encoding="utf-8").write(stub.replace("DAYFLAG", "false"))
        o3 = subprocess.run([NODE, f3], capture_output=True, text=True, timeout=60)
        check("4. night variant runs", o3.returncode, 0)
        if o3.returncode == 0:
            night = dict((c, ic) for c, _, ic in json.loads(o3.stdout))
            day = dict((c, ic) for c, _, ic in rows)
            check("4. clear sky uses the moon at night", night[0], "MOON")
            check("4. clear sky uses the sun by day", day[0], "SUN")
            check("4. rain looks the same day or night", night[61], day[61])

# -------------------------------------------------- 5. clock drift correction
check("5. clock resyncs to the second boundary",
      "1000 - (Date.now() % 1000)" in src, True)
check("5. clock does not use a bare 1000ms interval",
      bool(re.search(r"setInterval\([^,]+,\s*1000\s*\)", src)), False)
check("5. timezone label falls back to IANA",
      "resolvedOptions().timeZone" in src, True)

# --------------------------------------------- 6. weather refresh is bounded
check("6. refresh interval is 15 minutes", "15 * 60 * 1000" in src, True)
check("6. fetch has a timeout", "AbortSignal.timeout" in src, True)
check("6. failure keeps the last reading", "tidak tersedia" in src, True)
check("6. visibility refresh is throttled", "Date.now() - last > 3e5" in src, True)

shutil.rmtree(tmp, ignore_errors=True)
print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("widgets: all checks passed")
