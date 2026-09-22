"""Report: exam registrations that never passed and never came back for a retake.

A "retake" is any later exam by the same seafarer code at the same ijazah level,
whether it appears as mengulang_ke inside the original registration or as a
fresh re-registration under a new uc. Both are counted, so nobody who genuinely
returned is reported as dormant.

Run: python report_no_retake.py [DIKLAT]
     python report_no_retake.py          -> all institutions
     python report_no_retake.py AMC      -> one institution
"""
import datetime
import json
import os
import sqlite3
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ukp.db")
PASS_MARK = 70          # a subject is failed below this; matches etl.py's lulus rule

only = (sys.argv[1].upper() if len(sys.argv) > 1 else None)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

# Registrations with exam records but no pass, then filter to those with no
# later attempt at the same level. Done as two views so the "later exam"
# subquery reads plainly rather than as a nested join.
con.executescript("""
CREATE TEMP VIEW failed_uc AS
SELECT p.uc, p.sc, p.ijzh, p.diklat, p.nama, p.tpt_lahir, p.tgl_lahir,
       p.dob_usable, p.ukp1,
       COUNT(n.id) att, MAX(n.tgl_ujian) last_ex, MAX(n.mengulang_ke) ulang
FROM peserta p JOIN nilai n ON n.uc = p.uc
GROUP BY p.uc
HAVING SUM(n.lulus) = 0;

CREATE TEMP VIEW no_retake AS
SELECT f.* FROM failed_uc f
WHERE NOT EXISTS (
  SELECT 1 FROM peserta p2 JOIN nilai n2 ON n2.uc = p2.uc
  WHERE p2.sc = f.sc AND p2.ijzh = f.ijzh AND n2.tgl_ujian > f.last_ex);
""")

sql = "SELECT * FROM no_retake"
args = []
if only:
    sql += " WHERE diklat = ?"
    args = [only]
sql += " ORDER BY diklat, last_ex DESC, nama"
rows = con.execute(sql, args).fetchall()
if not rows:
    raise SystemExit(f"no rows for diklat={only!r}")

# Exam sessions, to count how many chances each person has let pass since.
sessions = [r[0] for r in con.execute(
    "SELECT DISTINCT tgl_ujian FROM nilai WHERE tgl_ujian IS NOT NULL"
    " ORDER BY tgl_ujian")]
latest = sessions[-1]

ref = {r["ijzh"]: r for r in con.execute("SELECT * FROM ref_ijzh")}
diklat_nama = {r["diklat"]: r["deskripsi"]
               for r in con.execute("SELECT * FROM ref_diklat")}


def failed_subjects(uc, ijzh):
    """Names of the subjects still below the pass mark on the last attempt."""
    r = con.execute(
        "SELECT mu, tgl_ujian FROM nilai WHERE uc=?"
        " ORDER BY tgl_ujian DESC, mengulang_ke DESC LIMIT 1", (uc,)).fetchone()
    if not r or not r["mu"]:
        return [], None
    scores = json.loads(r["mu"])
    names = json.loads(ref[ijzh]["mu_names"]) if ijzh in ref and ref[ijzh]["mu_names"] else []
    out = []
    for i, s in enumerate(scores):
        if s is None or s >= PASS_MARK:
            continue
        # mu_names can be shorter than the score list for older ijazah revisions
        out.append((names[i] if i < len(names) else f"Materi {i+1}", s))
    return out, r["tgl_ujian"]


wb = Workbook()
ws = wb.active
ws.title = "Belum Lulus & Belum Mengulang"

title = (f"PESERTA BELUM LULUS DAN BELUM MENGULANG UJIAN"
         f"{' - ' + only if only else ''}")
ws.append([title])
ws.append([f"Data per {latest} · {len(rows)} registrasi · "
           f"dibuat {datetime.datetime.now():%Y-%m-%d %H:%M}"])
ws.append([])

head = ["No", "Kode Pelaut", "Nama Lengkap", "Tempat, Tanggal Lahir",
         "Lembaga Diklat", "Tingkat Ijazah", "Keterangan Ijazah",
         "Jumlah Ujian", "Ujian Terakhir", "Sesi Terlewat",
         "Lama Menganggur (bulan)", "Jumlah Materi Belum Lulus",
         "Materi Belum Lulus (nilai)", "Nilai Terendah"]
ws.append(head)

today = datetime.date.fromisoformat(latest)
for i, r in enumerate(rows, 1):
    subs, last_try = failed_subjects(r["uc"], r["ijzh"])
    missed = sum(1 for s in sessions if s > r["last_ex"])
    d = datetime.date.fromisoformat(r["last_ex"])
    months = (today.year - d.year) * 12 + (today.month - d.month)
    ttl = ", ".join(x for x in (
        r["tpt_lahir"], r["tgl_lahir"] if r["dob_usable"] else None) if x)
    ws.append([
        i, r["sc"], r["nama"], ttl or "-",
        r["diklat"] or "", r["ijzh"],
        (ref[r["ijzh"]]["deskripsi"] if r["ijzh"] in ref else ""),
        r["att"], r["last_ex"], missed, months,
        len(subs),
        "; ".join(f"{n} ({v})" for n, v in subs) or "-",
        min((v for _, v in subs), default=""),
    ])

# --- formatting ---
navy = PatternFill("solid", fgColor="0E1937")
ws["A1"].font = Font(bold=True, size=13, color="0E1937")
ws["A2"].font = Font(italic=True, size=9, color="5B6B85")
hdr_row = 4
for c in ws[hdr_row]:
    c.font = Font(bold=True, color="FFFFFF", size=10)
    c.fill = navy
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
ws.row_dimensions[hdr_row].height = 34

widths = [5, 13, 30, 26, 13, 9, 42, 8, 12, 9, 12, 11, 70, 9]
for i, w in enumerate(widths, 1):
    ws.column_dimensions[get_column_letter(i)].width = w

for row in ws.iter_rows(min_row=hdr_row + 1, max_row=ws.max_row):
    row[12].alignment = Alignment(wrap_text=True, vertical="top")   # subject list
    for j in (0, 7, 9, 10, 11, 13):
        row[j].alignment = Alignment(horizontal="center", vertical="top")
    # flag the long-dormant: 12+ months with no return
    if isinstance(row[10].value, int) and row[10].value >= 12:
        row[10].font = Font(bold=True, color="A8071A")

ws.freeze_panes = f"A{hdr_row + 1}"
ws.auto_filter.ref = f"A{hdr_row}:{get_column_letter(len(head))}{ws.max_row}"

# --- summary sheet ---
s2 = wb.create_sheet("Rekap")
s2.append(["REKAP PESERTA BELUM LULUS & BELUM MENGULANG"])
s2["A1"].font = Font(bold=True, size=12, color="0E1937")
s2.append([])


def block(title, sql_, cols):
    s2.append([title])
    s2.cell(s2.max_row, 1).font = Font(bold=True, size=10)
    s2.append(cols)
    for c in s2[s2.max_row]:
        c.font = Font(bold=True, color="FFFFFF", size=9)
        c.fill = navy
    q = sql_ + (" WHERE diklat=?" if only and "WHERE" not in sql_ else "")
    for r in con.execute(sql_, args):
        s2.append(list(r))
    s2.append([])


w = " WHERE diklat=?" if only else ""
block("Per Lembaga Diklat",
      f"SELECT diklat, COUNT(*) FROM no_retake{w} GROUP BY diklat ORDER BY 2 DESC",
      ["Diklat", "Jumlah"])
block("Per Tingkat Ijazah",
      f"SELECT ijzh, COUNT(*) FROM no_retake{w} GROUP BY ijzh ORDER BY 2 DESC",
      ["Ijazah", "Jumlah"])
block("Per Tahun Ujian Terakhir",
      f"SELECT substr(last_ex,1,4), COUNT(*) FROM no_retake{w}"
      f" GROUP BY 1 ORDER BY 1 DESC",
      ["Tahun", "Jumlah"])
block("Per Jumlah Percobaan",
      f"SELECT att, COUNT(*) FROM no_retake{w} GROUP BY att ORDER BY att",
      ["Jumlah Ujian", "Peserta"])

s2.column_dimensions["A"].width = 46
s2.column_dimensions["B"].width = 12
s2.append([])
s2.append(["Catatan:"])
s2.cell(s2.max_row, 1).font = Font(bold=True, size=9)
for line in (
    "Belum lulus = seluruh percobaan pada registrasi tersebut tidak lulus "
    f"(ada materi di bawah {PASS_MARK}).",
    "Belum mengulang = tidak ada ujian berikutnya pada tingkat ijazah yang sama, "
    "baik sebagai ulangan maupun registrasi baru.",
    f"Sesi terlewat dihitung dari jumlah sesi ujian setelah tanggal ujian terakhir peserta (data per {latest}).",
    "Peserta dapat muncul lebih dari sekali bila gagal pada lebih dari satu tingkat ijazah.",
):
    s2.append([line])
    s2.cell(s2.max_row, 1).font = Font(italic=True, size=9, color="5B6B85")

name = f"belum-lulus-belum-mengulang{'-' + only if only else ''}-{datetime.date.today():%Y%m%d}.xlsx"
out = os.path.join(HERE, name)
wb.save(out)

# self-check: nobody in the report may have a later exam at the same level
bad = con.execute("""
SELECT COUNT(*) FROM no_retake f
WHERE EXISTS (SELECT 1 FROM peserta p2 JOIN nilai n2 ON n2.uc=p2.uc
              WHERE p2.sc=f.sc AND p2.ijzh=f.ijzh AND n2.tgl_ujian > f.last_ex)
""").fetchone()[0]
assert bad == 0, f"{bad} reported rows actually did retake"
passed = con.execute("""
SELECT COUNT(*) FROM no_retake f
WHERE EXISTS (SELECT 1 FROM nilai n WHERE n.uc=f.uc AND n.lulus=1)
""").fetchone()[0]
assert passed == 0, f"{passed} reported rows actually passed"

print(f"{out}\n{len(rows)} rows · self-check OK (no retakes, no passes included)")
con.close()
