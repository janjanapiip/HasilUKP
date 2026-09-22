# Aplikasi Cek Data UKP — PUKP-3 Wilayah I Jakarta

Aplikasi web self-service untuk peserta Ujian Keahlian Pelaut (UKP).
Peserta dapat melihat riwayat ujian, nilai, dan status Surat Keterangan Lulus (SKL)
tanpa perlu menghubungi administrator.

---

## Daftar Isi

1. [Persyaratan Sistem](#1-persyaratan-sistem)
2. [Instalasi Awal](#2-instalasi-awal)
3. [Menjalankan Aplikasi](#3-menjalankan-aplikasi)
4. [Memperbarui Data dari Excel](#4-memperbarui-data-dari-excel)
5. [Menjalankan Self-Test](#5-menjalankan-self-test)
6. [Struktur File](#6-struktur-file)
7. [Fitur Aplikasi](#7-fitur-aplikasi)
8. [Dashboard Statistik](#8-dashboard-statistik)
9. [Alur Pengguna (End-User)](#9-alur-pengguna-end-user)
10. [Keamanan & Rate Limiting](#10-keamanan--rate-limiting)
11. [Access Log & Pelacakan Penggunaan](#11-access-log--pelacakan-penggunaan)
12. [Exceptions (Data Tidak Terpetakan)](#12-exceptions-data-tidak-terpetakan)
13. [Konfigurasi Production](#13-konfigurasi-production)
14. [Keterangan Singkatan Tingkat Ijazah](#14-keterangan-singkatan-tingkat-ijazah)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Persyaratan Sistem

- **Python 3.11+** (versi lain mungkin jalan, belum diuji)
- **pip** (bawaan Python)
- **Node.js** (hanya untuk menjalankan `test_dashboard.js`, opsional)
- Tidak perlu database server — data tersimpan di **SQLite** (`ukp.db`)

---

## 2. Instalasi Awal

Jalankan dari folder `Project App UKP`:

```bash
cd "D:\PUKP-3\Project App UKP"

# Buat virtual environment
python -m venv .venv

# Aktifkan (Windows CMD)
.venv\Scripts\activate

# Aktifkan (Git Bash / MSYS)
source .venv/Scripts/activate

# Install dependensi
pip install flask openpyxl
```

Dependensi hanya dua: `flask` (web server) dan `openpyxl` (baca Excel saat ETL).

---

## 3. Menjalankan Aplikasi

### Langkah 1 — Pastikan `ukp.db` ada

Jika belum ada, jalankan ETL dulu (lihat [Bagian 4](#4-memperbarui-data-dari-excel)).

### Langkah 2 — Jalankan server

```bash
cd "D:\PUKP-3\Project App UKP"
.venv\Scripts\python.exe app.py
```

Output:
```
 * Running on http://127.0.0.1:5057
```

### Langkah 3 — Buka di browser

Buka **http://127.0.0.1:5057** di browser.

### Menghentikan server

Tekan `Ctrl+C` di terminal.

---

## 4. Memperbarui Data dari Excel

**Jawaban singkat: ya, cukup ganti file Excel-nya.** Aplikasi tidak pernah
membaca Excel saat melayani pengguna — semua data sudah dipindahkan ke
`ukp.db`. Jadi memperbarui data = menimpa file Excel lama, lalu membangun
ulang `ukp.db`.

### 4.1 Cara yang dianjurkan (paling aman)

```bash
cd "D:\PUKP-3\Project App UKP"
.venv\Scripts\python.exe update_data.py
```

Satu perintah ini menjalankan seluruh proses dengan pengaman:

1. memeriksa kedua file sumber ada dan bisa dibaca (menolak jika file
   masih terbuka di Excel),
2. membangun database baru ke file **sementara** (`ukp.db.new`) —
   `ukp.db` yang sedang dipakai tidak disentuh sama sekali,
3. membandingkan jumlah baris lama vs baru dan menampilkannya,
4. **menolak melanjutkan** jika ada tabel menyusut lebih dari 2%,
5. meminta konfirmasi, lalu menukar file dan menyimpan cadangan
   `ukp.db.bak`.

Jika terjadi kegagalan di langkah mana pun, `ukp.db` lama tetap utuh dan
aplikasi terus berjalan seperti biasa.

Contoh keluaran:

```
Sumber: D:\PUKP-3\v2026
  ukp_v2021.xlsx         18.6 MB   diubah 2026-09-03 16:42
  ukp_v2026.xlsx          5.6 MB   diubah 2026-09-15 08:11

Database sekarang: 32813 peserta, 41137 nilai, 8132 SKL  (ujian terakhir 2026-08-10)

Membangun database baru (sekitar 90 detik)...
...
Perbandingan:
  tabel                  lama       baru    selisih
  peserta               32813      33120       +307
  nilai                 41137      41890       +753
  skl                    8132       8340       +208
  ref_ijzh                 45         45         +0
  ref_diklat               12         12         +0
  lulus                 29505      30012       +507

  ujian terakhir : 2026-08-10 -> 2026-09-14
  cetak terakhir : 2026-08-27 -> 2026-09-12

Ganti ukp.db dengan yang baru? [y/N]:
```

**Yang harus diperiksa sebelum mengetik `y`:** angka pada kolom *selisih*
harus **positif atau nol**, dan *ujian terakhir* harus maju ke tanggal yang
lebih baru. Kalau tidak, ada yang salah dengan file sumber — lihat
[4.6](#46-bila-ada-peringatan-data-menyusut).

### 4.2 Opsi perintah

| Perintah | Kegunaan |
|---|---|
| `update_data.py` | proses normal, dengan konfirmasi |
| `update_data.py --check` | **uji coba**: bangun, bandingkan, lalu buang. `ukp.db` tidak diubah sama sekali. Gunakan ini untuk melihat dampak file Excel baru tanpa risiko |
| `update_data.py --yes` | tanpa tanya-jawab (untuk skrip terjadwal) |

Jalankan `--check` dulu bila ragu. Tidak ada efek samping apa pun.

### 4.3 Langkah lengkap saat menerima file Excel baru

1. **Cadangkan file lama** (sekali saja, untuk berjaga-jaga):

   ```bash
   cd "D:\PUKP-3\v2026"
   copy ukp_v2026.xlsx ukp_v2026_backup.xlsx
   ```

2. **Timpa file lama dengan yang baru.** Nama file **harus tetap sama**:
   `ukp_v2026.xlsx` dan `ukp_v2021.xlsx`. Menyimpan dengan nama lain
   (`ukp_v2026 (1).xlsx`, `ukp_v2026 REVISI.xlsx`) membuat ETL tetap membaca
   file lama tanpa pesan kesalahan apa pun.

3. **Tutup Excel.** File yang masih terbuka mengunci pembacaan di Windows.

4. **Jalankan pembaruan:**

   ```bash
   cd "D:\PUKP-3\Project App UKP"
   .venv\Scripts\python.exe update_data.py
   ```

5. **Uji:**

   ```bash
   .venv\Scripts\python.exe test_app.py
   .venv\Scripts\python.exe test_filter.py
   ```

   Keduanya harus berakhir dengan `all checks passed` / `all cross-checks passed`.

6. **Periksa di browser:**

   ```bash
   .venv\Scripts\python.exe app.py
   ```

   Buka <http://127.0.0.1:5057>, pastikan jumlah di dashboard bertambah dan
   cari satu peserta dari angkatan terbaru.

7. **Unggah ke situs publik** (bila perlu):

   ```bash
   git add ukp.db exceptions.csv
   git commit -m "Update data per September 2026"
   git push
   vercel deploy --prod --scope spp-service
   ```

   > `ukp.db` ikut disimpan di repositori karena Vercel tidak punya
   > penyimpanan permanen. Tanpa langkah ini, situs publik tetap memakai data
   > lama walaupun database lokal sudah diperbarui.

### 4.4 Syarat file Excel

Struktur workbook **tidak boleh berubah**. ETL membaca berdasarkan posisi
kolom, bukan nama judulnya.

| Sheet | Baris data mulai | Isi |
|---|---|---|
| `DataPeserta` | 2 | identitas peserta |
| `DNILAI` | 2 | nilai per materi ujian |
| `DataSKL` | 6 | penerbitan & cetak SKL |
| `CONS` | 4 | tabel referensi ijazah & diklat |

Yang **aman** dilakukan pada file Excel:

- menambah baris data baru di bawah,
- memperbaiki isi sel yang salah,
- menambah sheet baru (diabaikan ETL).

Yang **merusak** ETL:

- menyisipkan/menghapus/memindahkan **kolom**,
- mengubah urutan sheet atau menamainya ulang,
- menyimpan dengan **filter aktif** lalu menghapus baris tersembunyi,
- menyimpan sebagai `.xls` atau `.csv` (harus tetap `.xlsx`).

Tanggal boleh ditulis bebas — ETL mengenali format Indonesia
(`04-OKT-21`, `06 Juli 2026`) maupun ISO (`2026-07-06`).

### 4.5 Kalau hasilnya salah — cara mundur

Setiap pembaruan menyimpan `ukp.db.bak`. Untuk kembali ke data sebelumnya:

```bash
cd "D:\PUKP-3\Project App UKP"
copy ukp.db.bak ukp.db
```

Hentikan server dulu bila sedang berjalan. Tidak ada langkah lain —
aplikasi langsung memakai data lama begitu dijalankan ulang.

### 4.6 Bila ada peringatan "data menyusut"

```
!! PERINGATAN - data menyusut:
   - peserta turun 32813 baris (50.0%)
```

Artinya database baru punya jauh lebih sedikit baris daripada yang sedang
dipakai. Data ujian **tidak pernah berkurang**, jadi ini hampir selalu
masalah pada file sumber:

| Penyebab | Cara periksa |
|---|---|
| Workbook disimpan dengan filter aktif | Buka Excel → Data → Clear Filter → simpan ulang |
| Sheet terpotong saat copy-paste | Bandingkan jumlah baris `DataPeserta` dengan versi lama |
| File tertukar (mis. hanya berisi satu angkatan) | Periksa ukuran file — `ukp_v2021.xlsx` ± 18 MB, `ukp_v2026.xlsx` ± 5 MB |
| Kolom bergeser | Buka `exceptions.csv`, bila melonjak drastis berarti kunci tidak terbaca |

Jangan ketik `ya` sebelum penyebabnya jelas. Menjawab apa pun selain `ya`
membatalkan proses tanpa mengubah `ukp.db`.

### 4.7 Yang ikut terjaga saat pembaruan

- **`access_log`** (riwayat pemakaian aplikasi) disalin ke database baru.
  Data ini tidak ada di Excel, jadi tanpa penyalinan ia akan hilang setiap
  kali ETL dijalankan.
- **`ukp.db.bak`** selalu ditimpa dengan versi *sebelum* pembaruan terakhir.
  Hanya menyimpan satu generasi — simpan salinan sendiri bila perlu riwayat
  lebih panjang.

### 4.8 Menjalankan ETL secara langsung (tidak dianjurkan)

```bash
.venv\Scripts\python.exe etl.py
```

Perintah ini menulis **langsung** ke `ukp.db`. Bila gagal di tengah jalan,
database bisa tertinggal dalam keadaan setengah jadi dan aplikasi ikut rusak.
Gunakan hanya saat membuat database pertama kali, atau ketika `ukp.db` memang
belum ada.

Untuk membangun ke lokasi lain tanpa menyentuh yang aktif:

```bash
.venv\Scripts\python.exe etl.py --out coba.db
```

**File sumber yang dibaca:**
- `D:\PUKP-3\v2026\ukp_v2026.xlsx` — data era 2026
- `D:\PUKP-3\v2026\ukp_v2021.xlsx` — data era 2021

> Kedua file berada di folder `v2026`. Ini memang membingungkan, tetapi
> jangan dipindahkan — `etl.py` mencarinya di sana.

**Hasil ETL:**
- `ukp.db` — database SQLite (tabel: `peserta`, `nilai`, `skl`, `ref_ijzh`, `ref_diklat`, `access_log`)
- `exceptions.csv` — baris data yang tidak bisa dipetakan ke peserta (lihat [Bagian 12](#12-exceptions-data-tidak-terpetakan))

**Waktu proses:** sekitar 90 detik.

**Jumlah data saat ini:**
- 32.813 peserta
- 41.137 rekaman ujian (nilai)
- 8.132 SKL
- 45 jenis ijazah (ref_ijzh)

### 4.9 Seberapa sering perlu diperbarui?

Ujian berlangsung kira-kira bulanan. Perbarui setiap kali menerima workbook
hasil sidang terbaru. Aplikasi tidak menampilkan tanggal pembaruan data, jadi
patokannya adalah nilai *ujian terakhir* yang ditampilkan `update_data.py`.

---

## 5. Menjalankan Self-Test

### Test backend (Python)

```bash
.venv\Scripts\python.exe test_app.py
```

Output jika berhasil:
```
fixture: ADJI SAMKARYA NUGRAHA sc=6211716503 dob=1997-01-12 ukp1=2021-10-28
all checks passed; access_log outcomes: {'bad_verify': 9, 'not_found': 1, 'ok': 12, 'rate_limited': 2}
```

Test ini menggunakan salinan sementara `ukp.db` (tidak mengubah data asli).

### Test dashboard (Node.js, opsional)

Memerlukan server berjalan di port 5057.

```bash
# Pertama, extract script dari halaman yang sedang berjalan
curl -s http://127.0.0.1:5057/ > _page.html
python -c "import re; h=open('_page.html',encoding='utf-8').read(); s=re.findall(r'<script>(.*?)</script>',h,re.S); open('_dash.js','w',encoding='utf-8').write(s[-1])"

# Lalu jalankan test
node test_dashboard.js
```

Output jika berhasil:
```
colours OK: 20 ijazah codes, 20 distinct
dashboard OK: 12 view combinations rendered, 4 charts each
```

Hapus file sementara setelah selesai:
```bash
del _page.html _dash.js
```

---

## 6. Struktur File

```
Project App UKP/
├── .venv/                    # Virtual environment Python
├── static/
│   ├── chart.umd.min.js      # Chart.js 4.4.1 (vendored, tanpa CDN)
│   ├── logo-header.png        # Logo Kemenhub (header, lebar, teks putih)
│   ├── logo-kemenhub.png      # Logo Kemenhub emblem (dipakai favicon)
│   └── logo-kemenhub-full.webp
├── templates/
│   ├── base.html              # Layout utama: header, navbar, footer, CSS
│   ├── index.html             # Dashboard statistik
│   ├── cek.html               # Form pencarian peserta
│   ├── verify.html            # Form verifikasi identitas
│   ├── detail.html            # Kartu nilai per tingkat ijazah
│   ├── login.html             # Masuk administrator
│   ├── batch.html             # Cek massal (tempel kode pelaut)
│   ├── filter.html            # Cari berdasarkan filter + halaman
│   └── error.html             # Halaman kesalahan
├── app.py                     # Flask app, routes, API
├── etl.py                     # Import Excel → SQLite
├── update_data.py             # Pembaruan data terkendali (lihat Bagian 4)
├── report_no_retake.py        # Laporan belum lulus & belum mengulang
├── test_app.py                # Self-test backend
├── test_filter.py             # Self-test fitur filter + halaman
├── test_dashboard.js          # Self-test chart/dashboard (Node.js)
├── ukp.db                     # Database SQLite (hasil ETL)
├── ukp.db.bak                 # Cadangan otomatis dari update_data.py
├── exceptions.csv             # Baris data yang gagal dipetakan
├── vercel.json                # Konfigurasi deploy Vercel
├── requirements.txt           # flask, openpyxl
└── .env.local                 # UKP_ADMIN_HASH (rahasia, tidak masuk git)
```

---

## 7. Fitur Aplikasi

### 7.1 Dashboard Statistik (halaman utama)

- **Ringkasan angka:** total rekaman ujian, jumlah lulus, belum lulus, SKL tercetak
- **Grafik rekapitulasi** bulanan atau tahunan (bar/pie/donat)
- **Tingkat kelulusan** per tingkat ijazah (bar chart, warna unik per program)
- **Rekaman per tingkat ijazah** (bar/pie/donat)
- **Rekaman per lembaga diklat** (bar/pie/donat)
- **Tabel keterangan singkatan** tingkat ijazah

### 7.2 Pencarian Peserta

- Cari berdasarkan **nama** (minimal 3 karakter) atau **seafarer code** (angka 6+ digit)
- Hasil pencarian maksimal **25 baris**
- Dari hasil, klik "Lihat data →" untuk masuk ke verifikasi

### 7.3 Verifikasi Identitas

- Peserta harus membuktikan identitas sebelum melihat data
- Cukup isi **salah satu**: tanggal lahir (TGLLAHIR) **atau** tanggal UKP pertama (UKP1)
- Format tanggal yang diterima: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD Month YYYY`
- Tanggal lahir placeholder (misal 1996-06-05 yang ada di 1.938 baris) **ditolak otomatis**

### 7.4 Detail Nilai Peserta

Setelah verifikasi berhasil, ditampilkan:
- **Kartu per tingkat ijazah** (misal UGN5, PASN3, GMDSS)
- **Riwayat ujian:** tanggal ujian, tanggal sidang, mengulang ke-berapa, jumlah nilai < 70
- **Tabel nilai:** nama materi uji (MU1–MU22) dari CONS, nilai per materi, warna merah untuk < 70
- **Status kelulusan:** LULUS / BELUM LULUS
- **Lembaga diklat / asal sekolah**
- **Peringatan** jika data sumber tidak konsisten (badge kuning)

### 7.5 Status Surat Keterangan Lulus (SKL)

Untuk setiap tingkat ijazah, ditampilkan salah satu:
- **SUDAH DICETAK** — nomor SKL, tanggal cetak, berlaku sampai kapan
- **TERDAFTAR, BELUM DICETAK** — terdaftar di DataSKL tapi belum ada TglCetak
- **BELUM DICETAK** — tidak ada di DataSKL sama sekali

---

## 8. Dashboard Statistik

### Filter yang tersedia

| Filter           | Pilihan                                       |
|------------------|-----------------------------------------------|
| Tahun            | Semua tahun, 2018–2026                        |
| Dasar tanggal    | Tanggal Ujian / Tanggal Sidang                |
| Tampilan grafik  | Batang (Bar) / Lingkaran (Pie) / Donat        |
| Periode rekap    | Bulanan / Tahunan                             |

### Kode warna grafik

Setiap keluarga program memiliki **warna unik** (satu hue per keluarga).
Gradasi terang → gelap menandakan tingkat ijazah rendah → tinggi.

Contoh: UGN1 (biru gelap) → UGN5 (biru terang), karena ANT-V lebih rendah dari ANT-I.

### API Endpoint

`GET /api/stats?year=all&basis=ujian` — mengembalikan JSON lengkap untuk rendering chart.
Parameter:
- `year` = `all` atau tahun tertentu (misal `2026`)
- `basis` = `ujian` (default) atau `sidang`

---

## 9. Alur Pengguna (End-User)

```
Buka http://127.0.0.1:5057/
        ↓
Dashboard statistik (tanpa login)
        ↓
Ketik nama atau seafarer code → klik Cari
        ↓
Pilih nama dari daftar → klik "Lihat data →"
        ↓
Isi tanggal lahir ATAU tanggal UKP pertama → klik Verifikasi
        ↓
  ┌─ Cocok → tampilkan kartu nilai lengkap + status SKL
  └─ Tidak cocok → pesan error, bisa coba lagi (maks 8× per 15 menit)
        ↓
Klik "Keluar" untuk kembali
```

---

## 10. Keamanan & Rate Limiting

- **Verifikasi identitas** wajib sebelum melihat data nilai
- **Rate limit:** maksimal **8 kali gagal verifikasi** per IP dalam **15 menit**
  - Setelah 8× gagal → pesan "Terlalu banyak percobaan gagal"
  - Reset otomatis setelah 15 menit
  - Dijalankan dari tabel `access_log`, bukan in-memory (survive restart)
- **Tanggal lahir placeholder** (yang dipakai massal di data sumber) ditandai `dob_usable=0` dan **selalu ditolak**
- **Session:** satu seafarer code per sesi login; klik "Keluar" untuk clear
- **Secret key:** secara default di-generate ulang setiap restart (semua sesi logout). Set `UKP_SECRET` di environment untuk persistent session

### Belum diimplementasikan (tambahkan sebelum production)
- **CSRF protection** — `pip install flask-wtf` + satu baris konfigurasi
- **HTTPS** — wajib jika diakses dari luar jaringan lokal

---

## 11. Access Log & Pelacakan Penggunaan

Setiap aksi pengguna dicatat di tabel `access_log`:

| Kolom     | Isi                                          |
|-----------|----------------------------------------------|
| `ts`      | Timestamp ISO (detik)                        |
| `ip`      | IP klien (atau X-Forwarded-For)              |
| `ua`      | User-Agent browser (maks 300 karakter)       |
| `action`  | `search`, `verify`, `view`                   |
| `q`       | Query pencarian (jika action = search)       |
| `sc`      | Seafarer code (jika verify/view)             |
| `uc`      | UC key (jika view)                           |
| `outcome` | `ok`, `not_found`, `bad_verify`, `rate_limited` |

### Cara melihat log

```bash
.venv\Scripts\python.exe -c "
import sqlite3; c=sqlite3.connect('ukp.db')
for r in c.execute('SELECT ts, ip, action, outcome, q, sc FROM access_log ORDER BY ts DESC LIMIT 20'):
    print(r)
"
```

### Cara menghitung penggunaan per hari

```bash
.venv\Scripts\python.exe -c "
import sqlite3; c=sqlite3.connect('ukp.db')
for r in c.execute('SELECT substr(ts,1,10) hari, COUNT(*) FROM access_log GROUP BY hari ORDER BY hari DESC LIMIT 14'):
    print(r)
"
```

### Deteksi aktivitas tidak wajar

Pantau IP dengan banyak kegagalan:
```bash
.venv\Scripts\python.exe -c "
import sqlite3; c=sqlite3.connect('ukp.db')
for r in c.execute(\"\"\"
    SELECT ip, COUNT(*) n, MIN(ts), MAX(ts)
    FROM access_log WHERE outcome='bad_verify'
    GROUP BY ip HAVING n > 5 ORDER BY n DESC
\"\"\"):
    print(r)
"
```

---

## 12. Exceptions (Data Tidak Terpetakan)

File `exceptions.csv` berisi baris dari DNILAI atau DataSKL yang tidak bisa dipetakan ke peserta.

| Kolom             | Isi                                        |
|-------------------|--------------------------------------------|
| `sumber`          | Nama file Excel asal                       |
| `sheet`           | Nama sheet (DNILAI / DataSKL)              |
| `kunci_di_sheet`  | Kunci UC yang gagal dipetakan              |
| `nama_menurut_sc` | Nama peserta berdasarkan seafarer code     |
| `masalah`         | Jenis masalah                              |
| `keterangan`      | Detail tambahan                            |

**Jenis masalah:**
- `SC dikenal, nomor ujian tidak cocok` — seafarer code ada, tapi nomor ujian berbeda
- `SC tidak ada di DataPeserta` — seafarer code tidak terdaftar sama sekali

**Saat ini:** 20 baris (11 orang). Ini < 0,05% dari 41.137 rekaman — bisa diabaikan atau diperbaiki manual di Excel lalu ETL ulang.

---

## 13. Konfigurasi Production

### Akses dari jaringan lokal (LAN)

1. Ubah baris terakhir `app.py`:
   ```python
   app.run(host="0.0.0.0", port=5057, debug=False)
   ```

2. Set secret key agar sesi tidak hilang saat restart:
   ```bash
   # Windows CMD
   set UKP_SECRET=ganti-dengan-string-acak-panjang

   # Git Bash
   export UKP_SECRET=ganti-dengan-string-acak-panjang
   ```

### Production server (banyak pengguna)

Flask dev server single-threaded, tidak cocok untuk beban banyak.
Gunakan waitress:

```bash
pip install waitress
python -m waitress --port=5057 app:app
```

### Checklist sebelum production

- [ ] Set `UKP_SECRET` environment variable
- [ ] Ubah `host` ke `"0.0.0.0"` dan `debug=False`
- [ ] Tambah CSRF protection (`pip install flask-wtf`)
- [ ] Gunakan waitress atau gunicorn, bukan Flask dev server
- [ ] Pastikan `ukp.db` dan `static/` bisa dibaca oleh proses server
- [ ] Jika diakses dari luar LAN, pasang HTTPS (nginx reverse proxy)

---

## 14. Keterangan Singkatan Tingkat Ijazah

| Kode      | Keterangan                                         |
|-----------|-----------------------------------------------------|
| **UGN**   | Upgrading/Peningkatan Nautika                       |
| **UGT**   | Upgrading/Peningkatan Teknika                       |
| **GMDSS** | Global Maritime Distress and Safety System          |
| **PRAN**  | Pra Prala Nautika                                   |
| **PRAT**  | Pra Prala Teknika                                   |
| **PASN3** | ANT III Pasca Prala                                 |
| **PAST3** | ATT III Pasca Prala                                 |
| **PASN2** | ANT II Pasca Layar (D-IV Lanjutan Nautika)          |
| **PAST2** | ATT II Pasca Layar (D-IV Lanjutan Teknika)          |

Angka di belakang kode UGN/UGT/PRAN/PRAT menunjukkan tingkat ijazah:
- **UGN5** = Peningkatan Nautika ke ANT-V
- **UGT2** = Peningkatan Teknika ke ATT-II
- **PRAN3** = Pra Prala Nautika ke ANT-III
- dst.

---

## 15. Troubleshooting

### `ukp.db not found - run: python etl.py`
Jalankan ETL terlebih dahulu. Lihat [Bagian 4](#4-memperbarui-data-dari-excel).

### Port 5057 sudah dipakai
```bash
# Cari proses yang memakai port
netstat -ano | findstr :5057
# Hentikan proses tersebut, atau ubah port di app.py baris terakhir
```

### Data tidak berubah setelah update Excel
1. Jalankan ulang `etl.py`
2. Restart server (`Ctrl+C` lalu `python app.py`)
3. Hard refresh browser (`Ctrl+F5`)

### Terlalu banyak percobaan gagal (terkunci)
Rate limit 8× gagal per IP per 15 menit. Tunggu 15 menit, atau bersihkan log:
```bash
.venv\Scripts\python.exe -c "
import sqlite3; c=sqlite3.connect('ukp.db')
c.execute(\"DELETE FROM access_log WHERE outcome='bad_verify'\")
c.commit(); print('cleared')
"
```

### Chart tidak muncul
- Pastikan file `static/chart.umd.min.js` ada (205 KB)
- Hard refresh browser (`Ctrl+F5`)
- Periksa console browser (F12 → Console) untuk error JavaScript

### Folder dipindahkan
Jika folder `Project App UKP` dipindahkan ke lokasi lain:
1. Buat ulang venv: `python -m venv .venv && pip install flask openpyxl`
2. Path file sumber di `etl.py` bersifat relatif ke `../v2026/` — pastikan
   folder `v2026` tetap satu level di atas `Project App UKP`

---

*Dokumen ini dibuat 15 September 2026. Hubungi Pranata Laboratorium Pendidikan
SPP/STIP Jakarta jika ada pertanyaan.*
