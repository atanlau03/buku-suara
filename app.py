from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from gdrive_processor import (
    CHECKPOINT_PATH,
    STATE_PATH,
    DAPIL_DIR,
    latest_outputs,
    read_checkpoint_records,
    read_state,
    start_job,
    stop_job,
)

st.set_page_config(
    page_title="KPU Pileg OCR",
    page_icon="📄",
    layout="wide",
)

st.title("KPU Pileg OCR")
st.caption("Google Drive → OCR lokal → Excel Dapil + Checkpoint Indonesia")

with st.sidebar:
    st.header("Pemrosesan")
    folder = st.text_input(
        "Link folder Google Drive",
        placeholder="https://drive.google.com/drive/folders/...",
        help="Masukkan folder paling atas yang berisi struktur Pileg/Dapil/Kab-Kota/Kecamatan/PDF.",
    )
    dpi = st.number_input("DPI OCR", min_value=150, max_value=500, value=300, step=50)
    timeout_minutes = st.number_input("Batas waktu per PDF (menit)", min_value=1, max_value=120, value=15, step=1)

    st.divider()
    credentials_path = Path(__file__).resolve().parent / "credentials.json"
    if credentials_path.exists():
        st.success("credentials.json ditemukan")
    else:
        st.warning("credentials.json belum ada")
        st.caption("Tambahkan OAuth Desktop Client milik kamu sendiri ke folder aplikasi.")

    state = read_state()
    if state.get("running"):
        st.info("Proses sedang berjalan")
        if st.button("Hentikan antrean", use_container_width=True):
            stop_job()
            st.rerun()
    else:
        if st.button("Mulai / Lanjutkan", type="primary", use_container_width=True):
            if not folder.strip():
                st.error("Masukkan link folder Google Drive terlebih dahulu.")
            elif not credentials_path.exists():
                st.error("credentials.json belum ada. Letakkan file OAuth Desktop Client di folder aplikasi.")
            else:
                ok, message = start_job(folder.strip(), int(dpi), int(timeout_minutes * 60))
                if ok:
                    st.success("Pemrosesan dimulai.")
                    st.rerun()
                else:
                    st.warning(message)

    st.divider()
    st.caption("Output disimpan otomatis di folder outputs/.")


def download_button_for_file(path: Path, label: str, key: str):
    if not path.exists():
        return
    try:
        data = path.read_bytes()
        st.download_button(
            label,
            data=data,
            file_name=path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=key,
            use_container_width=True,
        )
    except Exception as exc:
        st.caption(f"File sedang diperbarui, coba lagi beberapa detik kemudian. ({exc})")


@st.fragment(run_every=2)
def live_status():
    state = read_state()
    records = read_checkpoint_records()

    st.subheader("Status")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total PDF", state.get("total", 0) or len(records))
    col2.metric("Selesai", state.get("done", 0))
    col3.metric("Status", "BERJALAN" if state.get("running") else "SIAP")
    col4.metric("Dapil Excel", len(list(DAPIL_DIR.glob("*.xlsx"))))

    message = state.get("message", "Siap.")
    if message:
        st.info(message)

    if records:
        df = pd.DataFrame(records)
        preferred = [
            "nama_file", "status", "provinsi", "dapil", "kab_kota",
            "kecamatan", "jumlah_baris", "pesan", "waktu_selesai",
        ]
        cols = [c for c in preferred if c in df.columns]
        st.dataframe(df[cols], use_container_width=True, hide_index=True)
    else:
        st.caption("Belum ada checkpoint. Setelah folder Drive dipindai, daftar PDF akan muncul di sini.")


live_status()

st.divider()
st.subheader("Download hasil sementara")
st.caption("File dapat diunduh kapan saja. Pemrosesan PDF berikutnya tetap berjalan di belakang layar.")

checkpoint = CHECKPOINT_PATH if CHECKPOINT_PATH.exists() else None
dapil_files, _ = latest_outputs()

if checkpoint or dapil_files:
    if checkpoint:
        st.markdown("**Checkpoint Indonesia**")
        download_button_for_file(checkpoint, "Download CHECKPOINT_INDONESIA.xlsx", "download_checkpoint")

    if dapil_files:
        st.markdown("**Excel per Dapil**")
        for path in dapil_files:
            download_button_for_file(path, f"Download {path.name}", f"download_{path.name}")
else:
    st.caption("Belum ada Excel hasil. Hasil akan muncul otomatis setelah PDF pertama selesai.")

st.divider()
st.subheader("Struktur folder yang dibaca")
st.code(
    "FOLDER PILEG DPR\n"
    "└── DAPIL\n"
    "    └── KAB/KOTA\n"
    "        └── KECAMATAN\n"
    "            └── FILE PDF",
    language="text",
)
