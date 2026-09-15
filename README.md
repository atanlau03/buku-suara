# Buku Suara — Rekapitulasi Hasil Suara Legislatif

Aplikasi web (Streamlit) untuk mengolah data hasil suara legislatif dari tingkat TPS:
upload data manual, rekap/dashboard, hitung kursi (Sainte-Lague), dan caleg terpilih
(metode suara terbanyak), lengkap dengan export laporan Excel.

## Alur data (penting)

1. **Upload Data** (Excel/CSV) — jalur utama masuk ke database. Kolom wajib
   cuma 7: `provinsi, kab_kota, kecamatan, kelurahan, tps, partai, jumlah_suara`.
   Satu baris = satu partai di satu TPS.
2. **Upload PDF (OCR)** — baca scan formulir, kamu koreksi hasilnya di layar,
   lalu klik "Unduh sebagai file Excel". File PDF **tidak langsung masuk ke
   database** — unduh dulu Excel-nya, baru upload lewat menu Upload Data.
   Ini supaya jalur masuk ke database selalu satu pintu yang sama (terbukti stabil).
3. **Dashboard** menghitung otomatis dari data mentah itu: jumlah TPS per
   kelurahan, total suara partai per kelurahan/kecamatan/kabupaten/provinsi —
   kamu tidak perlu input angka agregat itu sendiri.
4. **Laporan** untuk export Excel, **Hitung Kursi — Lanjutan** untuk yang
   butuh alokasi kursi per dapil (opsional, butuh kolom tambahan `dapil` dan
   `caleg` kalau mau sampai ke caleg terpilih).

## Instalasi OCR (untuk fitur Upload PDF)

Fitur "Upload PDF (OCR)" membaca formulir C.Hasil hasil scan. Ini butuh mesin
**Tesseract OCR** terpasang terpisah di komputer (bukan cuma lewat `pip`):

- **Windows**: unduh installer dari https://github.com/UB-Mannheim/tesseract/wiki,
  install, lalu catat lokasi `tesseract.exe` (biasanya
  `C:\Program Files\Tesseract-OCR\tesseract.exe`) dan isikan di sidebar halaman
  Upload PDF (OCR) jika belum otomatis terdeteksi.
- **macOS**: `brew install tesseract tesseract-lang`
- **Linux (Debian/Ubuntu)**: `sudo apt install tesseract-ocr tesseract-ocr-ind`

Untuk akurasi lebih baik pada teks Bahasa Indonesia, pastikan paket bahasa
`ind` (Indonesian) ikut terpasang, bukan cuma `eng`.

**Batasan penting**: OCR formulir hasil scan (apalagi tulisan tangan) tidak
akan 100% akurat. Fitur ini dirancang sebagai bantuan entri data — hasil
tebakan HARUS diperiksa dan dikoreksi manual sebelum disimpan.

## Cara Menjalankan

1. Pastikan Python 3.9+ terpasang.
2. Install dependency:
   ```
   pip install -r requirements.txt
   ```
3. Jalankan aplikasi:
   ```
   streamlit run app.py
   ```
4. Buka browser ke alamat yang muncul (biasanya http://localhost:8501).

## Struktur Folder

```
sistem_pileg/
├── app.py                     # halaman utama
├── database.py                # model & fungsi database (SQLite)
├── seat_calculation.py        # logika Sainte-Lague & alokasi caleg
├── requirements.txt
├── ocr_utils.py                # fungsi konversi PDF->gambar & OCR
├── ui_theme.py                 # tema tampilan merah-putih
├── .streamlit/config.toml      # konfigurasi tema Streamlit
├── pages/
│   ├── 1_Upload_Data.py            # upload CSV/Excel hasil suara per TPS
│   ├── 2_Upload_PDF_OCR.py         # upload scan formulir + crop OCR
│   ├── 3_Dashboard.py              # telusuri suara: Provinsi > Kabupaten > Kecamatan
│   ├── 4_Laporan.py                # export Excel (mentah, rekap partai, rekap wilayah)
│   └── 5_Hitung_Kursi_Lanjutan.py  # opsional: alokasi kursi Sainte-Lague & caleg terpilih
└── pileg.db                        # dibuat otomatis saat pertama kali dijalankan
```

## Catatan penggunaan dasar

Kalau kamu hanya butuh **total suara partai per TPS** (tanpa rincian per caleg),
cukup isi kolom `caleg` kosong (atau `SUARA PARTAI`) untuk setiap baris —
Dashboard akan tetap merekap totalnya dengan benar di semua level wilayah.
Data per caleg hanya diperlukan kalau kamu memakai halaman **Hitung Kursi —
Lanjutan** untuk menentukan caleg terpilih.

## Format Data Upload

Kolom wajib (unduh template Excel dari menu Upload Data):

| Kolom | Keterangan |
|---|---|
| provinsi | Nama provinsi |
| dapil | Nama daerah pemilihan |
| kab_kota | Kabupaten/Kota |
| kecamatan | Kecamatan |
| kelurahan | Kelurahan/Desa |
| tps | Nomor/ID TPS |
| partai | Nama partai |
| nomor_urut_partai | Nomor urut partai |
| caleg | Nama caleg (kosongkan untuk baris suara partai) |
| nomor_urut_caleg | Nomor urut caleg (0 untuk suara partai) |
| jumlah_suara | Jumlah suara sah |

## Catatan Metode

- **Sainte-Lague murni**: pembagi ganjil 1, 3, 5, 7, ... — metode resmi KPU sejak 2019.
- **Ambang batas parlemen** (opsional, biasanya 4%) hanya berlaku untuk DPR RI,
  dihitung dari total suara sah nasional, bukan per dapil.
- **Suara terbanyak**: kursi partai diberikan ke caleg dengan suara individu tertinggi,
  bukan berdasar nomor urut (sesuai Putusan MK 2008).

## Pengembangan Lanjutan (opsional)

- Ganti SQLite dengan PostgreSQL untuk multi-user / data besar.
- Tambah autentikasi (multi-operator input data per TPS).
- Tambah peta interaktif (misal dengan `folium`) jika data koordinat TPS tersedia.
- Migrasi ke Flask/Django + frontend terpisah bila perlu kustomisasi tampilan lebih dalam.
