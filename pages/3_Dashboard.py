import base64
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from database import (
    delete_by_file,
    get_full_dataframe,
    init_db,
    list_uploaded_files,
)

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
SCAN_RESULTS_DIR = BASE_DIR / "scan_results"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

header(
    "Dashboard",
    "Lihat kembali PDF yang sudah diproses dan hasil pembacaan yang tersimpan",
)


def archive_name(file_name):
    stem = Path(file_name).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return (safe or "hasil_scan") + ".json"


def load_saved_result(file_name):
    path = SCAN_RESULTS_DIR / archive_name(file_name)

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hasil = data.get("hasil", {})

        result = {}
        for key, value in hasil.items():
            if isinstance(value, list):
                result[key] = pd.DataFrame(value)
            else:
                result[key] = value

        return {
            "nama_file_pdf": data.get("nama_file_pdf", file_name),
            "waktu_scan": data.get("waktu_scan", ""),
            "dpi": data.get("dpi", ""),
            "hasil": result,
        }
    except Exception:
        return None


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
                "hasil_tersimpan": (SCAN_RESULTS_DIR / archive_name(path.name)).exists(),
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

df_all = get_full_dataframe()

if saved is not None:
    result = saved["hasil"]

    summary = result.get("summary", pd.DataFrame())
    ranges = result.get("ranges", pd.DataFrame())
    parties = result.get("parties", pd.DataFrame())
    validation = result.get("validation", pd.DataFrame())
    tps = result.get("tps", pd.DataFrame())

    st.success(
        f"Hasil pembacaan tersimpan untuk: {file_aktif}"
    )

    meta1, meta2 = st.columns(2)

    with meta1:
        st.write(f"**Waktu pembacaan:** {saved.get('waktu_scan', '-')}")
    with meta2:
        st.write(f"**DPI:** {saved.get('dpi', '-')}")

else:
    # Fallback untuk data lama yang belum mempunyai arsip JSON.
    # Data yang masih ada di database tetap bisa dilihat.
    if df_all.empty or "nama_file_asal" not in df_all.columns:
        st.warning(
            f"Belum ada hasil pembacaan tersimpan untuk '{file_aktif}'."
        )
        st.stop()

    df_file_fallback = df_all[
        df_all["nama_file_asal"].astype(str) == str(file_aktif)
    ].copy()

    if df_file_fallback.empty:
        st.warning(
            f"Belum ada hasil pembacaan untuk '{file_aktif}'."
        )
        st.stop()

    st.warning(
        "File ini berasal dari data lama. Detail arsip hasil pembacaan "
        "belum tersedia, tetapi data database masih dapat ditampilkan."
    )

    summary = pd.DataFrame()
    ranges = pd.DataFrame()
    parties = pd.DataFrame()
    validation = pd.DataFrame()
    tps = df_file_fallback


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


# ============================================================
# SUMMARY
# ============================================================

if not summary.empty:
    st.subheader("1. Ringkasan per File")

    c1, c2, c3 = st.columns(3)

    if "jumlah_halaman" in summary.columns:
        total_halaman = int(
            pd.to_numeric(
                summary["jumlah_halaman"],
                errors="coerce",
            ).fillna(0).sum()
        )
    else:
        total_halaman = 0

    if "jumlah_kelurahan" in summary.columns:
        total_kelurahan = int(
            pd.to_numeric(
                summary["jumlah_kelurahan"],
                errors="coerce",
            ).fillna(0).sum()
        )
    else:
        total_kelurahan = (
            int(ranges["kelurahan"].nunique())
            if "kelurahan" in ranges.columns
            else 0
        )

    if "jumlah_tps" in summary.columns:
        total_tps = int(
            pd.to_numeric(
                summary["jumlah_tps"],
                errors="coerce",
            ).fillna(0).sum()
        )
    else:
        total_tps = (
            int(tps["tps"].nunique())
            if "tps" in tps.columns
            else 0
        )

    c1.metric("Total Halaman", f"{total_halaman:,}")
    c2.metric("Kelurahan/Desa", f"{total_kelurahan:,}")
    c3.metric("TPS", f"{total_tps:,}")

    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# RANGE KELURAHAN
# ============================================================

if not ranges.empty:
    st.subheader("2. Rentang Halaman per Kelurahan/Desa")

    st.dataframe(
        ranges,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# SUARA PARTAI
# ============================================================

if not parties.empty:
    st.subheader("3. Suara Partai dan Rekap Kelurahan")

    parties_display = parties.copy()

    if (
        "halaman" in parties_display.columns
        and "status_validasi" in parties_display.columns
    ):
        perlu_dicek = (
            parties_display["status_validasi"]
            .astype(str)
            .str.strip()
            .ne("OK")
        )

        parties_display.loc[
            perlu_dicek,
            "halaman",
        ] = parties_display.loc[
            perlu_dicek,
            "halaman",
        ].apply(
            lambda value: (
                f"^ {value}"
                if pd.notna(value)
                and not str(value).strip().startswith("^")
                else value
            )
        )

    st.dataframe(
        parties_display,
        use_container_width=True,
        hide_index=True,
    )

    if "status_validasi" in parties_display.columns:
        jumlah_perlu_dicek = int(
            parties_display["status_validasi"]
            .astype(str)
            .str.strip()
            .ne("OK")
            .sum()
        )

        if jumlah_perlu_dicek > 0:
            st.warning(
                f"{jumlah_perlu_dicek:,} data mempunyai halaman "
                "yang perlu dilihat ulang."
            )
            st.caption(
                "^ = halaman dengan suara akhir partai yang perlu dilihat ulang."
            )


# ============================================================
# VALIDASI
# ============================================================

if not validation.empty:
    st.subheader("4. Validasi Pembacaan")

    if "status" in validation.columns:
        perlu = validation[
            validation["status"]
            .astype(str)
            .str.contains("PERLU DICEK", na=False)
        ]
    else:
        perlu = pd.DataFrame()

    if not perlu.empty:
        st.warning(
            f"{len(perlu):,} data perlu diperiksa."
        )

        st.dataframe(
            perlu,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("Semua data lolos validasi dasar.")

# ============================================================
# DETAIL TPS
# ============================================================

if not tps.empty:
    st.subheader("5. Data per TPS")

    if saved is not None:
        tps_display = tps.copy()

        st.dataframe(
            tps_display,
            use_container_width=True,
            hide_index=True,
        )
    else:
        detail_columns = [
            "provinsi",
            "dapil",
            "kab_kota",
            "kecamatan",
            "kelurahan",
            "tps",
            "no_partai",
            "partai",
            "jumlah_suara",
        ]

        available_columns = [
            col for col in detail_columns if col in tps.columns
        ]

        detail_df = tps[available_columns].copy()

        detail_df = detail_df.rename(
            columns={
                "provinsi": "Provinsi",
                "dapil": "Dapil",
                "kab_kota": "Kabupaten/Kota",
                "kecamatan": "Kecamatan",
                "kelurahan": "Kelurahan/Desa",
                "tps": "TPS",
                "no_partai": "No. Partai",
                "partai": "Partai",
                "jumlah_suara": "Jumlah Suara",
            }
        )

        sort_columns = [
            col
            for col in [
                "Kelurahan/Desa",
                "TPS",
                "No. Partai",
            ]
            if col in detail_df.columns
        ]

        if sort_columns:
            detail_df = detail_df.sort_values(sort_columns)

        st.dataframe(
            detail_df,
            use_container_width=True,
            hide_index=True,
        )
