"""UKP self-service lookup. Run: python app.py  (needs ukp.db from etl.py)"""
import datetime
import hmac
import json
import os
import re
import secrets
import sqlite3

from flask import (Flask, abort, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ukp.db")
# Vercel: serverless functions have a read-only deploy filesystem.
# access_log writes go to /tmp so they survive within a warm instance
# but are lost on cold start. Rate limiting is best-effort on Vercel.
LOG_DB = os.path.join(os.environ.get("TMPDIR", "/tmp"), "access_log.db") \
    if os.environ.get("VERCEL") else DB

FAIL_LIMIT = 8          # failed verifications per IP
SC_FAIL_LIMIT = 10      # ...and per seafarer code, regardless of source IP
WINDOW_MIN = 15         # ...within this many minutes
MAX_RESULTS = 25
BATCH_MAX = 30          # seafarer codes per admin batch lookup

ADMIN_USER = os.environ.get("UKP_ADMIN_USER", "admin")
# Hash only - never the password itself, this repo is public. Generate with:
#   python -c "from werkzeug.security import generate_password_hash as h; \
#              print(h(input('password: ')))"
# then set UKP_ADMIN_HASH in the environment (and on Vercel).
# Local dev reads .env.local, which is gitignored.
if os.path.exists(os.path.join(HERE, ".env.local")):
    for _line in open(os.path.join(HERE, ".env.local"), encoding="utf-8"):
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ADMIN_HASH = os.environ.get("UKP_ADMIN_HASH", "")

app = Flask(__name__)
# ponytail: dev secret regenerates each boot (logs everyone out on restart).
# Set UKP_SECRET in the environment before deploying behind more than one worker.
# With CSRF on, a missing UKP_SECRET also means tokens minted by one Vercel
# lambda are rejected by the next one, so POSTs fail intermittently.
app.secret_key = os.environ.get("UKP_SECRET") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Local dev is plain http; only demand TLS where it actually exists.
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)


def csrf_token():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    """Reject cross-site POSTs. Without this, a hidden form on any page can
    spend a visitor's verify attempts and lock their IP out of the service."""
    if request.method != "POST":
        return
    good = session.get("csrf")
    # Not compare_digest(sent, session.get("csrf", "")): that is True when both
    # are empty, so a session with no token would pass every check.
    if not good or not hmac.compare_digest(request.form.get("csrf", ""), good):
        log(request.endpoint or "?", "csrf_reject")
        abort(400)


@app.errorhandler(400)
def _stale_form(_):
    return render_template("error.html", pesan=(
        "Halaman sudah kedaluwarsa. Muat ulang halaman lalu coba lagi.")), 400


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


def log_db():
    """Writable connection for access_log. On Vercel this is /tmp; locally same as db()."""
    if "log_db" not in g:
        if LOG_DB == DB:
            return db()
        g.log_db = sqlite3.connect(LOG_DB)
        g.log_db.row_factory = sqlite3.Row
        g.log_db.execute(
            "CREATE TABLE IF NOT EXISTS access_log"
            " (ts TEXT, ip TEXT, ua TEXT, action TEXT, q TEXT,"
            "  sc TEXT, uc TEXT, outcome TEXT)")
    return g.log_db


@app.teardown_appcontext
def _close(_):
    if (c := g.pop("db", None)) is not None:
        c.close()
    if (c := g.pop("log_db", None)) is not None:
        c.close()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def log(action, outcome, q=None, sc=None, uc=None):
    try:
        log_db().execute(
            "INSERT INTO access_log (ts,ip,ua,action,q,sc,uc,outcome) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), client_ip(),
             request.headers.get("User-Agent", "")[:300], action, q, sc, uc, outcome),
        )
        log_db().commit()
    except sqlite3.OperationalError:
        pass  # read-only fs on Vercel cold start edge case


def throttled(sc=None):
    """True when this IP has failed too often, or when one seafarer code is
    being guessed at from anywhere. The per-sc limit matters because the
    per-IP counter is trivially reset with a new IP, and on Vercel it lives
    in per-instance /tmp that a cold start wipes."""
    since = (datetime.datetime.now()
             - datetime.timedelta(minutes=WINDOW_MIN)).isoformat(timespec="seconds")
    try:
        n = log_db().execute(
            "SELECT COUNT(*) FROM access_log WHERE ip=? AND ts>? AND outcome='bad_verify'",
            (client_ip(), since),
        ).fetchone()[0]
        if n >= FAIL_LIMIT:
            return True
        if sc:
            m = log_db().execute(
                "SELECT COUNT(*) FROM access_log"
                " WHERE sc=? AND ts>? AND outcome='bad_verify'", (sc, since),
            ).fetchone()[0]
            return m >= SC_FAIL_LIMIT
        return False
    except sqlite3.OperationalError:
        return False


def norm_date(t):
    """Accept 1996-06-05, 05/06/1996, 5 June 1996 -> ISO, else None."""
    t = (t or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(t, f).date().isoformat()
        except ValueError:
            pass
    return None


@app.context_processor
def nav():
    """Year list for the dashboard filter, available to every template."""
    return {"years": _years()}


@app.context_processor
def summary():
    """Feature 1: latest administrator input, shown on every page."""
    r = db().execute(
        "SELECT (SELECT COUNT(*) FROM peserta) p, (SELECT COUNT(*) FROM nilai) n,"
        " (SELECT COUNT(*) FROM skl) s, (SELECT MAX(tgl_ujian) FROM nilai) mu,"
        " (SELECT MAX(tgl_sidang) FROM nilai) ms, (SELECT MAX(tgl_cetak) FROM skl) mc"
    ).fetchone()
    return {"stat": r}


# ---------------------------------------------------------------- dashboard
def _years():
    return [r[0] for r in db().execute(
        "SELECT DISTINCT substr(tgl_ujian,1,4) y FROM nilai"
        " WHERE tgl_ujian IS NOT NULL ORDER BY y DESC")]


@app.route("/api/stats")
def api_stats():
    """Aggregates for the dashboard. ?year=2026 or ?year=all, ?basis=ujian|sidang"""
    year = request.args.get("year", "all")
    basis = "tgl_sidang" if request.args.get("basis") == "sidang" else "tgl_ujian"
    where, args = f"{basis} IS NOT NULL", []
    nwhere = f"n.{basis} IS NOT NULL"          # same filter, aliased for the join below
    if year != "all":
        where += f" AND substr({basis},1,4)=?"
        nwhere += f" AND substr(n.{basis},1,4)=?"
        args = [year]

    rows = lambda sql, a: [dict(r) for r in db().execute(sql, a)]

    monthly = rows(
        f"SELECT substr({basis},1,7) bulan, COUNT(*) total,"
        f" SUM(lulus) lulus FROM nilai WHERE {where}"
        f" GROUP BY bulan ORDER BY bulan", args)
    annual = rows(
        f"SELECT substr({basis},1,4) tahun, COUNT(*) total, SUM(lulus) lulus"
        f" FROM nilai WHERE {basis} IS NOT NULL GROUP BY tahun ORDER BY tahun", [])
    per_ijzh = rows(
        f"SELECT COALESCE(NULLIF(n.ijzh,''),'(kosong)') ijzh, COUNT(*) total,"
        f" SUM(n.lulus) lulus, r.deskripsi"
        f" FROM nilai n LEFT JOIN ref_ijzh r ON r.ijzh = n.ijzh"
        f" WHERE {nwhere}"
        f" GROUP BY n.ijzh ORDER BY total DESC", args)
    per_diklat = rows(
        f"SELECT COALESCE(NULLIF(p.diklat,''),'(kosong)') diklat, COUNT(*) total,"
        f" SUM(n.lulus) lulus FROM nilai n JOIN peserta p ON p.uc = n.uc"
        f" WHERE {nwhere}"
        f" GROUP BY p.diklat ORDER BY total DESC", args)
    # Count distinct uc, not rows: 1,115 uc own several SKL rows (reprints),
    # which would otherwise inflate the printed figure.
    skl_where = "1=1" if year == "all" else "substr(tgl_cetak,1,4)=?"
    skl_args = [] if year == "all" else [year]
    skl = db().execute(
        f"SELECT COUNT(DISTINCT uc) total,"
        f" COUNT(DISTINCT CASE WHEN tgl_cetak IS NOT NULL THEN uc END) cetak"
        f" FROM skl WHERE {skl_where}", skl_args).fetchone()
    tot = db().execute(
        f"SELECT COUNT(*) total, SUM(lulus) lulus,"
        f" COUNT(DISTINCT uc) peserta FROM nilai WHERE {where}", args).fetchone()

    return {
        "year": year, "basis": request.args.get("basis", "ujian"),
        "years": _years(),
        "total": dict(tot), "skl": dict(skl),
        "monthly": monthly, "annual": annual,
        "per_ijzh": per_ijzh, "per_diklat": per_diklat,
    }


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/cek", methods=["GET", "POST"])
def cek():
    if request.method == "GET":
        return render_template("cek.html")

    q = (request.form.get("q") or "").strip()
    if len(q) < 3:
        return render_template("cek.html", error="Masukkan minimal 3 karakter.")
    if throttled():
        log("search", "rate_limited", q=q)
        return render_template("cek.html", error=(
            f"Terlalu banyak percobaan gagal. Coba lagi dalam {WINDOW_MIN} menit."))

    if re.fullmatch(r"\d{6,}", q):
        rows = db().execute(
            "SELECT sc, nama, COUNT(*) n, GROUP_CONCAT(ijzh, ', ') levels"
            " FROM peserta WHERE sc=? GROUP BY sc, nama", (q,)
        ).fetchall()
    else:
        rows = db().execute(
            "SELECT sc, nama, COUNT(*) n, GROUP_CONCAT(ijzh, ', ') levels"
            " FROM peserta WHERE nama LIKE ?"
            " GROUP BY sc, nama ORDER BY nama LIMIT ?", (f"%{q.upper()}%", MAX_RESULTS + 1)
        ).fetchall()

    log("search", "ok" if rows else "not_found", q=q)
    if not rows:
        return render_template("cek.html", q=q, error="Data tidak ditemukan.")
    return render_template("cek.html", q=q, results=rows[:MAX_RESULTS],
                           truncated=len(rows) > MAX_RESULTS)


@app.route("/verify/<sc>", methods=["GET", "POST"])
def verify(sc):
    rows = db().execute("SELECT * FROM peserta WHERE sc=? ORDER BY ukp1", (sc,)).fetchall()
    if not rows:
        return redirect(url_for("index"))
    nama = rows[0]["nama"]

    if request.method == "GET":
        return render_template("verify.html", sc=sc, nama=nama)
    if throttled(sc):
        log("verify", "rate_limited", sc=sc)
        return render_template("verify.html", sc=sc, nama=nama, error=(
            f"Terlalu banyak percobaan gagal. Coba lagi dalam {WINDOW_MIN} menit."))

    # Either factor may match: TGLLAHIR is a placeholder or missing for ~15% of
    # records, and 5,414 seafarer codes carry more than one UKP1 date.
    dob = norm_date(request.form.get("tgl_lahir"))
    ukp1 = norm_date(request.form.get("ukp1"))
    ok = bool(
        (dob and any(r["tgl_lahir"] == dob and r["dob_usable"] for r in rows))
        or (ukp1 and any(r["ukp1"] == ukp1 for r in rows))
    )
    if not ok:
        log("verify", "bad_verify", sc=sc)
        return render_template("verify.html", sc=sc, nama=nama,
                               error="Data verifikasi tidak cocok.")

    session["sc"] = sc
    log("verify", "ok", sc=sc)
    return redirect(url_for("detail", sc=sc))


@app.route("/detail/<sc>")
def detail(sc):
    if session.get("sc") != sc and not is_admin():
        return redirect(url_for("verify", sc=sc))

    peserta = db().execute(
        "SELECT p.*, d.deskripsi diklat_nama FROM peserta p"
        " LEFT JOIN ref_diklat d ON d.diklat = p.diklat"
        " WHERE p.sc=? ORDER BY p.ukp1", (sc,)
    ).fetchall()

    ucs = [p["uc"] for p in peserta]
    marks = ",".join("?" * len(ucs))
    nilai = db().execute(
        f"SELECT n.*, r.deskripsi ijzh_nama, r.mu_names FROM nilai n"
        f" LEFT JOIN ref_ijzh r ON r.ijzh = n.ijzh"
        f" WHERE n.uc IN ({marks}) ORDER BY n.tgl_ujian DESC, n.mengulang_ke", ucs
    ).fetchall()
    # A uc can own several SKL rows (reprints). Iterate oldest-first so the
    # dict ends up holding the NEWEST one; rows with no tgl_cetak sort first
    # and never displace a printed one.
    skl_rows = db().execute(
        f"SELECT * FROM skl WHERE uc IN ({marks})"
        f" ORDER BY (tgl_cetak IS NOT NULL), tgl_cetak", ucs).fetchall()
    skl = {r["uc"]: r for r in skl_rows}
    skl_count = {}
    for r in skl_rows:
        skl_count[r["uc"]] = skl_count.get(r["uc"], 0) + 1

    # one card per ijazah level: its attempts, subject names, SKL status
    cards = []
    for p in peserta:
        att = [n for n in nilai if n["uc"] == p["uc"]]
        names = json.loads(att[0]["mu_names"] or "[]") if att and att[0]["mu_names"] else []
        # earliest exam date drives the chronology; fall back to ukp1 when a
        # level has no nilai rows yet so it still lands in the right slot
        dates = sorted(a["tgl_ujian"] for a in att if a["tgl_ujian"])
        cards.append({
            "p": p,
            "attempts": [dict(a, mu=json.loads(a["mu"])) for a in att],
            "mu_names": names,
            "skl": skl.get(p["uc"]),
            "skl_n": skl_count.get(p["uc"], 0),
            "lulus": any(a["lulus"] for a in att),
            "tgl": dates[0] if dates else (p["ukp1"] or ""),
            "tgl_akhir": dates[-1] if dates else (p["ukp1"] or ""),
            "n_ujian": len(att),
        })

    # chronological: the upgrade path reads oldest level -> newest
    cards.sort(key=lambda c: c["tgl"] or "9999")

    log("view", "ok", sc=sc, uc=",".join(ucs)[:200])
    return render_template("detail.html", sc=sc, nama=peserta[0]["nama"], cards=cards)


def is_admin():
    return bool(session.get("admin"))


app.jinja_env.globals["is_admin"] = is_admin


def admin_only(view):
    """Gate a route behind the admin session."""
    def wrapped(*a, **kw):
        if not is_admin():
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    wrapped.__name__ = view.__name__
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    if throttled():
        log("login", "rate_limited")
        return render_template("login.html", error=(
            f"Terlalu banyak percobaan gagal. Coba lagi dalam {WINDOW_MIN} menit."))

    user = (request.form.get("user") or "").strip()
    pw = request.form.get("pw") or ""
    if not ADMIN_HASH:
        log("login", "misconfigured")
        return render_template("login.html", error=(
            "Login admin belum dikonfigurasi (UKP_ADMIN_HASH belum diisi).")), 503
    # Same failure for wrong user and wrong password: telling them which one
    # was right confirms the username exists.
    if user != ADMIN_USER or not check_password_hash(ADMIN_HASH, pw):
        # reuse the bad_verify outcome so the existing rate limiter counts it
        log("login", "bad_verify")
        return render_template("login.html", error="Username atau password salah.")

    session.clear()          # new privilege level, new session id
    session["admin"] = True
    session["csrf"] = secrets.token_urlsafe(32)
    log("login", "ok")
    nxt = request.form.get("next") or request.args.get("next") or ""
    # only relative paths: an open redirect would let a phishing link bounce
    # through this domain
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                    else url_for("batch"))


def parse_codes(raw):
    """Free text -> de-duplicated seafarer codes, order preserved."""
    seen, out = set(), []
    for tok in re.split(r"[^0-9A-Za-z]+", raw or ""):
        tok = tok.strip().upper()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def batch_rows(codes):
    """One summary row per requested code, in the order asked."""
    rows = []
    for sc in codes:
        peserta = db().execute(
            "SELECT * FROM peserta WHERE sc=? ORDER BY ukp1", (sc,)).fetchall()
        if not peserta:
            rows.append({"sc": sc, "found": False})
            continue

        ucs = [p["uc"] for p in peserta]
        marks = ",".join("?" * len(ucs))
        nilai = db().execute(
            f"SELECT uc, ijzh, tgl_ujian, lulus FROM nilai WHERE uc IN ({marks})", ucs
        ).fetchall()
        skl = db().execute(
            f"SELECT uc, tgl_cetak FROM skl WHERE uc IN ({marks})"
            " ORDER BY tgl_cetak IS NOT NULL, tgl_cetak", ucs).fetchall()
        newest = {}
        for s in skl:                      # oldest-first, so newest wins
            newest[s["uc"]] = s["tgl_cetak"]

        # chronological upgrade path, same rule as the detail page
        by_uc = {}
        for n in nilai:
            by_uc.setdefault(n["uc"], []).append(n)
        steps = []
        for p in peserta:
            att = by_uc.get(p["uc"], [])
            dates = sorted(a["tgl_ujian"] for a in att if a["tgl_ujian"])
            steps.append({
                "ijzh": p["ijzh"],
                "tgl": dates[0] if dates else (p["ukp1"] or ""),
                "lulus": any(a["lulus"] for a in att),
                "cetak": newest.get(p["uc"]),
                "has_skl": p["uc"] in newest,
            })
        steps.sort(key=lambda s: s["tgl"] or "9999")

        last = steps[-1] if steps else {}
        p0 = peserta[0]
        ttl = " / ".join(x for x in (p0["tpt_lahir"],
                                     p0["tgl_lahir"] if p0["dob_usable"] else None) if x)
        rows.append({
            "sc": sc, "found": True, "nama": p0["nama"],
            "ttl": ttl or "-",
            "tpt_lahir": p0["tpt_lahir"] or "",
            "tgl_lahir": p0["tgl_lahir"] if p0["dob_usable"] else "",
            "diklat": p0["diklat"] or "",
            "riwayat": " > ".join(s["ijzh"] for s in steps),
            "n_level": len(steps),
            "n_ujian": len(nilai),
            "ijzh_akhir": last.get("ijzh", ""),
            "tgl_akhir": last.get("tgl", ""),
            "lulus_akhir": last.get("lulus", False),
            "skl_status": skl_label(last),
            "skl_tgl": last.get("cetak") or "",
        })
    return rows


def skl_label(step):
    """Same four states the detail page shows, as plain text for Excel."""
    if not step:
        return "-"
    if step.get("cetak"):
        return "SUDAH DICETAK"
    if step.get("has_skl"):
        return "TERDAFTAR, BELUM DICETAK"
    return "BELUM TERBIT" if step.get("lulus") else "TIDAK ADA"


@app.route("/batch", methods=["GET", "POST"])
@admin_only
def batch():
    if request.method == "GET":
        return render_template("batch.html", maks=BATCH_MAX)

    raw = request.form.get("codes") or ""
    codes = parse_codes(raw)
    if not codes:
        return render_template("batch.html", maks=BATCH_MAX,
                               error="Masukkan minimal satu kode pelaut.", raw=raw)
    over = len(codes) > BATCH_MAX
    codes = codes[:BATCH_MAX]
    rows = batch_rows(codes)
    log("batch", "ok", q=f"{len(codes)} codes")
    return render_template(
        "batch.html", maks=BATCH_MAX, rows=rows, raw=raw,
        ketemu=sum(1 for r in rows if r["found"]),
        error=(f"Lebih dari {BATCH_MAX} kode; hanya {BATCH_MAX} pertama diproses."
               if over else None))


@app.route("/batch.xlsx", methods=["POST"])
@admin_only
def batch_xlsx():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    codes = parse_codes(request.form.get("codes") or "")[:BATCH_MAX]
    if not codes:
        return redirect(url_for("batch"))
    rows = batch_rows(codes)

    wb = Workbook()
    ws = wb.active
    ws.title = "Rekap Peserta"
    head = ["No", "Kode Pelaut", "Nama Lengkap", "Tempat Lahir", "Tanggal Lahir",
            "Jenis Kelamin", "Riwayat Tingkat Ijazah", "Ijazah Terakhir",
            "Tanggal Ujian Terakhir", "Status SKL", "Tanggal Cetak SKL",
            "Jumlah Ujian", "Diklat"]
    ws.append(head)
    for i, r in enumerate(rows, 1):
        if not r["found"]:
            ws.append([i, r["sc"], "TIDAK DITEMUKAN"] + [""] * (len(head) - 3))
            continue
        ws.append([
            i, r["sc"], r["nama"], r["tpt_lahir"], r["tgl_lahir"],
            "",                      # Jenis Kelamin: not in the source workbooks
            r["riwayat"], r["ijzh_akhir"], r["tgl_akhir"],
            r["skl_status"], r["skl_tgl"], r["n_ujian"], r["diklat"],
        ])

    hdr_fill = PatternFill("solid", fgColor="0E1937")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    widths = [5, 14, 30, 18, 13, 13, 30, 14, 15, 24, 15, 11, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    note = ws.max_row + 2
    ws.cell(note, 1, "Jenis Kelamin tidak tersedia pada data sumber "
                     "(DataPeserta tidak memuat kolom tersebut).").font = Font(italic=True, size=9)
    ws.cell(note + 1, 1, f"Dibuat {datetime.datetime.now():%Y-%m-%d %H:%M} "
                         f"- PUKP-3 Wilayah I Jakarta").font = Font(italic=True, size=9)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log("batch", "export", q=f"{len(codes)} codes")
    return send_file(
        buf, as_attachment=True,
        download_name=f"rekap-peserta-{datetime.date.today():%Y%m%d}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    if not os.path.exists(DB):
        raise SystemExit("ukp.db not found - run: python etl.py")
    app.run(host="127.0.0.1", port=5057, debug=True)
