# KPU Pileg OCR — Google Drive → OCR Lokal → Excel

Sistem pemindaian dan pengolahan dokumen PDF rekapitulasi KPU secara lokal.

Sistem membaca file PDF dari struktur folder Google Drive, melakukan pengecekan kualitas PDF, menjalankan OCR lokal, mengambil data hasil pemindaian, kemudian menyimpan hasil ke Excel berdasarkan Dapil serta membuat checkpoint seluruh file yang diproses.

> Sistem ini dibuat untuk kebutuhan pengolahan data rekapitulasi Pileg DPR.

---

## 1. Alur Sistem

```text
Google Drive
     │
     ▼
Scan folder secara rekursif
     │
     ▼
Temukan semua PDF
     │
     ▼
Ambil informasi wilayah dari nama folder
     │
     ├── Dapil
     ├── Kab/Kota
     └── Kecamatan
     │
     ▼
Download PDF sementara
     │
     ▼
PDF Quality Check
     │
     ├── PDF rusak / kosong
     │       └── PDF_RUSAK
     │
     └── PDF valid
             │
             ▼
       OCR Lokal
             │
             ├── Berhasil
             │      └── Validasi + Excel
             │
             ├── Tidak terbaca
             │      └── DATA_TIDAK_TERBACA
             │
             ├── Timeout
             │      └── SKIPPED
             │
             └── Error
                    └── GAGAL
```

---

## 2. Fitur Utama

- Membaca PDF langsung dari Google Drive.
- Tidak membutuhkan upload PDF satu per satu melalui aplikasi.
- Scan folder Google Drive secara rekursif.
- Nama file PDF tidak harus memiliki format tertentu.
- Informasi wilayah diambil dari struktur folder.
- OCR dilakukan secara lokal.
- PDF yang rusak dapat dipisahkan dari proses.
- PDF yang terlalu lama diproses dapat dilewati menggunakan timeout.
- Satu PDF bermasalah tidak menghentikan seluruh proses.
- Hasil disimpan berdasarkan Dapil.
- Checkpoint mencatat status setiap PDF.
- Proses dapat dilanjutkan tanpa harus mengulang file yang sudah selesai.
- Credential Google tidak disimpan di Git.
- Hasil OCR lokal tidak disimpan di Git.

---

## 3. Struktur Google Drive

Sistem mengharapkan struktur folder seperti berikut:

```text
PILEG DPR
│
├── DAPIL
│   │
│   ├── KAB/KOTA
│   │   │
│   │   ├── KECAMATAN
│   │   │   │
│   │   │   ├── file_1.pdf
│   │   │   ├── file_2.pdf
│   │   │   └── ...
│   │   │
│   │   └── KECAMATAN LAIN
│   │
│   └── KAB/KOTA LAIN
│
└── DAPIL LAIN
```

Contoh:

```text
PILEG DPR
└── DAPIL JAWA BARAT 6
    └── KOTA DEPOK
        └── CIMANGGIS
            ├── dokumen_001.pdf
            ├── dokumen_002.pdf
            └── dokumen_003.pdf
```

Sistem akan membaca:

```text
dapil      = DAPIL JAWA BARAT 6
kab_kota   = KOTA DEPOK
kecamatan  = CIMANGGIS
```

Nama file PDF sendiri tidak digunakan untuk menentukan wilayah.

---

## 4. Struktur Folder Project

```text
buku-suara-main/
│
├── app.py
├── gdrive_processor.py
├── pdf_worker.py
├── pdf_hybrid_engine.py
├── local_ocr_engine.py
├── database.py
├── simple_excel.py
├── village_resolver.py
├── gdrive_processor_backup.py
│
├── credentials.json        # lokal, JANGAN commit
├── token.json              # lokal, JANGAN commit
│
├── master/
│
├── outputs/
│   ├── dapil/
│   ├── checkpoint/
│   ├── state/
│   └── tmp/
│
└── .venv/
```

`credentials.json`, `token.json`, `outputs/`, dan `.venv/` tidak boleh masuk repository.

---

## 5. Persyaratan

Disarankan menggunakan:

- Windows
- Python 3.x
- Git
- Google Account
- Google Drive
- Google Drive API
- Tesseract OCR
- Virtual environment Python

Library Python utama yang digunakan antara lain:

- Streamlit
- pandas
- openpyxl
- PyMuPDF
- pytesseract
- Pillow
- pdf2image
- Google API Client
- Google Authentication

---

## 6. Instalasi

Clone repository:

```powershell
git clone -b firda-ocr https://github.com/atanlau03/buku-suara.git
cd buku-suara
```

Buat virtual environment:

```powershell
python -m venv .venv
```

Aktifkan:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependency:

```powershell
pip install -r requirements.txt
```

Jika project memiliki file dependency khusus Google Drive:

```powershell
pip install -r requirements_gdrive.txt
```

---

## 7. Google Drive API

Sistem menggunakan Google Drive API untuk membaca file dari Google Drive.

Setiap komputer yang menjalankan project perlu melakukan autentikasi Google sendiri.

Buat project pada Google Cloud Console dan aktifkan:

```text
Google Drive API
```

---

## 8. OAuth Credentials

Buat OAuth Client ID dengan tipe:

```text
Desktop app
```

Download file credential dari Google Cloud.

Rename menjadi:

```text
credentials.json
```

Letakkan di folder utama project:

```text
buku-suara/
├── app.py
├── gdrive_processor.py
├── pdf_worker.py
└── credentials.json
```

### PENTING

`credentials.json` adalah credential lokal.

Jangan upload ke GitHub.

File tersebut sudah dimasukkan ke `.gitignore`.

---

## 9. Autentikasi Google

Saat aplikasi pertama kali dijalankan, sistem akan membuka proses OAuth Google.

Login menggunakan akun Google yang memiliki akses ke folder Drive yang akan diproses.

Setelah autentikasi berhasil, sistem akan membuat:

```text
token.json
```

`token.json` juga hanya digunakan secara lokal dan tidak boleh di-upload ke GitHub.

---

## 10. Menjalankan Aplikasi

Aktifkan virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Jalankan:

```powershell
python -m streamlit run app.py --server.port 8502
```

Kemudian buka:

```text
http://localhost:8502
```

---

## 11. Tampilan Aplikasi

Aplikasi memiliki konfigurasi utama:

```text
Link folder Google Drive
DPI OCR
Batas waktu per PDF
```

Contoh konfigurasi:

```text
DPI OCR             : 300
Batas waktu PDF     : 15 detik
```

Untuk pengujian, timeout dapat dibuat lebih pendek.

Untuk pemrosesan sebenarnya, timeout sebaiknya disesuaikan dengan ukuran dan kondisi PDF.

---

## 12. Link Google Drive

Masukkan link folder utama Google Drive.

Contoh:

```text
https://drive.google.com/drive/folders/XXXXXXXX
```

Folder tidak perlu dibuat public.

Akun Google yang melakukan OAuth harus memiliki akses ke folder tersebut.

Sistem akan membaca isi folder secara rekursif.

---

## 13. Status Pemrosesan

Setiap PDF memiliki status.

### MENUNGGU

File sudah ditemukan tetapi belum diproses.

### DIPROSES

File sedang diproses.

### BERHASIL

OCR berhasil menghasilkan data yang dapat dimasukkan ke laporan.

### GAGAL

Terjadi error saat proses.

Pesan error disimpan pada checkpoint.

### PDF_RUSAK

File tidak dapat dibuka atau dianggap tidak valid saat quality check.

### DATA_TIDAK_TERBACA

PDF dapat diproses tetapi hasil OCR tidak menghasilkan data laporan yang dapat digunakan.

### SKIPPED

File dilewati karena melebihi batas waktu pemrosesan.

---

## 14. Timeout

Sistem memiliki batas waktu pemrosesan per PDF.

Tujuannya adalah mencegah satu file yang sangat buruk membuat seluruh proses berhenti terlalu lama.

Contoh:

```text
Batas waktu = 60 detik
```

Jika sebuah PDF belum selesai setelah batas tersebut:

```text
SKIPPED
```

File berikutnya tetap dapat diproses.

Informasi skip dicatat di checkpoint.

---

## 15. Output Excel

Sistem menghasilkan dua jenis output utama.

### 15.1 Excel per Dapil

Hasil utama disimpan berdasarkan Dapil.

Contoh:

```text
outputs/
└── dapil/
    ├── DAPIL_JAWA_BARAT_6.xlsx
    ├── DAPIL_JAWA_BARAT_7.xlsx
    └── ...
```

Jika beberapa PDF berasal dari Dapil yang sama, hasilnya dimasukkan ke workbook Dapil yang sama.

---

## 16. Format Data Utama

Kolom laporan utama:

```text
kelurahan
No partai
nama_partai
suara_akhir_partai
suara_sah
suara_tidak_sah
total_suara
provinsi
dapil
kab_kota
kecamatan
```

Urutan kolom harus dipertahankan.

---

## 17. Penanda Data Tidak Pasti

Jika sistem tidak yakin terhadap suatu nilai atau hasil validasi tidak dapat memastikan nilai tersebut, nilai dapat diberi tanda:

```text
^
```

Contoh:

```text
kelurahan = BUMI DIPASENA UTAMA
suara_sah = 7277
suara_tidak_sah = ^
```

Tanda tersebut digunakan agar data yang perlu diperiksa manual dapat ditemukan kembali.

---

## 18. Validasi Total Suara

Salah satu pengecekan penting:

```text
total_suara = suara_sah + suara_tidak_sah
```

Contoh:

```text
suara_sah        = 7277
suara_tidak_sah  = 442
total_suara      = 7719
```

Data yang tidak memenuhi logika tersebut perlu ditandai untuk pemeriksaan.

---

## 19. Data Pindahan

Dokumen KPU dapat memiliki bagian seperti:

```text
DATA PINDAHAN
JUMLAH PINDAHAN
```

Bagian tersebut tidak boleh dihitung sebagai data suara tambahan apabila hanya merupakan bagian informasi perpindahan/pelengkap dokumen.

Sistem harus menghindari double counting.

---

## 20. Checkpoint Indonesia

Selain Excel per Dapil, sistem membuat checkpoint:

```text
outputs/
└── checkpoint/
    └── CHECKPOINT_INDONESIA.xlsx
```

Checkpoint digunakan untuk mencatat perkembangan seluruh file yang ditemukan.

Informasi yang dicatat antara lain:

```text
drive_file_id
nama_file
status
provinsi
dapil
kab_kota
kecamatan
kelurahan
waktu_mulai
waktu_selesai
jumlah_baris
pesan
drive_path
```

Checkpoint berguna untuk mengetahui:

- file mana yang sudah selesai
- file mana yang sedang diproses
- file mana yang gagal
- file mana yang rusak
- file mana yang dilewati karena timeout
- pesan/error yang terjadi
- lokasi file di Google Drive

---

## 21. Resume Processing

Sistem menyimpan state pemrosesan secara lokal.

Lokasi:

```text
outputs/state/
```

File state:

```text
processor_state.json
```

Tujuannya agar pemrosesan dapat dilanjutkan dan file yang sudah berhasil tidak perlu diproses ulang.

---

## 22. File Sementara

File sementara digunakan selama proses OCR.

Lokasi:

```text
outputs/tmp/
```

File sementara akan dibersihkan setelah proses selesai.

---

## 23. Master Wilayah

Project menyediakan folder:

```text
master/
```

Folder ini digunakan untuk data master wilayah Indonesia.

Tujuan master wilayah adalah membantu resolver ketika OCR menghasilkan nama wilayah yang tidak persis sama dengan nama sebenarnya.

Contoh masalah:

```text
BUMI DIPASENA UTAM4
```

dapat dicocokkan dengan data master:

```text
BUMI DIPASENA UTAMA
```

Master wilayah sebaiknya menggunakan dataset wilayah yang sudah disepakati dalam project.

Jangan memasukkan data master yang belum diverifikasi sebagai data resmi.

---

## 24. Prinsip OCR

Sistem menggunakan OCR lokal.

OCR bukan hanya mengambil teks mentah dari PDF.

Alur pengolahan dapat melibatkan:

```text
PDF
 ↓
Text Layer / OCR
 ↓
Pengambilan data
 ↓
Pembersihan
 ↓
Resolver wilayah
 ↓
Validasi
 ↓
Excel
```

PDF dengan kualitas berbeda dapat menghasilkan tingkat keberhasilan OCR yang berbeda.

Dokumen yang sangat buram, miring, rusak, atau memiliki struktur yang sulit dibaca dapat membutuhkan pemeriksaan manual.

---

## 25. Keamanan

Jangan pernah melakukan commit terhadap:

```text
credentials.json
token.json
client_secret*.json
.env
outputs/
.venv/
```

Pastikan:

```powershell
git status --ignored
```

menunjukkan file credential sebagai ignored.

Jika credential pernah terlanjur masuk repository, segera lakukan pengamanan credential tersebut melalui Google Cloud.

---

## 26. Git Branch

Versi sistem Google Drive OCR ini dikembangkan pada branch:

```text
firda-ocr
```

Untuk mengambil versi tersebut:

```powershell
git clone -b firda-ocr https://github.com/atanlau03/buku-suara.git
```

Jika repository sudah pernah di-clone:

```powershell
git checkout firda-ocr
git pull origin firda-ocr
```

Branch `main` tidak digunakan sebagai branch pengembangan sistem ini.

---

## 27. Troubleshooting

### A. `credentials.json` tidak ditemukan

Pastikan file berada di:

```text
folder-project/
└── credentials.json
```

---

### B. Google menampilkan `access_denied`

Pastikan akun Google yang digunakan sudah ditambahkan sebagai:

```text
Test user
```

pada OAuth App jika aplikasi masih dalam mode testing.

---

### C. OAuth berhasil tetapi PDF = 0

Periksa:

1. Link folder Google Drive benar.
2. Akun Google memiliki akses ke folder.
3. Folder tersebut benar-benar berisi struktur folder/PDF.
4. Struktur folder sesuai dengan struktur yang diharapkan.
5. Aplikasi sudah melakukan scan secara rekursif.

---

### D. PDF menjadi `SKIPPED`

Artinya PDF melewati batas timeout.

Periksa:

```text
Batas waktu per PDF
```

Jika file memang membutuhkan waktu lebih lama, naikkan timeout.

---

### E. PDF menjadi `PDF_RUSAK`

Quality check tidak dapat membuka PDF dengan normal.

Periksa file tersebut secara manual.

---

### F. PDF menjadi `DATA_TIDAK_TERBACA`

PDF berhasil diproses tetapi hasil OCR tidak menghasilkan data laporan yang dapat digunakan.

File perlu diperiksa untuk mengetahui apakah:

- kualitas scan terlalu buruk
- struktur dokumen berbeda
- text layer bermasalah
- OCR gagal membaca tabel
- resolver/validasi belum menangani pola tersebut

---

## 28. Prinsip Pengembangan

Project ini ditujukan untuk menangani dataset KPU dalam jumlah besar.

Karena kualitas PDF dapat berbeda-beda, sistem harus mengikuti prinsip:

```text
1 file bermasalah
        ↓
jangan hentikan seluruh proses
        ↓
catat status
        ↓
lanjutkan file berikutnya
```

Selain itu:

```text
data berhasil
     ↓
langsung simpan
     ↓
checkpoint diperbarui
```

Dengan demikian hasil yang sudah berhasil tetap tersedia meskipun masih ada file lain yang belum selesai.

---

## 29. Catatan Penting

Sistem ini melakukan OCR dan pengolahan data secara otomatis, sehingga hasil tetap perlu divalidasi terutama pada dokumen dengan kualitas scan rendah.

Excel hasil sistem bukan berarti seluruh data telah dipastikan benar secara manual.

Kolom/record yang diberi tanda:

```text
^
```

perlu diperiksa kembali.

---

## 30. Quick Start

```powershell
git clone -b firda-ocr https://github.com/atanlau03/buku-suara.git
cd buku-suara

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

python -m streamlit run app.py --server.port 8502
```

Kemudian:

```text
1. Masukkan link folder Google Drive
2. Login Google melalui OAuth
3. Atur DPI OCR
4. Atur timeout
5. Klik Mulai / Lanjutkan
6. Pantau checkpoint
7. Ambil Excel Dapil dari outputs/dapil/
```

---

## 31. Status Project

```text
Google Drive
     ↓
PDF KPU
     ↓
OCR lokal
     ↓
Validasi data
     ↓
Excel per Dapil
     +
Checkpoint Indonesia
```

Pengembangan berikutnya dapat difokuskan pada peningkatan kemampuan OCR/Text Layer, resolver wilayah, validasi angka, dan penanganan PDF berkualitas rendah.
