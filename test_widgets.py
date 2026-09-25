"""Check the dashboard widgets.

The previous version of this test re-implemented the calendar grid in its own
JS string, so it passed while the real page threw a ReferenceError on the
first tick and rendered nothing at all. So the first thing checked here is
that the template's own script RUNS: the whole IIFE is executed under node
against a minimal DOM stub, and any throw -- or anything the per-widget
try/catch swallows -- fails the test.

Beyond that, the calendar is the only part with real logic (Monday-first
offsets, leap years, month lengths, holiday lookup), so its grid is
re-derived from Python's own calendar module and the holiday marks are
re-read from static/libur.json, not from whatever the template inlined.
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
LIBUR_JSON = os.path.join(HERE, "static", "libur.json")
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
    shown = got if len(repr(got)) < 120 else repr(got)[:117] + "..."
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {shown}"
          + ("" if ok else f" != {want}"))
    if not ok:
        fails.append(label)


def run_node(src, name):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    return subprocess.run([NODE, path], capture_output=True, text=True, timeout=90)


def tail_json(stdout):
    lines = [l for l in stdout.splitlines() if l.startswith("@@")]
    return json.loads(lines[-1][2:]) if lines else None


src = open(TPL, encoding="utf-8").read()
libur_all = json.load(open(LIBUR_JSON, encoding="utf-8"))
LIBUR = {k: v for k, v in libur_all.items() if not k.startswith("_")}

# The widget script is the LAST <script> block; the first only assigns
# window.LIBUR from the Jinja variable.
blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
check("0. two script blocks", len(blocks), 2)
widget_js = blocks[-1]

# ----------------------------------------------------------- 1. it renders
with A.app.test_client() as c:
    r = c.get("/")
    check("1. dashboard still returns 200", r.status_code, 200)
    html = r.get_data(as_text=True)
for el in ("wkT", "wkS", "wkD", "wkH", "calG", "calM", "calN", "wxT", "wxD", "wxF"):
    if f'id="{el}"' not in html:
        fails.append(f"1. #{el} missing")
check("1. every widget element rendered",
      [f for f in fails if f.startswith("1. #")], [])
check("1. widgets appear before the filter card",
      html.index('class="wdg"') < html.index('id="fYear"'), True)
check("1. hidden when printing", "@media print{.wdg{display:none}}" in html, True)
# The holiday table must arrive inlined, not fetched.
check("1. holiday table inlined into the page",
      "Proklamasi Kemerdekaan" in html, True)
check("1. inlined table parses as JSON",
      bool(re.search(r"window\.LIBUR = \{.+\};", html)), True)

# --------------------------------------- 2. the script actually runs
# This is the check the old test lacked. `view` was declared with `let` after
# the clock's first tick read it -- a TDZ ReferenceError that killed the whole
# IIFE, clock and weather included, while every string-matching check passed.
DOM_STUB = r"""
const store = {};
function el(id){
  return store[id] || (store[id] = {
    id, _t:'', _h:'', style:{}, onclick:null,
    set textContent(v){ this._t = String(v); }, get textContent(){ return this._t; },
    set innerHTML(v){ this._h = String(v); },   get innerHTML(){ return this._h; },
  });
}
const ERR = [];
global.document = { getElementById: el, addEventListener: () => {}, hidden: false };
global.window = {};
global.Intl = { DateTimeFormat: () => ({
  resolvedOptions: () => ({ timeZone: 'Asia/Jakarta' }) }) };
// Any network use during the logic checks is a failure, not a skip.
global.fetch = () => Promise.reject(new Error('no network in test'));
const realTimeout = global.setTimeout;
global.setTimeout = () => 0;
global.setInterval = () => 0;
const realError = console.error;
console.error = (...a) => { ERR.push(a.map(String).join(' ')); };
window.LIBUR = LIBUR_JSON;
"""
stub = DOM_STUB.replace("LIBUR_JSON", json.dumps(LIBUR))

probe = stub + widget_js + r"""
// The weather fetch rejects asynchronously, so let microtasks flush before
// reading the cards -- realTimeout is the un-stubbed timer captured above.
realTimeout(function(){
console.log('@@' + JSON.stringify({
  errors: ERR,
  clock: el('wkT').textContent, sec: el('wkS').textContent,
  date: el('wkD').textContent, zone: el('wkZ').textContent,
  hari: el('wkH').textContent,
  month: el('calM').textContent, year: el('calY').textContent,
  cells: (el('calG').innerHTML.match(/<span>\d+<\/span>/g) || []).length,
  heads: (el('calG').innerHTML.match(/<th>(\w+)<\/th>/g) || [])
           .map(s => s.slice(4, -5)),
  note: el('calN').innerHTML,
  wxfail: el('wxU').textContent,
  wxtemp: el('wxT').textContent,
}));
}, 50);
"""
p = run_node(probe, "probe.js")
check("2. template script runs without throwing", p.returncode, 0)
if p.returncode != 0:
    print("      " + p.stderr.strip().splitlines()[0][:200])
    print("\nThe script does not run; later checks would be meaningless.")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)

res = tail_json(p.stdout) or {}
# The only expected console.error is the stubbed fetch rejection.
real_errs = [e for e in res.get("errors", []) if "no network in test" not in e]
check("2. no widget logged an error", real_errs, [])
check("2. clock rendered HH:MM",
      bool(re.match(r"^\d\d:\d\d$", res.get("clock", ""))), True)
check("2. seconds rendered", bool(re.match(r"^\d\d$", res.get("sec", ""))), True)
check("2. zone label resolved",
      res.get("zone") in ("WIB", "WITA", "WIT", "Asia/Jakarta"), True)
check("2. date line is Indonesian",
      bool(re.match(r"^(Minggu|Senin|Selasa|Rabu|Kamis|Jumat|Sabtu), \d{1,2} "
                    r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|"
                    r"September|Oktober|November|Desember) \d{4}$",
                    res.get("date", ""))), True)
check("2. day-type label present", bool(res.get("hari", "").strip()), True)
check("2. calendar drew 42 cells", res.get("cells"), 42)
check("2. weekday header is Monday-first", res.get("heads"),
      ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"])
check("2. holiday note rendered", bool(res.get("note", "").strip()), True)
# A dead weather API must not blank the clock or calendar.
check("2. weather failure is reported, not silent",
      "tidak tersedia" in res.get("wxfail", "").lower(), True)
check("2. cold failure shows no bogus temperature", res.get("wxtemp"), "--")
check("2. clock survived the weather failure",
      bool(re.match(r"^\d\d:\d\d$", res.get("clock", ""))), True)

# --------------------------------------- 3. no server-side weather call
app_py = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
check("3. weather is not fetched from Python",
      bool(re.search(r"bmkg|open-meteo", app_py, re.I)), False)
check("3. app.py loads the holiday table", "_load_libur" in app_py, True)
check("3. index() passes libur to the template", "libur=LIBUR" in app_py, True)
check("3. weather points at BMKG Marunda",
      "api.bmkg.go.id" in widget_js and "31.72.04.1003" in widget_js, True)
check("3. no Open-Meteo left behind", "open-meteo" in src.lower(), False)
check("3. no API key in the template",
      bool(re.search(r"(api[_-]?key|appid|token)=", src, re.I)), False)
check("3. holidays are not fetched at runtime", "libur.json" in widget_js, False)

# --------------------------------------- 4. calendar grid vs Python
# Drive the real drawCal() through the same buttons a user clicks, then compare
# against Python's calendar module and against libur.json.
CASES = [
    (2024, 2),   # leap February
    (2026, 2),   # non-leap February
    (2026, 3),   # 31 days, Idul Fitri + the cuti bersama cluster
    (2026, 11),  # starts on a Sunday
    (2027, 2),   # starts on a Monday
    (2026, 12),  # year boundary, Natal + cuti
    (2027, 1),   # January after that boundary
    (2100, 2),   # century non-leap year
]
harness = stub + widget_js + r"""
const CASES = CASES_JSON;
const outs = [];
for (const [y, m] of CASES){
  el('cNow').onclick();                      // back to today
  const now = new Date();
  let cur = now.getFullYear() * 12 + now.getMonth();
  const want = y * 12 + (m - 1);
  const back = want < cur;
  const btn = back ? el('cPrev') : el('cNext');
  let guard = 0;
  while (cur !== want && guard++ < 4000){ btn.onclick(); cur += back ? -1 : 1; }
  const g = el('calG').innerHTML;
  outs.push({
    y, m, guard,
    month: el('calM').textContent, year: el('calY').textContent,
    cells: (g.match(/<span>(\d+)<\/span>/g) || [])
             .map(s => Number(s.replace(/\D/g, ''))),
    classes: (g.match(/<td class="([^"]*)"/g) || [])
             .map(s => s.slice(11, -1)),
    titles: (g.match(/title="([^"]*)"/g) || []).map(s => s.slice(7, -1)),
    note: el('calN').innerHTML,
  });
}
console.log('@@' + JSON.stringify(outs));
"""
p = run_node(harness.replace("CASES_JSON", json.dumps(CASES)), "cal.js")
check("4. calendar harness runs", p.returncode, 0)
if p.returncode != 0:
    print("      " + p.stderr.strip().splitlines()[0][:200])
grids = tail_json(p.stdout) or []
check("4. one grid per case", len(grids), len(CASES))

BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]

for g in grids:
    y, m = g["y"], g["m"]
    tag = f"{y}-{m:02d}"
    first_wd, ndays = calendar.monthrange(y, m)      # first_wd: 0 = Monday
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    prev_days = calendar.monthrange(py, pm)[1]

    # Independently built expected grid.
    want = []
    for i in range(42):
        n = i - first_wd + 1
        want.append(prev_days + n if n < 1 else n - ndays if n > ndays else n)

    check(f"4.{tag} month label", g["month"], BULAN[m - 1])
    check(f"4.{tag} year label", g["year"], str(y))
    check(f"4.{tag} grid matches Python", g["cells"], want)
    check(f"4.{tag} in-month run is 1..{ndays}",
          g["cells"][first_wd:first_wd + ndays], list(range(1, ndays + 1)))

    off = [i for i, c in enumerate(g["classes"]) if "off" in c.split()]
    check(f"4.{tag} spill-over cells marked off", off,
          list(range(first_wd)) + list(range(first_wd + ndays, 42)))

    bad_col = [d for d in range(1, ndays + 1)
               if dt.date(y, m, d).weekday() != (first_wd + d - 1) % 7]
    check(f"4.{tag} every date in the right column", bad_col, [])

    sun = [i for i, c in enumerate(g["classes"]) if "sun" in c.split()]
    check(f"4.{tag} Sunday is the last column",
          sorted(set(i % 7 for i in sun)), [6])

    # ------- holiday marks, re-derived from libur.json
    ytab = LIBUR.get(str(y), {})
    lib, cut = ytab.get("libur", {}), ytab.get("cuti", {})
    want_lib, want_cut, want_names = [], [], []
    for d in range(1, ndays + 1):
        key = f"{m:02d}-{d:02d}"
        idx = first_wd + d - 1
        if key in lib:
            want_lib.append(idx)
            want_names.append(f"{d} {BULAN[m-1]} — {lib[key]}")
        elif key in cut:
            want_cut.append(idx)
            want_names.append(f"{d} {BULAN[m-1]} — {cut[key]}")
    got_lib = [i for i, c in enumerate(g["classes"])
               if "lib" in c.split() and "off" not in c.split()]
    got_cut = [i for i, c in enumerate(g["classes"])
               if "cut" in c.split() and "off" not in c.split()]
    check(f"4.{tag} libur cells", got_lib, want_lib)
    check(f"4.{tag} cuti cells", got_cut, want_cut)

    note_names = [n.strip() for n in re.findall(r"<b[^>]*>([^<]+)</b>", g["note"])]
    check(f"4.{tag} note lists holidays in date order", note_names, want_names)
    if not want_names:
        check(f"4.{tag} empty month says so", "Tidak ada libur" in g["note"], True)

# --------------------------------------- 5. holiday table sanity
for year, tab in sorted(LIBUR.items()):
    lib, cut = tab.get("libur", {}), tab.get("cuti", {})
    check(f"5.{year} has the full libur list", len(lib) >= 15, True)
    check(f"5.{year} has cuti bersama", len(cut) >= 5, True)
    check(f"5.{year} no date is both libur and cuti",
          sorted(set(lib) & set(cut)), [])
    check(f"5.{year} keys are MM-DD",
          [k for k in list(lib) + list(cut) if not re.match(r"^\d\d-\d\d$", k)], [])
    unreal = []
    for k in list(lib) + list(cut):
        mm, dd = int(k[:2]), int(k[3:])
        if not 1 <= mm <= 12 or not 1 <= dd <= calendar.monthrange(int(year), mm)[1]:
            unreal.append(k)
    check(f"5.{year} every date exists in that year", unreal, [])
    check(f"5.{year} every date is named",
          [k for k, v in list(lib.items()) + list(cut.items()) if not str(v).strip()],
          [])

# Spot-checks hardcoded from the SKB, so a bad edit to libur.json is caught
# rather than silently agreed with.
check("5. 2026 Kemerdekaan", LIBUR["2026"]["libur"].get("08-17"),
      "Proklamasi Kemerdekaan")
check("5. 2026 Idul Fitri spans 21-22 Maret",
      ["03-21" in LIBUR["2026"]["libur"], "03-22" in LIBUR["2026"]["libur"]],
      [True, True])
check("5. 2026 Natal cuti bersama on 24 Des",
      "12-24" in LIBUR["2026"]["cuti"], True)
check("5. 2026 Waisak", LIBUR["2026"]["libur"].get("05-31"),
      "Hari Raya Waisak 2570 BE")
check("5. 2027 Tahun Baru", LIBUR["2027"]["libur"].get("01-01"),
      "Tahun Baru 2027 Masehi")
check("5. 2027 Nyepi is 8 Maret", "03-08" in LIBUR["2027"]["libur"], True)
check("5. 2026 has no 26 Des libur", "12-26" in LIBUR["2026"]["libur"], False)
check("5. 2027 does have 26 Des libur", "12-26" in LIBUR["2027"]["libur"], True)
check("5. metadata records the SKB source",
      any("SKB" in s for s in libur_all.get("_sumber", [])), True)

# --------------------------------------- 6. weather icon mapping
m_icon = re.search(r"function icon\(code, day\)\{.*?\n    \}", widget_js, re.S)
check("6. icon() found in the template", bool(m_icon), True)
m_s = re.search(r"var S = function\(\)\{.*?\n    \};", widget_js, re.S)
check("6. svg helper found", bool(m_s), True)
consts = re.findall(
    r"var (?:SUN|MOON|CLOUD|PCLD|RAIN|STORM|FOG)\s*=\s*S\(.*?\);", widget_js, re.S)
check("6. all seven icons defined", len(consts), 7)

if m_icon and m_s:
    # BMKG's documented codes, not WMO's.
    codes = [0, 1, 2, 3, 4, 5, 10, 45, 60, 61, 63, 80, 95, 97]
    sizes = {}
    for flag, tag in (("true", "day"), ("false", "night")):
        src_js = (m_s.group(0) + "\n" + "\n".join(consts) + "\n"
                  + m_icon.group(0) + "\n"
                  + "const CODES=" + json.dumps(codes) + ";\n"
                  + "console.log('@@' + JSON.stringify(CODES.map(c => {\n"
                  + f"  const s = icon(c, {flag});\n"
                  + "  return [c, s.indexOf('<svg') === 0, s.length];\n"
                  + "})));")
        p = run_node(src_js, f"icon_{tag}.js")
        check(f"6.{tag} icon() runs", p.returncode, 0)
        if p.returncode != 0:
            print("      " + p.stderr.strip().splitlines()[0][:200])
            continue
        rows = tail_json(p.stdout) or []
        check(f"6.{tag} every code returns an svg",
              [c for c, ok, _ in rows if not ok], [])
        check(f"6.{tag} every code covered", len(rows), len(codes))
        sizes[tag] = {c: n for c, _, n in rows}
    if len(sizes) == 2:
        check("6. clear sky differs day vs night",
              sizes["day"][0] != sizes["night"][0], True)
        check("6. overcast is the same day or night",
              sizes["day"][3] == sizes["night"][3], True)
        check("6. rain is the same day or night",
              sizes["day"][61] == sizes["night"][61], True)

# --------------------------------------- 7. resilience wiring
# Each widget guarded separately: calendar, clock tick, clock outer, weather.
check("7. four independent try/catch guards",
      widget_js.count("}catch(e){ console.error"), 4)
check("7. clock resyncs to the second boundary",
      "1000 - (Date.now() % 1000)" in widget_js, True)
check("7. clock does not use a bare 1000ms interval",
      bool(re.search(r"setInterval\([^,]+,\s*1000\s*\)", widget_js)), False)
check("7. timezone falls back to IANA",
      "resolvedOptions().timeZone" in widget_js, True)
check("7. refresh interval is 15 minutes", "15 * 60 * 1000" in widget_js, True)
check("7. fetch has a timeout", "AbortSignal.timeout" in widget_js, True)
check("7. visibility refresh is throttled",
      "Date.now() - last > 3e5" in widget_js, True)
# Month stepping must pin to the 1st: from 31 Jan, setMonth(1) lands in March.
check("7. month navigation pins to the 1st",
      widget_js.count("view.setDate(1)"), 2)
check("7. holiday names are escaped before injection",
      "esc(h.n)" in widget_js, True)

# --------------------------------------- 8. weather card vs a real BMKG payload
# A captured live response, so the parsing is proven against BMKG's actual
# shape (3-hourly slots nested one level deep) rather than a hand-made fixture.
FIXTURE = os.path.join(HERE, "tests", "fixtures", "bmkg_marunda.json")
check("8. BMKG fixture present", os.path.exists(FIXTURE), True)
if os.path.exists(FIXTURE):
    payload = open(FIXTURE, encoding="utf-8").read()
    bm = json.loads(payload)
    check("8. fixture is the Marunda kelurahan", bm["lokasi"]["desa"], "Marunda")

    wx_stub = DOM_STUB.replace("LIBUR_JSON", "{}").replace(
        "global.fetch = () => Promise.reject(new Error('no network in test'));",
        "const PAYLOAD = " + payload + ";\n"
        "global.fetch = () => Promise.resolve({ ok: true, status: 200,\n"
        "  json: () => Promise.resolve(PAYLOAD) });")
    wx_probe = wx_stub + widget_js + r"""
realTimeout(function(){
  console.log('@@' + JSON.stringify({
    errors: ERR,
    temp: el('wxT').textContent, desc: el('wxD').textContent,
    isSvg: el('wxI').innerHTML.indexOf('<svg') === 0,
    meta: (el('wxM').innerHTML.match(/<i>([^<]*)<\/i>/g) || [])
            .map(s => s.slice(3, -4)),
    days: (el('wxF').innerHTML.match(/<div>(\w{3})/g) || []).map(s => s.slice(5)),
    ranges: (el('wxF').innerHTML.match(/<b>(\d+)°<\/b>(\d+)°/g) || [])
              .map(s => s.replace(/<\/?b>/g, '').split('°').filter(Boolean)
                        .map(Number)),
    fcIcons: (el('wxF').innerHTML.match(/<div class="wfi"><svg/g) || []).length,
    updated: el('wxU').textContent,
  }));
}, 80);
"""
    p = run_node(wx_probe, "wxlive.js")
    check("8. weather renders from a real payload", p.returncode, 0)
    if p.returncode != 0:
        print("      " + p.stderr.strip().splitlines()[0][:200])
    else:
        w = tail_json(p.stdout) or {}
        check("8. no error while parsing BMKG", w.get("errors"), [])

        # Re-derive every expectation from the payload, in Python.
        slots = []
        for grp in bm["data"][0]["cuaca"]:
            slots.extend(grp if isinstance(grp, list) else [grp])
        slots.sort(key=lambda s: s["local_datetime"])
        by_day = {}
        for s in slots:
            by_day.setdefault(s["local_datetime"][:10], []).append(s)

        check("8. temperature is a plain integer",
              bool(re.match(r"^-?\d+$", w.get("temp", ""))), True)
        check("8. temperature is one of the payload values",
              int(w["temp"]) in [round(float(s["t"])) for s in slots], True)
        check("8. description is BMKG's own label",
              w.get("desc") in {s["weather_desc"] for s in slots}, True)
        check("8. current icon is an svg", w.get("isSvg"), True)
        check("8. humidity chip rendered",
              any(x.startswith("lembap ") for x in w.get("meta", [])), True)
        check("8. wind chip rendered",
              any(x.startswith("angin ") for x in w.get("meta", [])), True)
        check("8. updated line credits BMKG", "BMKG" in w.get("updated", ""), True)

        # Only whole days belong in the forecast row: a partial tail day gives a
        # min/max drawn from two or three slots that reads as a real daily range.
        cur_day = None
        nowkey = dt.datetime.now().strftime("%Y-%m-%d %H")
        for s in slots:
            if s["local_datetime"][:13] <= nowkey:
                cur_day = s["local_datetime"][:10]
        cur_day = cur_day or slots[0]["local_datetime"][:10]
        whole = sorted(k for k, v in by_day.items()
                       if len(v) >= 6 and k > cur_day)[:3]
        SHORT = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
        check("8. forecast lists only whole days", w.get("days"),
              [SHORT[dt.date(*map(int, k.split("-"))).weekday()] for k in whole])
        check("8. one icon per forecast day", w.get("fcIcons"), len(whole))
        check("8. forecast min/max match the payload", w.get("ranges"),
              [[round(max(float(s["t"]) for s in by_day[k])),
                round(min(float(s["t"]) for s in by_day[k]))] for k in whole])
        check("8. every range is max-then-min",
              [r for r in (w.get("ranges") or []) if r[0] < r[1]], [])

# --------------------------------------- 9. BMKG hour granularity
# The midday icon must not hinge on an exact hour string: BMKG emits 11:00 and
# 14:00 for Marunda, so matching '13' silently fell back to the 02:00 slot.
check("9. midday icon matches the nearest hour",
      "Math.abs(Number(a.local_datetime.slice(11,13)) - 12)" in widget_js, True)
check("9. partial days are filtered out", "byDay[k].length >= 6" in widget_js, True)

shutil.rmtree(tmp, ignore_errors=True)
print()
if fails:
    print(f"FAILED ({len(fails)}):")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("widgets: all checks passed")
