"""Safely rebuild ukp.db from the two source workbooks.

Why this exists instead of running etl.py directly: etl.py writes into the
live ukp.db, so a bad workbook or a crash halfway leaves the app with a
half-built database and no way back. This builds into a temporary file,
checks it against the database currently in use, and only swaps it in if the
new one is sane. The previous database is kept as ukp.db.bak either way.

Run:  python update_data.py
      python update_data.py --yes      skip the confirmation prompt
      python update_data.py --check    build and compare, then throw it away

A table shrinking by more than 2% is treated as suspicious and blocks the
swap, because the usual cause is a workbook saved with a filter applied or a
sheet accidentally truncated - which looks like a successful run otherwise.
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ukp.db")
NEW = os.path.join(HERE, "ukp.db.new")
BAK = os.path.join(HERE, "ukp.db.bak")
SRC_DIR = os.path.join(os.path.dirname(HERE), "v2026")
SOURCES = ["ukp_v2021.xlsx", "ukp_v2026.xlsx"]

# Tables that must never quietly lose rows. access_log is excluded on purpose:
# it lives only in the running database and is not rebuilt.
COUNTED = ["peserta", "nilai", "skl", "ref_ijzh", "ref_diklat"]
SHRINK_TOLERANCE = 0.02


def counts(path):
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect(path)
    out = {}
    for t in COUNTED:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    for label, sql in (("lulus", "SELECT COUNT(*) FROM nilai WHERE lulus=1"),
                       ("ujian_terakhir", "SELECT MAX(tgl_ujian) FROM nilai"),
                       ("cetak_terakhir", "SELECT MAX(tgl_cetak) FROM skl")):
        try:
            out[label] = con.execute(sql).fetchone()[0]
        except sqlite3.Error:
            out[label] = None
    con.close()
    return out


def carry_over_log(old, new):
    """access_log records who used the app; it is not in the workbooks, so a
    rebuild would erase it. Copy it into the new database before the swap."""
    if not os.path.exists(old):
        return 0
    src = sqlite3.connect(old)
    try:
        rows = src.execute("SELECT ts,ip,ua,action,q,sc,uc,outcome"
                           " FROM access_log").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        src.close()
    if not rows:
        return 0
    dst = sqlite3.connect(new)
    dst.executemany(
        "INSERT INTO access_log (ts,ip,ua,action,q,sc,uc,outcome)"
        " VALUES (?,?,?,?,?,?,?,?)", rows)
    dst.commit()
    dst.close()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="do not ask before swapping")
    ap.add_argument("--check", action="store_true",
                    help="build and compare only, leave ukp.db untouched")
    args = ap.parse_args()

    print("=" * 62)
    print("PEMBARUAN DATA UKP")
    print("=" * 62)

    # 1. sources present and readable ------------------------------------
    missing = [f for f in SOURCES if not os.path.exists(os.path.join(SRC_DIR, f))]
    if missing:
        sys.exit(f"BATAL: file sumber tidak ditemukan di {SRC_DIR}\n"
                 + "\n".join(f"  - {m}" for m in missing))
    print(f"\nSumber: {SRC_DIR}")
    for f in SOURCES:
        p = os.path.join(SRC_DIR, f)
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        print(f"  {f:20} {os.path.getsize(p) / 1e6:6.1f} MB   diubah {ts:%Y-%m-%d %H:%M}")
        try:                                    # catches a file still open in Excel
            with open(p, "rb") as fh:
                fh.read(4)
        except OSError as e:
            sys.exit(f"BATAL: {f} tidak dapat dibaca ({e}). Tutup file di Excel.")

    before = counts(DB)
    if before:
        print(f"\nDatabase sekarang: {before.get('peserta')} peserta, "
              f"{before.get('nilai')} nilai, {before.get('skl')} SKL"
              f"  (ujian terakhir {before.get('ujian_terakhir')})")

    # 2. build into a scratch file ---------------------------------------
    for stale in (NEW, NEW + "-journal"):
        if os.path.exists(stale):
            os.remove(stale)
    print("\nMembangun database baru (sekitar 90 detik)...\n" + "-" * 62)
    r = subprocess.run([sys.executable, os.path.join(HERE, "etl.py"), "--out", NEW],
                       cwd=HERE)
    print("-" * 62)
    if r.returncode != 0:
        if os.path.exists(NEW):
            os.remove(NEW)
        sys.exit("\nBATAL: etl.py gagal. Database lama TIDAK diubah.")

    # 3. compare against what is running now ------------------------------
    after = counts(NEW)
    print("\nPerbandingan:")
    print(f"  {'tabel':16} {'lama':>10} {'baru':>10} {'selisih':>10}")
    suspicious = []
    for t in COUNTED + ["lulus"]:
        old, new = before.get(t), after.get(t)
        if new is None:
            sys.exit(f"BATAL: tabel {t} tidak ada di database baru.")
        if old is None:
            print(f"  {t:16} {'-':>10} {new:>10} {'baru':>10}")
            continue
        diff = new - old
        print(f"  {t:16} {old:>10} {new:>10} {diff:>+10}")
        if old and new < old * (1 - SHRINK_TOLERANCE):
            suspicious.append(f"{t} turun {old - new} baris ({100 * (old - new) / old:.1f}%)")
    print(f"\n  ujian terakhir : {before.get('ujian_terakhir')} -> "
          f"{after.get('ujian_terakhir')}")
    print(f"  cetak terakhir : {before.get('cetak_terakhir')} -> "
          f"{after.get('cetak_terakhir')}")

    if suspicious:
        print("\n!! PERINGATAN - data menyusut:")
        for w in suspicious:
            print(f"   - {w}")
        print("   Penyebab umum: workbook disimpan dengan filter aktif, atau\n"
              "   sheet terpotong. Periksa file sumber sebelum melanjutkan.")

    if args.check:
        os.remove(NEW)
        print("\n--check: database baru dibuang, ukp.db tidak diubah.")
        return

    if suspicious and not args.yes:
        if input("\nTetap lanjutkan? ketik 'ya' untuk konfirmasi: ").strip().lower() != "ya":
            os.remove(NEW)
            sys.exit("Dibatalkan. Database lama tetap dipakai.")
    elif not args.yes:
        if input("\nGanti ukp.db dengan yang baru? [y/N]: ").strip().lower() not in ("y", "ya"):
            os.remove(NEW)
            sys.exit("Dibatalkan. Database lama tetap dipakai.")

    # 4. swap, keeping the old file ---------------------------------------
    moved = carry_over_log(DB, NEW)
    if os.path.exists(DB):
        shutil.copy2(DB, BAK)
        print(f"\nCadangan disimpan: {os.path.basename(BAK)}")
    os.replace(NEW, DB)
    print(f"Database diperbarui: {DB}")
    if moved:
        print(f"Access log dipindahkan: {moved} baris")

    print("\nLangkah berikutnya:")
    print("  1. .venv\\Scripts\\python.exe test_app.py")
    print("  2. .venv\\Scripts\\python.exe app.py       (periksa di browser)")
    print("  3. git add ukp.db && git commit -m \"Update data\" && git push")
    print("  4. vercel deploy --prod --scope spp-service")
    print("\nJika ada masalah: copy ukp.db.bak ukp.db")


if __name__ == "__main__":
    main()
