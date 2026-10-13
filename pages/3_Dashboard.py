import io
import base64
from pathlib import Path

import pandas as pd
import streamlit as st

from database import (
    delete_by_file,
    get_full_dataframe,
    init_db,
    list_uploaded_files,
)

from scan_archive import archive_name, load_saved_result, SCAN_RESULTS_DIR
from results_view import render_results

from ui_theme import (
    apply_theme,
    breadcrumb,
    header,
)

st.set_page_config(
    page_title="Buku Suara - Dashboard",
    layout="wide",
)

apply_theme()
init_db()

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploaded_pdfs"


UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


header(
    "Dashboard",
    "Lihat kembali PDF yang sudah diproses dan hasil pembacaan yang tersimpan",
)



def list_pdf_files():
    rows = []

    for path in sorted(
        UPLOAD_DIR.glob("*.pdf"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        rows.append(
            {
                "nama_file_asal": path.name,
                "waktu_file": pd.Timestamp.fromtimestamp(path.stat().st_mtime),
                "tersimpan": True,
                "hasil_tersimpan": (SCAN_RESULTS_DIR / archive_name(name)).exists(),
            }
        )

    return pd.DataFrame(rows)


if "file_terpilih_dashboard" not in st.session_state:
    st.session_state["file_terpilih_dashboard"] = None


pdf_files = list_pdf_files()
db_files = list_uploaded_files()

# Gabungkan daftar PDF fisik dengan daftar file di database.
known_names = set(pdf_files["nama_file_asal"].astype(str)) if not pdf_files.empty else set()

if not db_files.empty:
    for _, row in db_files.iterrows():
        name = str(row.get("nama_file_asal", "")).strip()

        if not name or name in known_names:
            continue

        pdf_files = pd.concat(
            [
                pdf_files,
                pd.DataFrame(
                    [
                        {
                            "nama_file_asal": name,
                            "waktu_file": row.get("waktu_upload_terakhir", ""),
                            "tersimpan": False,
                            "hasil_tersimpan": False,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )

if pdf_files.empty:
    st.info("Belum ada PDF tersimpan. Upload PDF baru melalui menu Pemindai Data.")
    st.stop()


# ============================================================
# FILE PDF TERSIMPAN
# ============================================================

st.subheader("File PDF Tersimpan")

st.caption(
    "PDF yang sudah diproses di Pemindai Data tersimpan di sistem. "
    "Pilih file untuk melihat kembali hasil pembacaannya tanpa upload ulang."
)

selected_file = st.session_state.get("file_terpilih_dashboard")

for idx, row in pdf_files.iterrows():
    file_name = str(row["nama_file_asal"])
    is_selected = file_name == selected_file

    c1, c2, c3, c4 = st.columns([4.5, 2.5, 2, 2])

    with c1:
        st.write(file_name)

    with c2:
        if bool(row.get("hasil_tersimpan", False)):
            st.write("Hasil tersimpan")
        else:
            st.write("Data database")

    with c3:
        if is_selected:
            st.button(
                "Sedang dilihat",
                key=f"selected_{idx}_{file_name}",
                disabled=True,
                use_container_width=True,
            )
        else:
            if st.button(
                "Lihat Hasil",
                key=f"view_{idx}_{file_name}",
                type="primary",
                use_container_width=True,
            ):
                st.session_state["file_terpilih_dashboard"] = file_name
                st.rerun()

    with c4:
        if st.button(
            "Hapus",
            key=f"delete_{idx}_{file_name}",
            use_container_width=True,
        ):
            delete_by_file(file_name)

            archive_path = SCAN_RESULTS_DIR / archive_name(file_name)
            if archive_path.exists():
                try:
                    archive_path.unlink()
                except Exception:
                    pass

            pdf_path = UPLOAD_DIR / file_name
            if pdf_path.exists():
                try:
                    pdf_path.unlink()
                except Exception:
                    pass

            if is_selected:
                st.session_state["file_terpilih_dashboard"] = None

            st.rerun()

    st.divider()


file_aktif = st.session_state.get("file_terpilih_dashboard")

if not file_aktif:
    st.info("Pilih salah satu PDF untuk melihat hasil pembacaan.")
    st.stop()


# ============================================================
# LOAD HASIL TERSIMPAN
# ============================================================

saved = load_saved_result(file_aktif)

if saved is not None:
    result = saved["hasil"]

    st.success(f"Hasil pembacaan tersimpan untuk: {file_aktif}")

    meta1, meta2 = st.columns(2)
    with meta1:
        st.write(f"**Waktu pembacaan:** {saved.get('waktu_scan', '-')}")
    with meta2:
        st.write(f"**DPI:** {saved.get('dpi', '-')}")

else:
    df_all = get_full_dataframe()

    if df_all.empty or "nama_file_asal" not in df_all.columns:
        st.warning(f"Belum ada hasil pembacaan tersimpan untuk '{file_aktif}'.")
        st.stop()

    df_fallback = df_all[
        df_all["nama_file_asal"].astype(str) == str(file_aktif)
    ].copy()

    if df_fallback.empty:
        st.warning(f"Belum ada hasil pembacaan untuk '{file_aktif}'.")
        st.stop()

    st.warning(
        "File ini berasal dari data lama. Detail arsip hasil pembacaan "
        "belum tersedia, tetapi data database masih dapat ditampilkan."
    )

    result = {"tps": df_fallback}


pdf_path = UPLOAD_DIR / file_aktif

if pdf_path.exists():
    with st.expander("Buka PDF asli"):
        encoded = base64.b64encode(pdf_path.read_bytes()).decode("utf-8")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{encoded}" '
            f'width="100%" height="700" type="application/pdf"></iframe>',
            unsafe_allow_html=True,
        )


st.divider()
st.header("Hasil Pembacaan Otomatis")
st.caption(f"File: {file_aktif}")

def build_excel_bytes(hasil):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in hasil.items():
            if name == "raw" or not isinstance(df, pd.DataFrame) or df.empty:
                continue
            df.to_excel(writer, sheet_name=str(name)[:31], index=False)
    return buffer.getvalue()


excel_bytes = build_excel_bytes(result)

if excel_bytes:
    st.download_button(
        "Unduh hasil file ini (Excel)",
        data=excel_bytes,
        file_name=f"{Path(file_aktif).stem[:80]}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_excel_dashboard",
    )
    
render_results(result)

# ============================================================
# PDF ASLI
# ============================================================

pdf_path = UPLOAD_DIR / file_aktif

if pdf_path.exists():
    with st.expander("Buka PDF asli"):
        pdf_bytes = pdf_path.read_bytes()
        encoded = base64.b64encode(pdf_bytes).decode("utf-8")

        st.markdown(
            f"""
            <iframe
                src="data:application/pdf;base64,{encoded}"
                width="100%"
                height="700"
                type="application/pdf">
            </iframe>
            """,
            unsafe_allow_html=True,
        )


# ============================================================
# HASIL PEMBACAAN OTOMATIS
# ============================================================

st.divider()
st.header("Hasil Pembacaan Otomatis")
st.caption(f"File: {file_aktif}")


