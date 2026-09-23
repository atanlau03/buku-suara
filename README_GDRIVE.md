# KPU OCR — Google Drive + Checkpoint

Versi ini mempertahankan engine OCR lokal dari project `buku-suara-main` dan menambahkan alur pemrosesan dari Google Drive.

## Yang sudah disiapkan
- Membaca PDF langsung dari folder Google Drive privat melalui OAuth di komputer pengguna.
- Menelusuri subfolder secara rekursif.
- Metadata folder dipakai untuk `provinsi`, `dapil`, `kab_kota`, dan `kecamatan`.
- Satu Excel untuk setiap Dapil.
- Banyak PDF/kelurahan dalam Dapil yang sama digabung ke Excel yang sama, dengan satu baris kosong sebagai pemisah.
- Excel Dapil disimpan setelah setiap PDF berhasil.
- `CHECKPOINT_INDONESIA.xlsx` diperbarui setelah setiap file.
- PDF rusak, gagal dibaca, data kosong, atau melewati timeout tidak menghentikan file berikutnya.
- File yang sudah `BERHASIL`, `PDF_RUSAK`, atau `DATA_TIDAK_TERBACA` tidak diproses ulang pada tombol berikutnya.
- Output ditulis secara atomik agar file yang sedang ditulis tidak menjadi Excel setengah jadi.
- UI sederhana dan download tersedia selama proses masih berjalan.

## Kolom Excel Dapil
`kelurahan`, `No partai`, `nama_partai`, `suara_akhir_partai`, `suara_sah`, `suara_tidak_sah`, `total_suara`, `provinsi`, `dapil`, `kab_kota`, `kecamatan`

Jika validasi angka belum meyakinkan, nilai diberi `^` dan dicatat di checkpoint.

## Satu kali setup Google
Google Drive API menggunakan OAuth Desktop. Tidak ada password Google yang ditulis di aplikasi. Google menyarankan membuat OAuth Client ID tipe Desktop dan menyimpan file hasil unduhan sebagai `credentials.json`. Setelah login pertama, token lokal akan dipakai kembali. Referensi resmi: https://developers.google.com/workspace/drive/api/quickstart/python

1. Buka Google Cloud Console.
2. Buat/ pilih project.
3. Aktifkan Google Drive API.
4. Konfigurasikan Google Auth Platform/OAuth consent.
5. Buat OAuth Client ID → Desktop app.
6. Download JSON dan rename menjadi `credentials.json`.
7. Taruh `credentials.json` di folder utama project ini.

## Instalasi
Di PowerShell pada folder project:

```powershell
python -m pip install -r requirements_gdrive.txt
```

Pastikan Tesseract OCR sudah terpasang dan bisa ditemukan oleh `pytesseract` sesuai setup project lama.

## Menjalankan
```powershell
python -m streamlit run app.py --server.port 8502
```

Buka halaman **KPU OCR — Google Drive** di sidebar.

Tempel link folder `PILE G DPR` paling atas. Struktur yang didukung:

`PILE G DPR / DAPIL / KAB/KOTA / KECAMATAN / PDF`

Jika nama folder provinsi berada satu tingkat di atas Dapil, sistem juga mencoba mempertahankan nama tersebut sebagai metadata.

## Catatan penting
OCR tetap bersifat pembacaan dokumen. Tidak ada sistem OCR yang dapat menjamin 100% benar untuk semua scan yang rusak. Karena itu nilai yang gagal divalidasi tidak dipaksakan menjadi angka dan diberi `^`.
