# KPU Pileg OCR — Google Drive

Aplikasi ini berjalan melalui **Streamlit** seperti project Buku Suara sebelumnya.
VS Code/PowerShell hanya dipakai untuk menjalankan backend lokalnya.

## 1. Install dependency

```powershell
python -m pip install -r requirements_gdrive.txt
```

## 2. Google Drive OAuth

Aplikasi memakai OAuth akun Google milik pengguna sendiri.
Tidak ada email/password yang dimasukkan ke source code.

Buat **OAuth Client ID → Desktop app** pada Google Cloud, download file JSON,
lalu rename menjadi:

```text
credentials.json
```

dan letakkan di folder yang sama dengan `app.py`.

Pada login pertama, browser akan terbuka untuk meminta izin membaca Google Drive.
Setelah berhasil, token lokal disimpan sebagai `token.json` sehingga login tidak perlu diulang setiap kali.

## 3. Jalankan

```powershell
python -m streamlit run app.py --server.port 8502
```

Buka:

```text
http://localhost:8502
```

## 4. Struktur Drive

Masukkan link folder paling atas yang berisi struktur seperti:

```text
FOLDER PILEG DPR/
└── DAPIL/
    └── KAB/KOTA/
        └── KECAMATAN/
            └── FILE.pdf
```

Jika ada folder provinsi di antara root dan Dapil, sistem juga akan mengambilnya dari struktur folder.

## 5. Output

Semua hasil berada di:

```text
outputs/
├── dapil/
│   ├── DAPIL_....xlsx
│   └── ...
├── checkpoint/
│   └── CHECKPOINT_INDONESIA.xlsx
├── state/
│   └── processor_state.json
└── tmp/
```

### Excel Dapil

Kolom:

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

Setiap PDF yang berhasil diproses akan langsung ditambahkan ke Excel Dapil yang sesuai.
Jika Dapil yang sama memiliki PDF berikutnya, datanya tetap masuk ke workbook yang sama,
dengan satu baris kosong sebagai pemisah antar sumber PDF.

### Checkpoint

Checkpoint diperbarui setelah setiap perubahan status file:

- `MENUNGGU`
- `DIPROSES`
- `BERHASIL`
- `PDF_RUSAK`
- `DATA_TIDAK_TERBACA`
- `GAGAL`
- `SKIPPED`

File yang sudah `BERHASIL`, `PDF_RUSAK`, atau `DATA_TIDAK_TERBACA` tidak diproses ulang pada resume normal.
File `GAGAL`/`SKIPPED` dapat dicoba lagi pada proses berikutnya.

## 6. Download saat proses masih berjalan

Excel tidak menunggu seluruh Indonesia selesai. Setelah PDF pertama selesai,
file Excel sudah tersedia dan dapat di-download dari Streamlit sementara PDF lain tetap diproses.

Penulisan Excel menggunakan file sementara lalu `os.replace`, sehingga file yang sedang ditulis
tidak disajikan sebagai hasil setengah jadi.

## 7. Jika satu PDF bermasalah

PDF diperiksa terlebih dahulu. OCR per file dijalankan dalam subprocess dengan timeout.
Jika satu PDF rusak, gagal, atau melewati timeout, statusnya masuk checkpoint dan antrean lanjut ke file berikutnya.
