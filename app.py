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


# ref_diklat comes from the CONS sheet, which is missing the host school and
# spells Binasena as BNS, so these two never resolve by join. Kept here rather
# than patched into ukp.db because etl.py rebuilds ref_diklat on every refresh.
DIKLAT_ALIAS = {
    "STIP": "Sekolah Tinggi Ilmu Pelayaran Jakarta",
    "BINA SENA": "Akademi Maritim Binasena",
}


def _diklats():
    """Training institutions that actually own exam records, biggest first."""
    return [r[0] for r in db().execute(
        "SELECT p.diklat FROM nilai n JOIN peserta p ON p.uc = n.uc"
        " WHERE COALESCE(p.diklat,'')<>''"
        " GROUP BY p.diklat ORDER BY COUNT(*) DESC")]


def _diklat_nama(code):
    if not code:
        return ""
    r = db().execute(
        "SELECT deskripsi FROM ref_diklat WHERE diklat=?", [code]).fetchone()
    return (r[0] if r and r[0] else "") or DIKLAT_ALIAS.get(code, "")


@app.route("/api/stats")
def api_stats():
    """Aggregates for the dashboard.

    ?year=2026|all  ?basis=ujian|sidang  ?diklat=STIP (empty = all institutions)

    The peserta join is added only when a diklat is chosen: 18 of 41,990 nilai
    rows have no matching peserta, and joining unconditionally would silently
    drop them from the unfiltered headline figures.
    """
    year = request.args.get("year", "all")
    basis = "tgl_sidang" if request.args.get("basis") == "sidang" else "tgl_ujian"
    diklat = (request.args.get("diklat") or "").strip()
    if diklat and diklat not in _diklats():
        diklat = ""                            # unknown value: ignore, don't 500

    join = " JOIN peserta p ON p.uc = n.uc" if diklat else ""
    nwhere, args = f"n.{basis} IS NOT NULL", []
    if year != "all":
        nwhere += f" AND substr(n.{basis},1,4)=?"
        args.append(year)
    if diklat:
        nwhere += " AND p.diklat=?"
        args.append(diklat)

    # Same institution filter, no year filter: the annual series is a trend line.
    awhere, aargs = f"n.{basis} IS NOT NULL", []
    if diklat:
        awhere += " AND p.diklat=?"
        aargs.append(diklat)

    rows = lambda sql, a: [dict(r) for r in db().execute(sql, a)]

    monthly = rows(
        f"SELECT substr(n.{basis},1,7) bulan, COUNT(*) total,"
        f" SUM(n.lulus) lulus FROM nilai n{join} WHERE {nwhere}"
        f" GROUP BY bulan ORDER BY bulan", args)
    annual = rows(
        f"SELECT substr(n.{basis},1,4) tahun, COUNT(*) total, SUM(n.lulus) lulus"
        f" FROM nilai n{join} WHERE {awhere} GROUP BY tahun ORDER BY tahun", aargs)
    per_ijzh = rows(
        f"SELECT COALESCE(NULLIF(n.ijzh,''),'(kosong)') ijzh, COUNT(*) total,"
        f" SUM(n.lulus) lulus, r.deskripsi"
        f" FROM nilai n{join} LEFT JOIN ref_ijzh r ON r.ijzh = n.ijzh"
        f" WHERE {nwhere}"
        f" GROUP BY n.ijzh ORDER BY total DESC", args)
    # Never filtered by institution: this is the cross-institution comparison,
    # so it must keep every bar even while the rest of the page is narrowed.
    dwhere, dargs = f"n.{basis} IS NOT NULL", []
    if year != "all":
        dwhere += f" AND substr(n.{basis},1,4)=?"
        dargs.append(year)
    per_diklat = rows(
        f"SELECT COALESCE(NULLIF(p.diklat,''),'(kosong)') diklat, COUNT(*) total,"
        f" SUM(n.lulus) lulus, COUNT(DISTINCT n.uc) peserta, d.deskripsi"
        f" FROM nilai n JOIN peserta p ON p.uc = n.uc"
        f" LEFT JOIN ref_diklat d ON d.diklat = p.diklat"
        f" WHERE {dwhere}"
        f" GROUP BY p.diklat ORDER BY total DESC", dargs)
    for r in per_diklat:
        if not r["deskripsi"]:
            r["deskripsi"] = DIKLAT_ALIAS.get(r["diklat"], "")
    # Count distinct uc, not rows: 1,115 uc own several SKL rows (reprints),
    # which would otherwise inflate the printed figure.
    sjoin = " JOIN peserta p ON p.uc = s.uc" if diklat else ""
    skl_where, skl_args = "1=1", []
    if year != "all":
        skl_where = "substr(s.tgl_cetak,1,4)=?"
        skl_args.append(year)
    if diklat:
        skl_where += " AND p.diklat=?"
        skl_args.append(diklat)
    skl = db().execute(
        f"SELECT COUNT(DISTINCT s.uc) total,"
        f" COUNT(DISTINCT CASE WHEN s.tgl_cetak IS NOT NULL THEN s.uc END) cetak"
        f" FROM skl s{sjoin} WHERE {skl_where}", skl_args).fetchone()
    tot = db().execute(
        f"SELECT COUNT(*) total, SUM(n.lulus) lulus,"
        f" COUNT(DISTINCT n.uc) peserta FROM nilai n{join}"
        f" WHERE {nwhere}", args).fetchone()

    return {
        "year": year, "basis": request.args.get("basis", "ujian"),
        "diklat": diklat,
        "years": _years(), "diklats": _diklats(),
        "diklat_nama": _diklat_nama(diklat),
        "total": dict(tot), "skl": dict(skl),
        "monthly": monthly, "annual": annual,
        "per_ijzh": per_ijzh, "per_diklat": per_diklat,
    }


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", diklats=_diklats())


# Loaded once at import: the guide is static reference data rebuilt only when
# build_kompetensi.py is re-run, so re-reading it per request buys nothing.
def _load_kompetensi():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kompetensi.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


KOMPETENSI = _load_kompetensi()


@app.route("/kompetensi")
def kompetensi():
    if not KOMPETENSI:
        return render_template(
            "error.html", pesan="Daftar kompetensi belum tersedia."), 503
    log("kompetensi", "ok")
    return render_template("kompetensi.html", k=KOMPETENSI)


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


PER_PAGE = 100            # rows per page in the browser
FILTER_XLSX_MAX = 5000    # hard ceiling on a single export

STATUS_OPTS = [
    ("semua", "Semua status"),
    ("lulus", "Sudah lulus"),
    ("belum_lulus", "Belum lulus"),
    ("belum_mengulang", "Belum lulus & belum mengulang"),
    ("belum_skl", "Lulus tapi SKL belum terbit"),
]
STATUS_LABEL = dict(STATUS_OPTS)


def filter_opts():
    """Dropdown contents read from the data, so they cannot drift from it."""
    return {
        "diklat": [r[0] for r in db().execute(
            "SELECT DISTINCT diklat FROM peserta WHERE COALESCE(diklat,'')<>''"
            " ORDER BY diklat")],
        "ijzh": [r[0] for r in db().execute(
            "SELECT DISTINCT ijzh FROM peserta WHERE COALESCE(ijzh,'')<>''"
            " ORDER BY ijzh")],
        "tahun": [r[0] for r in db().execute(
            "SELECT DISTINCT substr(tgl_ujian,1,4) y FROM nilai"
            " WHERE tgl_ujian IS NOT NULL ORDER BY y DESC")],
        "status": STATUS_OPTS,
    }


def _filter_sql(diklat="", ijzh="", tahun="", status="semua"):
    """Build the filter query once, so the row fetch and the count can never
    disagree about what matches."""
    where, args = ["1=1"], []
    if diklat:
        where.append("p.diklat=?")
        args.append(diklat)
    if ijzh:
        where.append("p.ijzh=?")
        args.append(ijzh)

    having, hargs = [], []
    if status == "lulus":
        having.append("SUM(n.lulus)>0")
    elif status in ("belum_lulus", "belum_mengulang"):
        having.append("SUM(n.lulus)=0")
    elif status == "belum_skl":
        having.append("SUM(n.lulus)>0 AND s.skl_n IS NULL")
    if tahun:
        having.append("substr(MAX(n.tgl_ujian),1,4)=?")
        hargs.append(tahun)

    inner = f"""
        SELECT p.uc, p.sc, p.nama, p.tpt_lahir, p.tgl_lahir, p.dob_usable,
               p.diklat, p.ijzh, COUNT(n.id) att, MAX(n.tgl_ujian) last_ex,
               SUM(n.lulus) pass_n, s.cetak, s.skl_n
        FROM peserta p JOIN nilai n ON n.uc = p.uc
        LEFT JOIN (SELECT uc, MAX(tgl_cetak) cetak, COUNT(*) skl_n
                   FROM skl GROUP BY uc) s ON s.uc = p.uc
        WHERE {' AND '.join(where)}
        GROUP BY p.uc
        {'HAVING ' + ' AND '.join(having) if having else ''}"""

    if status == "belum_mengulang":
        # No later exam at the same level, counting re-registrations too.
        body = f"""SELECT * FROM ({inner}) f WHERE NOT EXISTS (
                     SELECT 1 FROM peserta p2 JOIN nilai n2 ON n2.uc = p2.uc
                     WHERE p2.sc = f.sc AND p2.ijzh = f.ijzh
                       AND n2.tgl_ujian > f.last_ex)"""
        order = " ORDER BY f.last_ex DESC, f.nama, f.uc"
    else:
        body = f"SELECT * FROM ({inner})"
        order = " ORDER BY last_ex DESC, nama, uc"
    return body, order, args + hargs


def filter_rows(diklat="", ijzh="", tahun="", status="semua",
                limit=FILTER_XLSX_MAX, offset=0):
    """Registrations (one row per seafarer per ijazah level) matching a filter.

    The unit is the registration, not the person: someone who failed two levels
    is two rows, which is what a follow-up list needs. 'belum_mengulang' counts
    a retake whether it came back as another attempt on the same registration
    or as a fresh re-registration, so nobody who did return is listed.

    Sorting ends in uc, which is unique. Without that tiebreak SQLite may order
    equal (last_ex, nama) pairs differently between queries, and a paged reader
    would see a row twice or miss it entirely."""
    body, order, a = _filter_sql(diklat, ijzh, tahun, status)
    sql = body + order + " LIMIT ? OFFSET ?"

    ref = {r["ijzh"]: r["deskripsi"] for r in db().execute("SELECT * FROM ref_ijzh")}
    out = []
    for r in db().execute(sql, a + [limit, offset]):
        lulus = bool(r["pass_n"])
        step = {"cetak": r["cetak"], "has_skl": bool(r["skl_n"]), "lulus": lulus}
        out.append({
            "sc": r["sc"], "nama": r["nama"], "diklat": r["diklat"] or "",
            "ijzh": r["ijzh"], "ijzh_nama": ref.get(r["ijzh"], ""),
            "ttl": ", ".join(x for x in (
                r["tpt_lahir"], r["tgl_lahir"] if r["dob_usable"] else None) if x) or "-",
            "att": r["att"], "last_ex": r["last_ex"] or "",
            "lulus": lulus, "status": "LULUS" if lulus else "BELUM LULUS",
            "skl_status": skl_label(step), "skl_tgl": r["cetak"] or "",
        })
    return out


def filter_count(diklat="", ijzh="", tahun="", status="semua"):
    """Total matches. Counts in SQL rather than fetching every row, because
    this now runs on every page view, not only when the list truncates."""
    body, _order, a = _filter_sql(diklat, ijzh, tahun, status)
    return db().execute(f"SELECT COUNT(*) FROM ({body})", a).fetchone()[0]


def _xlsx(title, head, widths, data, notes=()):
    """One formatted sheet in a workbook. Shared by both export paths."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Rekap"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13, color="0E1937")
    ws.append([f"Dibuat {datetime.datetime.now():%Y-%m-%d %H:%M} "
               f"\u00b7 {len(data)} baris \u00b7 PUKP-3 Wilayah I Jakarta"])
    ws["A2"].font = Font(italic=True, size=9, color="5B6B85")
    ws.append([])
    ws.append(head)
    hr = ws.max_row
    for row in data:
        ws.append(row)

    fill = PatternFill("solid", fgColor="0E1937")
    for c in ws[hr]:
        c.font = Font(bold=True, color="FFFFFF", size=10)
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[hr].height = 30
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = f"A{hr + 1}"
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(head))}{ws.max_row}"

    for n in notes:
        ws.append([])
        ws.append([n])
        ws.cell(ws.max_row, 1).font = Font(italic=True, size=9, color="5B6B85")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _filter_title(f):
    bits = [STATUS_LABEL.get(f.get("status") or "semua", "")]
    for k, pre in (("diklat", ""), ("ijzh", "tingkat "), ("tahun", "tahun ")):
        if f.get(k):
            bits.append(pre + f[k])
    return "REKAP PESERTA - " + " \u00b7 ".join(b for b in bits if b).upper()


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


@app.route("/filter", methods=["GET", "POST"])
@admin_only
def filter_view():
    """Build a list from dropdowns instead of pasted codes."""
    f = {k: (request.values.get(k) or "").strip()
         for k in ("diklat", "ijzh", "tahun")}
    f["status"] = (request.values.get("status") or "semua").strip()
    if f["status"] not in STATUS_LABEL:
        f["status"] = "semua"

    opts = filter_opts()
    if request.method == "GET" and not request.args:
        return render_template("filter.html", opts=opts, f=f)

    total = filter_count(**f)
    pages = max(1, -(-total // PER_PAGE))          # ceiling division
    try:
        page = int(request.values.get("page") or 1)
    except ValueError:
        page = 1
    page = max(1, min(page, pages))                # clamp, never 404 on a stale link

    rows = filter_rows(**f, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    log("filter", "ok", q=f"{f['status']}/{f['diklat']}/{f['ijzh']}/{f['tahun']} p{page}")
    return render_template(
        "filter.html", opts=opts, f=f, rows=rows, total=total,
        page=page, pages=pages, per_page=PER_PAGE,
        first=(page - 1) * PER_PAGE + 1, last=(page - 1) * PER_PAGE + len(rows),
        xlsx_max=FILTER_XLSX_MAX)


@app.route("/filter.xlsx", methods=["POST"])
@admin_only
def filter_xlsx():
    f = {k: (request.form.get(k) or "").strip()
         for k in ("diklat", "ijzh", "tahun")}
    f["status"] = (request.form.get("status") or "semua").strip()
    if f["status"] not in STATUS_LABEL:
        f["status"] = "semua"

    rows = filter_rows(**f, limit=FILTER_XLSX_MAX)
    if not rows:
        return redirect(url_for("filter_view", **f))

    head = ["No", "Kode Pelaut", "Nama Lengkap", "Tempat, Tanggal Lahir",
            "Lembaga Diklat", "Tingkat Ijazah", "Keterangan Ijazah",
            "Jumlah Ujian", "Ujian Terakhir", "Status Kelulusan",
            "Status SKL", "Tanggal Cetak SKL"]
    widths = [5, 14, 30, 26, 13, 10, 42, 8, 13, 15, 24, 15]
    data = [[i, r["sc"], r["nama"], r["ttl"], r["diklat"], r["ijzh"],
             r["ijzh_nama"], r["att"], r["last_ex"], r["status"],
             r["skl_status"], r["skl_tgl"]]
            for i, r in enumerate(rows, 1)]

    notes = [
        "Belum lulus = seluruh percobaan pada registrasi tersebut tidak lulus "
        "(ada materi di bawah 70).",
        "Belum mengulang = tidak ada ujian berikutnya pada tingkat ijazah yang "
        "sama, baik sebagai ulangan maupun registrasi baru.",
        "Satu baris = satu registrasi tingkat ijazah; peserta dapat muncul "
        "lebih dari sekali bila menempuh lebih dari satu tingkat.",
    ]
    if len(rows) >= FILTER_XLSX_MAX:
        notes.insert(0, f"PERHATIAN: hasil dipotong pada {FILTER_XLSX_MAX} baris. "
                        f"Persempit filter untuk data lengkap.")

    buf = _xlsx(_filter_title(f), head, widths, data, notes)
    bits = [f[k] for k in ("status", "diklat", "ijzh", "tahun") if f[k]]
    log("filter", "export", q=f"{len(rows)} rows")
    return send_file(
        buf, as_attachment=True,
        download_name=f"rekap-{'-'.join(bits) or 'semua'}-"
                      f"{datetime.date.today():%Y%m%d}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    if not os.path.exists(DB):
        raise SystemExit("ukp.db not found - run: python etl.py")
    app.run(host="127.0.0.1", port=5057, debug=True)
