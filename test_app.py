"""Self-check against the real ukp.db. Run: python test_app.py"""
import os
import re
import shutil
import sqlite3
import tempfile

import app as A

# Run against a throwaway copy: the rate-limit check below writes bad_verify
# rows, which would otherwise lock the real site out of its own access_log.
A.DB = os.path.join(tempfile.mkdtemp(), "test.db")
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ukp.db"), A.DB)
A.LOG_DB = A.DB          # keep log writes in the throwaway copy
A.app.config["TESTING"] = True
A.app.secret_key = "test"
con = sqlite3.connect(A.DB)
con.row_factory = sqlite3.Row
# Clear any leftover rate-limit rows from the real DB (copied above)
con.execute("DELETE FROM access_log")
con.commit()

# pick a real seafarer with grades, a usable DOB and an SKL row
row = con.execute("""
    SELECT p.sc, p.nama, p.tgl_lahir, p.ukp1 FROM peserta p
    JOIN nilai n USING(uc) JOIN skl s ON s.uc = p.uc
    WHERE p.dob_usable = 1 AND p.ukp1 IS NOT NULL AND s.tgl_cetak IS NOT NULL
    LIMIT 1""").fetchone()
assert row, "no fixture row in db - run etl.py first"
sc, nama, dob, ukp1 = row["sc"], row["nama"], row["tgl_lahir"], row["ukp1"]
print(f"fixture: {nama} sc={sc} dob={dob} ukp1={ukp1}")

with A.app.test_client() as c:
    before = con.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]

    r = c.get("/")
    assert b"peserta" in r.data and b"rekaman ujian" in r.data, "summary missing"
    # dashboard shell: header text, the four canvases, and the vendored chart lib
    assert "Pelaksana Ujian Keahlian Pelaut-3 Wilayah I Jakarta".encode() in r.data, "header missing"
    for cv in (b"cPeriod", b"cRate", b"cIjzh", b"cDiklat"):
        assert cv in r.data, f"canvas {cv} missing"
    assert c.get("/static/chart.umd.min.js").status_code == 200, "chart.js not vendored"

    # stats API: every filter combination answers, and totals stay self-consistent
    for url in ("/api/stats?year=all&basis=ujian", "/api/stats?year=all&basis=sidang",
                "/api/stats?year=2026&basis=ujian", "/api/stats?year=2026&basis=sidang"):
        j = c.get(url).get_json()
        assert j["total"]["total"] >= j["total"]["lulus"] >= 0, f"bad totals {url}"
        assert sum(m["total"] for m in j["monthly"]) == j["total"]["total"], f"monthly != total {url}"
        assert sum(i["total"] for i in j["per_ijzh"]) == j["total"]["total"], f"ijzh != total {url}"
        assert j["skl"]["total"] >= (j["skl"]["cetak"] or 0), f"skl cetak > total {url}"
    # a year filter must actually narrow the result
    assert (c.get("/api/stats?year=2026&basis=ujian").get_json()["total"]["total"]
            < c.get("/api/stats?year=all&basis=ujian").get_json()["total"]["total"]), "year filter inert"

    r = c.post("/cek", data={"q": sc})
    assert nama.encode() in r.data, "search by seafarer code failed"
    r = c.post("/cek", data={"q": nama[:6]})
    assert b"Seafarer Code" in r.data, "search by name failed"
    r = c.post("/cek", data={"q": "ZZQQXX"})
    assert "tidak ditemukan".encode() in r.data, "empty search not handled"

    # /cek page must render on its own (GET)
    r = c.get("/cek")
    assert r.status_code == 200 and b"Cek Data Peserta" in r.data, "/cek page missing"

    # detail must be unreachable before verifying
    assert c.get(f"/detail/{sc}").status_code == 302, "detail not gated"

    r = c.post(f"/verify/{sc}", data={"tgl_lahir": "1801-01-01", "ukp1": ""})
    assert "tidak cocok".encode() in r.data, "bad verify accepted"

    r = c.post(f"/verify/{sc}", data={"tgl_lahir": dob, "ukp1": ""}, follow_redirects=True)
    assert b"Materi Uji" in r.data, "DOB verify failed"
    assert b"Surat Keterangan Lulus" in r.data, "SKL block missing"
    c.get("/logout")

    # second factor alone must also pass (DOB is placeholder for ~15% of records)
    r = c.post(f"/verify/{sc}", data={"tgl_lahir": "", "ukp1": ukp1}, follow_redirects=True)
    assert b"Materi Uji" in r.data, "UKP1-only verify failed"

    # --- progression: one seafarer code, several ijazah levels, oldest first ---
    multi = con.execute("""
        SELECT p.sc, MIN(p.ukp1) u1, COUNT(DISTINCT p.uc) lv
        FROM peserta p JOIN nilai n ON n.uc = p.uc
        WHERE p.dob_usable = 1 AND n.tgl_ujian IS NOT NULL
        GROUP BY p.sc HAVING lv >= 3 LIMIT 1""").fetchone()
    if multi:
        c.get("/logout")
        r = c.post(f"/verify/{multi['sc']}", data={"tgl_lahir": "", "ukp1": multi["u1"]},
                   follow_redirects=True)
        h = r.data.decode("utf-8", "replace")
        assert "Riwayat Tingkat Ijazah" in h, "progression timeline missing"
        # the timeline must list every level the seafarer code owns
        steps = re.findall(r'tl-dot (?:pass|fail)">(\d+)</div>\s*<div class="tl-body">\s*<b>(\w+)</b>', h)
        assert len(steps) == multi["lv"], f"timeline shows {len(steps)} of {multi['lv']} levels"
        assert [int(n) for n, _ in steps] == list(range(1, multi["lv"] + 1)), "steps misnumbered"
        # dates must run oldest -> newest
        dates = re.findall(r'tl-body">.*?<div class="muted">\s*(\d{4}-\d{2}-\d{2})', h, re.S)
        assert dates == sorted(dates), f"levels out of chronological order: {dates}"
        # per-level cards carry the same ordering
        cards = re.findall(r'step-no">(\d+)</span>(\w+)', h)
        assert [lv for _, lv in cards] == [lv for _, lv in steps], "cards disagree with timeline"
        print(f"progression OK: {multi['sc']} {' -> '.join(lv for _, lv in steps)}")
        c.get("/logout")

    # search results must expose the levels so the upgrade path is visible pre-verify
    r = c.post("/cek", data={"q": sc})
    assert b"Tingkat Ijazah" in r.data, "search results missing level column"

    # --- SKL status: reprints must show the NEWEST print date ---
    rp = con.execute("""
        SELECT p.sc, p.ukp1, MAX(s.tgl_cetak) newest, MIN(s.tgl_cetak) oldest
        FROM skl s JOIN peserta p ON p.uc = s.uc
        WHERE p.ukp1 IS NOT NULL AND s.tgl_cetak IS NOT NULL
        GROUP BY s.uc HAVING COUNT(*) > 1 AND newest <> oldest LIMIT 1""").fetchone()
    if rp:
        c.get("/logout")
        h = c.post(f"/verify/{rp['sc']}", data={"tgl_lahir": "", "ukp1": rp["ukp1"]},
                   follow_redirects=True).data.decode("utf-8", "replace")
        assert rp["newest"] in h, f"newest print date {rp['newest']} not shown"
        assert rp["oldest"] not in h, f"stale print date {rp['oldest']} shown instead"
        assert "cetak, tanggal terbaru" in h, "reprint count not disclosed"
        print(f"SKL reprint OK: {rp['sc']} shows {rp['newest']} not {rp['oldest']}")
        c.get("/logout")

    # a pass with no SKL row must read 'belum terbit', never 'sudah dicetak'
    nb = con.execute("""
        SELECT p.sc, p.ukp1 FROM peserta p JOIN nilai n ON n.uc = p.uc
        WHERE n.lulus = 1 AND p.ukp1 IS NOT NULL
          AND p.sc NOT IN (SELECT p2.sc FROM peserta p2 JOIN skl s ON s.uc = p2.uc)
        LIMIT 1""").fetchone()
    if nb:
        h = c.post(f"/verify/{nb['sc']}", data={"tgl_lahir": "", "ukp1": nb["ukp1"]},
                   follow_redirects=True).data.decode("utf-8", "replace")
        assert "BELUM TERBIT" in h, "passed-but-unissued SKL mislabelled"
        assert "SUDAH DICETAK" not in h, "unissued SKL claimed as printed"
        c.get("/logout")

    # dashboard SKL figures count seafarers, not reprint rows
    j = c.get("/api/stats?year=all&basis=ujian").get_json()
    uc_tot, uc_cetak = con.execute(
        "SELECT COUNT(DISTINCT uc),"
        " COUNT(DISTINCT CASE WHEN tgl_cetak IS NOT NULL THEN uc END) FROM skl").fetchone()
    assert j["skl"]["total"] == uc_tot, f"skl total {j['skl']['total']} != {uc_tot} distinct uc"
    assert j["skl"]["cetak"] == uc_cetak, f"skl cetak {j['skl']['cetak']} != {uc_cetak} distinct uc"
    rows_cetak = con.execute("SELECT COUNT(*) FROM skl WHERE tgl_cetak IS NOT NULL").fetchone()[0]
    assert j["skl"]["cetak"] < rows_cetak, "reprints still inflating the printed count"
    print(f"SKL stat OK: {j['skl']['cetak']} seafarers printed (was {rows_cetak} rows)")

    # Indonesian month names ('06 Juli 2026') used to parse as None, so a
    # printed SKL showed as unprinted. Guard the loaded data, not just the ETL.
    blank = con.execute("SELECT COUNT(*) FROM skl WHERE tgl_cetak IS NULL").fetchone()[0]
    assert blank < 20, f"{blank} SKL rows have no print date - date parsing regressed?"
    blank_ujian = con.execute("SELECT COUNT(*) FROM nilai WHERE tgl_ujian IS NULL").fetchone()[0]
    assert blank_ujian == 0, f"{blank_ujian} exam rows have no date - date parsing regressed?"
    c.get("/logout")

    # rate limit kicks in after FAIL_LIMIT bad attempts
    for _ in range(A.FAIL_LIMIT):
        c.post(f"/verify/{sc}", data={"tgl_lahir": "1801-01-01", "ukp1": ""})
    r = c.post(f"/verify/{sc}", data={"tgl_lahir": dob, "ukp1": ""})
    assert b"Terlalu banyak" in r.data, "rate limit not enforced"

    after = con.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert after > before, "nothing written to access_log"
    kinds = dict(con.execute(
        "SELECT outcome, COUNT(*) FROM access_log GROUP BY outcome").fetchall())
    for k in ("ok", "not_found", "bad_verify", "rate_limited"):
        assert k in kinds, f"outcome {k!r} never logged"

# Key repair: records whose exam number differed only by letter case or batch
# suffix must end up attached to a real person, and be reachable in the UI.
repaired = con.execute(
    "SELECT n.uc, n.uc_raw, p.sc, p.nama FROM nilai n JOIN peserta p USING(uc)"
    " WHERE n.uc <> n.uc_raw").fetchall()
assert repaired, "key repair recovered nothing - canon() regression?"
for r in repaired:
    assert r["uc_raw"].upper().replace("-", "")[:20] \
        == r["uc"].upper().replace("-", "")[:20], \
        f"repaired key is not the same exam: {r['uc_raw']} -> {r['uc']}"
print(f"key repair: {len(repaired)} nilai rows re-attached, e.g. "
      f"{repaired[0]['uc_raw']} -> {repaired[0]['uc']} ({repaired[0]['nama']})")

with A.app.test_client() as c:
    # the rate-limit block above left this IP throttled; clear it
    con.execute("DELETE FROM access_log WHERE outcome='bad_verify'")
    con.commit()
    sc2 = repaired[0]["sc"]
    p = con.execute("SELECT tgl_lahir, ukp1, dob_usable FROM peserta WHERE sc=?"
                    " AND ukp1 IS NOT NULL LIMIT 1", (sc2,)).fetchone()
    if p:
        r = c.post(f"/verify/{sc2}", data={"tgl_lahir": "", "ukp1": p["ukp1"]},
                   follow_redirects=True)
        assert b"Materi Uji" in r.data, "repaired record not viewable in UI"
        print("repaired record renders for its owner")

print("all checks passed; access_log outcomes:", kinds)
con.close()
