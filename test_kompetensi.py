"""Cross-check /kompetensi against the source workbook, not against its own JSON.

Every expected figure is re-read from 'Daftar Kompetensi UKP Perdana.xlsx' with
queries written separately from build_kompetensi.py, so the page cannot pass by
agreeing with the extractor that produced it.
"""
import json
import os
import shutil
import sys
import tempfile

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.environ.get(
    "KOMPETENSI_XLSX", r"D:\PUKP-3\Daftar Kompetensi UKP Perdana.xlsx")

tmp = tempfile.mkdtemp(prefix="ukp_komp_")
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


K = json.load(open(os.path.join(HERE, "kompetensi.json"), encoding="utf-8"))
wb = openpyxl.load_workbook(XLSX, data_only=True)
txt = lambda c: "" if c is None else str(c).strip()

# ------------------------------------------- 1. index sheet: names and counts
want_idx = {}
for r in wb["Daftar Kompetensi UKP"].iter_rows(min_row=5, values_only=True):
    a, b, c, d = (txt(x) for x in (r + (None,) * 4)[:4])
    if b and c and d:
        want_idx[b] = (c, int(d))
check("1. level count matches index sheet", len(K["tingkat"]), len(want_idx))
for t in K["tingkat"]:
    w = want_idx.get(t["tingkat"])
    if not w:
        fails.append(f"1. {t['tingkat']} not in index sheet")
        continue
    if (t["nama"], t["total"]) != w:
        check(f"1. {t['tingkat']} nama/jumlah", (t["nama"], t["total"]), w)
check("1. all index levels present",
      sorted(want_idx), sorted(t["tingkat"] for t in K["tingkat"]))

# ------------------------------------------ 2. recap sheet: CBA / komp split
want_rec = {}
for r in wb["Rekap Mata Uji"].iter_rows(min_row=4, values_only=True):
    a, b, c, d, e = (txt(x) for x in (r + (None,) * 5)[:5])
    if b and c != "":
        want_rec[b] = (int(c or 0), int(d or 0), int(e or 0))
for t in K["tingkat"]:
    w = want_rec.get(t["tingkat"])
    if w and (t["cba"], t["komp"], t["total"]) != w:
        check(f"2. {t['tingkat']} cba/komp/total", (t["cba"], t["komp"], t["total"]), w)
check("2. every level appears in the recap sheet",
      sorted(want_rec), sorted(t["tingkat"] for t in K["tingkat"]))
check("2. grand total matches recap sheet", K["total_mu"], sum(v[2] for v in want_rec.values()))
check("2. CBA total matches", K["total_cba"], sum(v[0] for v in want_rec.values()))
check("2. Komprehensif total matches", K["total_komp"], sum(v[1] for v in want_rec.values()))

# ------------------------------------- 3. detail sheets: every subject row read
raw = 0
for sh in ("Nautika Peningkatan", "Nautika Pembentukan", "Keterampilan",
           "Teknika Peningkatan", "Teknika Pembentukan"):
    for r in wb[sh].iter_rows(min_row=3, values_only=True):
        a, b, c, d, e, f = (txt(x) for x in (r + (None,) * 6)[:6])
        if c and e and not a.lower().startswith("subtotal"):
            raw += 1
check("3. every subject row extracted", sum(len(t["mu"]) for t in K["tingkat"]), raw)
check("3. subject total is 269", K["total_mu"], 269)
for t in K["tingkat"]:
    nos = [m["no"] for m in t["mu"]]
    if nos != list(range(1, len(nos) + 1)):
        fails.append(f"3. {t['tingkat']} subject numbers not sequential")
    jen = {m["jenis"] for m in t["mu"]}
    if not jen <= {"CBA", "Komprehensif"}:
        fails.append(f"3. {t['tingkat']} unexpected jenis: {jen}")
check("3. numbering and jenis clean across all levels",
      [f for f in fails if f.startswith("3.")], [])
# The detail sheet labels GMDSS MU03/MU04 'Praktik'; the recap sheet and PUKP
# both treat them as Komprehensif. Assert the extractor normalises every
# non-CBA label, so a workbook that reintroduces 'Praktik' cannot leak it to
# the page or split the filter into a third option.
check("3. no Praktik survives extraction",
      sorted({m["jenis"] for t in K["tingkat"] for m in t["mu"]}),
      ["CBA", "Komprehensif"])
gmdss = next(t for t in K["tingkat"] if t["kode"] == "GMDSS")
check("3. GMDSS MU03/MU04 are Komprehensif",
      [m["jenis"] for m in gmdss["mu"] if m["kode"] in ("MU03", "MU04")],
      ["Komprehensif", "Komprehensif"])
check("3. GMDSS split is 2 CBA + 2 Komprehensif",
      (gmdss["cba"], gmdss["komp"], gmdss["total"]), (2, 2, 4))
# The source sheet must still say Praktik -- if it stops, the normalisation is
# dead code and this test should be revisited rather than silently passing.
src_prak = sum(
    1 for r in wb["Keterampilan"].iter_rows(min_row=3, values_only=True)
    if txt((r + (None,) * 6)[5]) == "Praktik")
check("3. source workbook still labels 2 rows Praktik", src_prak, 2)

# ------------------------------------------- 4. codes line up with the database
import sqlite3  # noqa: E402
d = sqlite3.connect(os.environ["UKP_DB"])
dbc = {r[0] for r in d.execute("SELECT DISTINCT ijzh FROM nilai WHERE COALESCE(ijzh,'')<>''")}
kc = {t["kode"] for t in K["tingkat"]}
check("4. no guide code missing from the database", sorted(kc - dbc), [])
check("4. no database code missing from the guide", sorted(dbc - kc), [])
check("4. every level has a code", [t["tingkat"] for t in K["tingkat"] if not t["kode"]], [])
for t in K["tingkat"]:
    r = d.execute("SELECT deskripsi FROM ref_ijzh WHERE ijzh=?", [t["kode"]]).fetchone()
    if r and r[0] and r[0].strip().upper() != t["nama"].strip().upper():
        fails.append(f"4. {t['kode']} nama differs from ref_ijzh")
check("4. names agree with ref_ijzh", [f for f in fails if f.startswith("4. ")
                                       and "nama differs" in f], [])

# ------------------------------------------------------------ 5. the page itself
with A.app.test_client() as c:
    r = c.get("/kompetensi")
    check("5. /kompetensi returns 200", r.status_code, 200)
    html = r.get_data(as_text=True)
check("5. print button present", 'window.print()' in html, True)
check("5. A4 page rule present", '@page' in html and 'A4' in html, True)
check("5. level filter present", 'id="fLv"' in html, True)
check("5. search box present", 'id="fQ"' in html, True)
missing_lv = [t["kode"] for t in K["tingkat"] if f'data-k="{t["kode"]}"' not in html]
check("5. every level rendered on the page", missing_lv, [])
# Spot-check real subject text, including a long one and an apostrophe.
spot = [
    ("UGN1", "MU11", "English Language"),
    ("PASN3", "MU01", None),
    ("GMDSS", "MU01", None),
]
for code, mu, want_name in spot:
    t = next(x for x in K["tingkat"] if x["kode"] == code)
    m = next(x for x in t["mu"] if x["kode"] == mu)
    if want_name:
        check(f"5. {code} {mu} name", m["nama"], want_name)
    if m["nama"][:40] not in html:
        fails.append(f"5. {code} {mu} text missing from page")
check("5. spot-checked subjects appear in the HTML",
      [f for f in fails if "text missing" in f], [])
check("5. subject rows in HTML", html.count('class="mu"'), K["total_mu"])
check("5. no Praktik tag rendered", 'tag prak' in html or 'Praktik' in html, False)
# detail.html deep-links to /kompetensi#<kode>, so every level needs an anchor.
missing_anchor = [t["kode"] for t in K["tingkat"] if f'id="{t["kode"]}"' not in html]
check("5. every level has a deep-link anchor", missing_anchor, [])

# ------------------------------------------------------- 6. reachable from nav
with A.app.test_client() as c:
    for path in ("/", "/cek"):
        if 'href="/kompetensi"' not in c.get(path).get_data(as_text=True):
            fails.append(f"6. nav link missing on {path}")
check("6. nav link on guest pages", [f for f in fails if f.startswith("6.")], [])

# --------------------------------------------- 7. guide survives a missing file
old = A.KOMPETENSI
try:
    A.KOMPETENSI = None
    with A.app.test_client() as c:
        check("7. missing JSON degrades to 503, not a crash",
              c.get("/kompetensi").status_code, 503)
finally:
    A.KOMPETENSI = old

shutil.rmtree(tmp, ignore_errors=True)
print()
print(f"levels: {K['total_tingkat']}  subjects: {K['total_mu']}"
      f"  ({K['total_cba']} CBA + {K['total_komp']} Komprehensif)")
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("kompetensi guide: all cross-checks passed")
