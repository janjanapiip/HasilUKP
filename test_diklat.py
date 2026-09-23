"""Cross-check the dashboard's diklat filter against independently written SQL.

The point is that /api/stats must not be able to pass by agreeing with itself:
every expected number below is computed here from ukp.db with queries written
separately from the ones in app.py.

Runs against a temp copy of ukp.db so production data and access_log are safe.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(prefix="ukp_diklat_")
shutil.copy(os.path.join(HERE, "ukp.db"), os.path.join(tmp, "ukp.db"))
os.environ["UKP_DB"] = os.path.join(tmp, "ukp.db")
os.environ["LOG_DB"] = os.path.join(tmp, "ukp.db")
os.environ.setdefault("UKP_SECRET", "test-only-secret")

sys.path.insert(0, HERE)
import app as A  # noqa: E402

A.app.config["TESTING"] = True
DB = os.environ["UKP_DB"]
fails = []


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f" != {want}"))
    if not ok:
        fails.append(label)


def api(**kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    with A.app.test_client() as c:
        r = c.get(f"/api/stats?{q}")
        assert r.status_code == 200, f"/api/stats?{q} -> {r.status_code}"
        return json.loads(r.data)


# ---------------------------------------------------------------- 1. dropdown
d = con()
want_dk = [r[0] for r in d.execute(
    "SELECT p.diklat FROM peserta p JOIN nilai n ON n.uc = p.uc"
    " WHERE p.diklat IS NOT NULL AND p.diklat <> ''"
    " GROUP BY p.diklat ORDER BY COUNT(*) DESC")]
got = api(year="all", basis="ujian")
check("1. dropdown lists every diklat owning exam rows", got["diklats"], want_dk)
print(f"     institutions: {', '.join(want_dk)}")

# ------------------------------------------------- 2. unfiltered must not shrink
# The peserta join must be absent when no diklat is chosen: 18 nilai rows have
# no peserta row and would vanish from the national totals.
all_rows = d.execute(
    "SELECT COUNT(*) FROM nilai WHERE tgl_ujian IS NOT NULL").fetchone()[0]
check("2. unfiltered total keeps orphan nilai rows", got["total"]["total"], all_rows)
orph = d.execute(
    "SELECT COUNT(*) FROM nilai n LEFT JOIN peserta p ON p.uc = n.uc"
    " WHERE n.tgl_ujian IS NOT NULL AND p.uc IS NULL").fetchone()[0]
print(f"     ({orph} rows have no peserta match; they must still be counted)")

# --------------------------------------------- 3. per-diklat totals & pass rate
sums = {"total": 0, "lulus": 0}
for dk in want_dk:
    w = d.execute(
        "SELECT COUNT(*) total, SUM(n.lulus) lulus, COUNT(DISTINCT n.uc) peserta"
        " FROM nilai n JOIN peserta p ON p.uc = n.uc"
        " WHERE n.tgl_ujian IS NOT NULL AND p.diklat = ?", [dk]).fetchone()
    g = api(year="all", basis="ujian", diklat=dk)
    check(f"3. {dk} total", g["total"]["total"], w["total"])
    check(f"3. {dk} lulus", g["total"]["lulus"], w["lulus"])
    check(f"3. {dk} peserta unik", g["total"]["peserta"], w["peserta"])
    check(f"3. {dk} echoes filter", g["diklat"], dk)
    sums["total"] += w["total"]
    sums["lulus"] += w["lulus"]
    rate = 100 * w["lulus"] / w["total"] if w["total"] else 0
    print(f"     {dk:10s} {w['total']:6d} rekaman  {rate:5.1f}% lulus")

# ------------------------------------------------------------ 4. the partition
# Every institution's rows plus the orphans must equal the national figure:
# proves the filter neither drops nor double-counts.
check("4. sum over institutions + orphans == national total",
      sums["total"] + orph, all_rows)

# ---------------------------------------------------- 5. per_diklat stays whole
# The comparison chart must keep all institutions even while filtered.
g = api(year="all", basis="ujian", diklat=want_dk[0])
check("5. per_diklat unaffected by the filter",
      sorted(r["diklat"] for r in g["per_diklat"]),
      sorted(r[0] for r in d.execute(
          "SELECT COALESCE(NULLIF(p.diklat,''),'(kosong)') FROM nilai n"
          " JOIN peserta p ON p.uc = n.uc WHERE n.tgl_ujian IS NOT NULL"
          " GROUP BY p.diklat")))
tbl = {r["diklat"]: r for r in g["per_diklat"]}
w0 = d.execute(
    "SELECT COUNT(*) total, SUM(n.lulus) lulus, COUNT(DISTINCT n.uc) peserta"
    " FROM nilai n JOIN peserta p ON p.uc = n.uc"
    " WHERE n.tgl_ujian IS NOT NULL AND p.diklat = ?", [want_dk[0]]).fetchone()
check(f"5. table row {want_dk[0]} total", tbl[want_dk[0]]["total"], w0["total"])
check(f"5. table row {want_dk[0]} peserta", tbl[want_dk[0]]["peserta"], w0["peserta"])

# ------------------------------------------------- 6. per_ijzh IS filtered down
dk = "AMC"
w = {r[0]: (r[1], r[2]) for r in d.execute(
    "SELECT n.ijzh, COUNT(*), SUM(n.lulus) FROM nilai n"
    " JOIN peserta p ON p.uc = n.uc"
    " WHERE n.tgl_ujian IS NOT NULL AND p.diklat = ? GROUP BY n.ijzh", [dk])}
g = api(year="all", basis="ujian", diklat=dk)
gi = {r["ijzh"]: (r["total"], r["lulus"]) for r in g["per_ijzh"]}
check(f"6. per_ijzh scoped to {dk}", gi, w)

# --------------------------------------------------- 7. diklat + year together
for dk, yr in (("STIP", "2026"), ("AMC", "2021")):
    w = d.execute(
        "SELECT COUNT(*) total, SUM(n.lulus) lulus FROM nilai n"
        " JOIN peserta p ON p.uc = n.uc"
        " WHERE substr(n.tgl_ujian,1,4) = ? AND p.diklat = ?", [yr, dk]).fetchone()
    g = api(year=yr, basis="ujian", diklat=dk)
    check(f"7. {dk} + {yr} total", g["total"]["total"], w["total"])
    check(f"7. {dk} + {yr} lulus", g["total"]["lulus"], w["lulus"])

# ------------------------------------------------------- 8. basis=sidang honoured
w = d.execute(
    "SELECT COUNT(*) FROM nilai n JOIN peserta p ON p.uc = n.uc"
    " WHERE n.tgl_sidang IS NOT NULL AND p.diklat = 'AMD'").fetchone()[0]
check("8. basis=sidang + diklat", api(basis="sidang", diklat="AMD")["total"]["total"], w)

# --------------------------------------------------------------- 9. SKL scoped
dk = "AMC"
w = d.execute(
    "SELECT COUNT(DISTINCT s.uc) total,"
    " COUNT(DISTINCT CASE WHEN s.tgl_cetak IS NOT NULL THEN s.uc END) cetak"
    " FROM skl s JOIN peserta p ON p.uc = s.uc WHERE p.diklat = ?", [dk]).fetchone()
g = api(year="all", basis="ujian", diklat=dk)
check(f"9. SKL total scoped to {dk}", g["skl"]["total"], w["total"])
check(f"9. SKL cetak scoped to {dk}", g["skl"]["cetak"], w["cetak"])

# ------------------------------------------------------ 10. monthly/annual sum
g = api(year="2026", basis="ujian", diklat="STIP")
check("10. monthly sums to total",
      sum(r["total"] for r in g["monthly"]), g["total"]["total"])
# annual ignores the year filter on purpose (trend line) but keeps the diklat one
wa = d.execute(
    "SELECT COUNT(*) FROM nilai n JOIN peserta p ON p.uc = n.uc"
    " WHERE n.tgl_ujian IS NOT NULL AND p.diklat = 'STIP'").fetchone()[0]
check("10. annual spans all years, still scoped",
      sum(r["total"] for r in g["annual"]), wa)

# ------------------------------------------------------------ 11. bad input safe
for bad in ("ZZZ", "'; DROP TABLE nilai;--", "", "%20"):
    r = api(year="all", basis="ujian", diklat=bad.replace(" ", "%20").replace("&", ""))
    check(f"11. junk diklat {bad!r} falls back to national", r["total"]["total"], all_rows)
check("11. nilai table intact",
      d.execute("SELECT COUNT(*) FROM nilai").fetchone()[0],
      con().execute("SELECT COUNT(*) FROM nilai").fetchone()[0])

# -------------------------------------------------------------- 12. page renders
with A.app.test_client() as c:
    html = c.get("/").get_data(as_text=True)
check("12. index has the diklat select", 'id="fDiklat"' in html, True)
for dk in want_dk:
    if f'value="{dk}"' not in html:
        fails.append(f"12. {dk} missing from dropdown")
check("12. every institution is an option",
      all(f'value="{dk}"' in html for dk in want_dk), True)
check("12. recap table present", 'id="tDiklat"' in html, True)
check("12. api call passes diklat", "diklat=${encodeURIComponent(dk)}" in html, True)

# --------------------------------------------- 13. every institution has a name
g = api(year="all", basis="ujian")
missing = [r["diklat"] for r in g["per_diklat"] if not r["deskripsi"]]
check("13. every charted institution has a description", missing, [])
for dk in ("STIP", "BINA SENA"):
    check(f"13. {dk} name resolves via alias",
          bool(api(year="all", diklat=dk)["diklat_nama"]), True)

shutil.rmtree(tmp, ignore_errors=True)
print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("diklat dashboard filter: all cross-checks passed")
