import streamlit as st
from database import init_db, get_full_dataframe, list_uploaded_files
from ui_theme import apply_theme, letterhead

st.set_page_config(page_title="Buku Suara", page_icon="🗳️", layout="wide")
apply_theme()
init_db()

letterhead("Buku Suara", "Rekapitulasi suara legislatif — dari TPS sampai provinsi, di satu tempat")

st.markdown(
    """
Data suara masuk lewat satu pintu: file Excel/CSV. Kalau sumbernya scan PDF,
baca dulu dengan bantuan OCR di menu Upload PDF — hasilnya diunduh sebagai
file Excel, lalu file itu diupload lewat menu Upload Data. Begitu masuk,
semuanya bisa ditelusuri dari Dashboard: pilih provinsi, turun ke kabupaten,
kecamatan, sampai kelurahan/desa — jumlah TPS dan total suara per partai
dihitung otomatis.

Menu di sebelah kiri:

- **Upload Data** — masukkan data dari file Excel/CSV (jalur utama ke database).
- **Upload PDF (OCR)** — baca scan formulir, koreksi, lalu hasilkan file Excel.
- **Dashboard** — telusuri suara per partai, per wilayah, sampai kelurahan/desa.
- **Laporan** — unduh rekap dalam Excel.
- **Hitung Kursi (lanjutan)** — opsional, untuk yang butuh alokasi kursi per dapil.
"""
)

st.divider()

# Ambil informasi file dari database
df_files = list_uploaded_files()

if not df_files.empty:
    st.markdown("### Daftar File Terunggah")
    
    # Menyiapkan tampilan tabel file yang bersih
    df_display = df_files.copy()
    
    column_renames = {
        "nama_file_asal": "Nama File",
        "waktu_upload_terakhir": "Waktu Upload",
        "jumlah_baris": "Jumlah Baris",
        "total_suara": "Total Suara",
        "sumber": "Metode Upload",
        "aktif": "Status"
    }
    
    available_cols = [c for c in column_renames.keys() if c in df_display.columns]
    df_display = df_display[available_cols]
    
    if "aktif" in df_display.columns:
        df_display["aktif"] = df_display["aktif"].map({1: "🟢 Aktif", 0: "🔴 Nonaktif"})

    df_display = df_display.rename(columns=column_renames)
    
    st.dataframe(df_display, use_container_width=True, hide_index=True)

else:
    st.info("Belum ada file yang diunggah. Mulai dari menu Upload Data atau Upload PDF (OCR) di sidebar.")