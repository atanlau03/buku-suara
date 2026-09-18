"""
PEMINDAI DATA
=============

PDF KPU
 ↓
OCR Lokal
 ↓
Analisis Struktur
 ↓
Validasi
 ↓
Database
 ↓
Dashboard
"""

import os
import json
import re
import sys
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

from streamlit.runtime.scriptrunner import (
    add_script_run_ctx,
)


# ============================================================
# BASE DIRECTORY
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(BASE_DIR),
    )


# ============================================================
# DATABASE
# ============================================================

from database import (
    import_dataframe,
    init_db,
)


# ============================================================
# LOCAL OCR SAJA
# ============================================================

from local_ocr_engine import (
    process_pdf_local_engine,
)
from excel_export import build_excel


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

init_db()


# ============================================================
# FOLDER PDF
# ============================================================

UPLOAD_DIR = (
    BASE_DIR
    / "uploaded_pdfs"
)

UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

SCAN_RESULTS_DIR = BASE_DIR / "scan_results"
SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Buku Suara - Pemindai Data",
    layout="wide",
)


# ============================================================
# HEADER
# ============================================================

st.title(
    "Pemindai Data Hasil Suara"
)

st.caption(
    "PDF → OCR Lokal → Struktur Dokumen → "
    "Desa → TPS → Partai → Validasi → Database"
)


# ============================================================
# STATE
# ============================================================

if "bg_status" not in st.session_state:

    st.session_state[
        "bg_status"
    ] = {
        "running": False,
        "progress": 0,
        "message": "Siap memproses.",
    }


if "scan_result" not in st.session_state:

    st.session_state[
        "scan_result"
    ] = None


# ============================================================
# PENYIMPANAN HASIL PEMBACAAN
# ============================================================

def _archive_name(file_name):
    stem = Path(file_name).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return (safe or "hasil_scan") + ".json"


def save_scan_result(file_name, result, dpi_val):
    """Simpan hasil pembacaan agar bisa dibuka kembali dari Dashboard."""
    archive = {
        "nama_file_pdf": file_name,
        "waktu_scan": pd.Timestamp.now().isoformat(),
        "dpi": int(dpi_val),
        "hasil": {},
    }

    for key, value in result.items():
        if isinstance(value, pd.DataFrame):
            archive["hasil"][key] = json.loads(
                value.to_json(orient="records", date_format="iso")
            )
        else:
            archive["hasil"][key] = value

    output_path = SCAN_RESULTS_DIR / _archive_name(file_name)
    temp_path = output_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(output_path)


# ============================================================
# BACKGROUND WORKER
# ============================================================

def worker_process(
    selected_files,
    dpi_val,
):

    status = st.session_state[
        "bg_status"
    ]

    status[
        "running"
    ] = True

    status[
        "progress"
    ] = 0

    status[
        "message"
    ] = "Menyiapkan OCR lokal..."


    file_results = []

    buckets = {
        "summary": [],
        "db": [],
        "ranges": [],
        "tps": [],
        "parties": [],
        "validation": [],
        "raw": [],
    }


    total_files = max(
        1,
        len(selected_files),
    )


    for file_idx, file_name in enumerate(
        selected_files,
        start=1,
    ):

        file_path = (
            UPLOAD_DIR
            / file_name
        )

        try:

            # =================================================
            # BACA FILE
            # =================================================

            with open(
                file_path,
                "rb",
            ) as input_file:

                file_bytes = (
                    input_file.read()
                )


            # =================================================
            # CALLBACK
            # =================================================

            def callback(
                page_progress,
                message,
            ):

                try:

                    total_progress = (
                        (
                            file_idx - 1
                        )
                        / total_files
                        * 100
                    ) + (
                        page_progress
                        / total_files
                    )

                    status[
                        "progress"
                    ] = min(
                        100,
                        int(
                            total_progress
                        ),
                    )

                    status[
                        "message"
                    ] = (
                        f"[{file_idx}/{total_files}] "
                        f"{message}"
                    )

                except Exception:
                    pass


            # =================================================
            # PROCESS PDF
            # =================================================

            result = (
                process_pdf_local_engine(
                    file_bytes,
                    file_name,
                    callback,
                    dpi_val,
                )
            )

            file_results.append((file_name, result))

            # Simpan hasil lengkap secara permanen untuk dibuka kembali
            # dari Dashboard tanpa upload atau scan ulang.
            save_scan_result(
                file_name,
                result,
                dpi_val,
            )


            # =================================================
            # KUMPULKAN HASIL
            # =================================================

            for key in buckets:

                value = result.get(
                    key
                )

                if (
                    isinstance(
                        value,
                        pd.DataFrame,
                    )
                    and not value.empty
                ):

                    buckets[
                        key
                    ].append(
                        value
                    )


            # =================================================
            # DATABASE
            # =================================================

            db_result = result.get(
                "db"
            )

            if (
                isinstance(
                    db_result,
                    pd.DataFrame,
                )
                and not db_result.empty
            ):

                import_dataframe(
                    db_result,
                    sumber=(
                        "Scan PDF (OCR Lokal)"
                    ),
                    nama_file_asal=file_name,
                )


        except Exception as error:

            status[
                "message"
            ] = (
                f"Gagal memproses "
                f"{file_name}: {error}"
            )


    # =========================================================
    # GABUNGKAN HASIL
    # =========================================================

    st.session_state[
        "scan_file_results"
    ] = file_results

    try:
        st.session_state["excel_bytes"] = build_excel(file_results)
    except Exception as excel_error:
        st.session_state["excel_bytes"] = None
        status["message"] = f"OCR selesai, tetapi Excel gagal dibuat: {excel_error}"

    st.session_state[
        "scan_result"
    ] = {

        key: (
            pd.concat(
                values,
                ignore_index=True,
            )
            if values
            else pd.DataFrame()
        )

        for key, values
        in buckets.items()
    }


    # =========================================================
    # SELESAI
    # =========================================================

    status[
        "progress"
    ] = 100

    status[
        "running"
    ] = False

    status[
        "message"
    ] = (
        "Selesai! "
        "PDF telah dibaca dengan OCR lokal "
        "dan data disimpan ke database serta arsip hasil pembacaan."
    )


# ============================================================
# PENGATURAN PEMINDAI
# ============================================================

st.subheader(
    "Pengaturan Pemindai Data"
)


st.info(
    "Mesin pembacaan menggunakan OCR lokal "
    "Tesseract + analisis struktur dokumen. "
    "Tidak menggunakan Gemini atau API AI."
)


dpi = st.slider(
    "DPI PDF → gambar",
    min_value=220,
    max_value=400,
    value=300,
    step=10,
    help=(
        "300 DPI direkomendasikan untuk "
        "scan formulir KPU."
    ),
)


st.divider()


# ============================================================
# INPUT PDF
# ============================================================

st.subheader(
    "Input PDF"
)


uploaded = st.file_uploader(
    "Pilih PDF hasil rekap KPU",
    type=["pdf"],
    accept_multiple_files=True,
    key="pdf_repo_uploader",
)


selected = []


if not uploaded:

    st.info(
        "Upload satu atau beberapa PDF "
        "untuk memulai."
    )

else:

    # ========================================================
    # SIMPAN PDF
    # ========================================================

    for uploaded_file in uploaded:

        file_path = (
            UPLOAD_DIR
            / uploaded_file.name
        )

        with open(
            file_path,
            "wb",
        ) as output_file:

            output_file.write(
                uploaded_file.getbuffer()
            )


    # ========================================================
    # PILIH FILE
    # ========================================================

    selected = st.multiselect(
        "File yang diproses",
        [
            uploaded_file.name
            for uploaded_file
            in uploaded
        ],
        default=[
            uploaded_file.name
            for uploaded_file
            in uploaded
        ],
    )


    is_running = st.session_state[
        "bg_status"
    ][
        "running"
    ]


    # ========================================================
    # MULAI
    # ========================================================

    if st.button(
        "Mulai Proses Otomatis",
        type="primary",
        use_container_width=True,
        disabled=is_running,
    ):

        if not selected:

            st.warning(
                "Pilih minimal satu file."
            )

        else:

            # ------------------------------------------------
            # RESET STATUS
            # ------------------------------------------------

            st.session_state[
                "bg_status"
            ] = {
                "running": True,
                "progress": 0,
                "message": (
                    "Memulai OCR lokal..."
                ),
            }

            st.session_state[
                "scan_result"
            ] = None
            st.session_state["scan_file_results"] = []
            st.session_state["excel_bytes"] = None


            # ------------------------------------------------
            # THREAD
            # ------------------------------------------------

            thread = threading.Thread(
                target=worker_process,
                args=(
                    selected,
                    dpi,
                ),
                daemon=True,
            )

            add_script_run_ctx(
                thread
            )

            thread.start()

            st.rerun()


# ============================================================
# PROGRESS
# ============================================================

@st.fragment(
    run_every="1s"
)
def render_progress_section():

    status = st.session_state.get(
        "bg_status",
        {},
    )


    if status.get(
        "running"
    ):

        st.markdown(
            "---"
        )

        with st.status(
            "📄 **OCR lokal sedang membaca PDF...**",
            expanded=True,
        ):

            st.write(
                "**Status:** "
                + str(
                    status.get(
                        "message",
                        "Memproses...",
                    )
                )
            )

            st.progress(
                status.get(
                    "progress",
                    0,
                )
            )

            st.caption(
                "PDF dibaca secara lokal "
                "tanpa koneksi Gemini/API."
            )


    elif status.get(
        "progress"
    ) == 100:

        st.success(
            status.get(
                "message",
                "Selesai.",
            )
        )

        # Fragment ini ikut melakukan polling. Karena worker berjalan di
        # background, tombol download ditempatkan di sini agar langsung
        # muncul begitu proses selesai tanpa perlu refresh browser manual.
        excel_bytes = st.session_state.get("excel_bytes")
        if excel_bytes:
            st.divider()
            st.subheader("⬇️ Excel Hasil Rekap")
            st.download_button(
                label="📥 Download Excel Otomatis",
                data=excel_bytes,
                file_name="hasil_rekap_kpu.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
                key="download_excel_progress",
            )


render_progress_section()


# ============================================================
# HASIL
# ============================================================

result = st.session_state.get(
    "scan_result"
)


if result:

    summary = result.get(
        "summary",
        pd.DataFrame(),
    )

    ranges = result.get(
        "ranges",
        pd.DataFrame(),
    )

    parties = result.get(
        "parties",
        pd.DataFrame(),
    )

    validation = result.get(
        "validation",
        pd.DataFrame(),
    )


    st.divider()

    st.header(
        "Hasil Pembacaan Otomatis"
    )


    # ========================================================
    # SUMMARY
    # ========================================================

    if not summary.empty:

        c1, c2, c3 = st.columns(
            3
        )

        c1.metric(
            "Total Halaman",
            f"{int(summary['jumlah_halaman'].sum()):,}",
        )

        c2.metric(
            "Kelurahan/Desa",
            f"{int(summary['jumlah_kelurahan'].sum()):,}",
        )

        c3.metric(
            "TPS",
            f"{int(summary['jumlah_tps'].sum()):,}",
        )


        st.subheader(
            "1. Ringkasan per File"
        )

        st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True,
        )


    # ========================================================
    # RANGE DESA
    # ========================================================

    if not ranges.empty:

        st.subheader(
            "2. Rentang Halaman per Kelurahan/Desa"
        )

        st.dataframe(
            ranges,
            use_container_width=True,
            hide_index=True,
        )


    # ========================================================
    # PARTAI
    # ========================================================

    if not parties.empty:

        st.subheader(
            "3. Suara Partai per TPS dan Rekap Kelurahan"
        )

        # Tandai halaman yang hasil suara akhirnya perlu diperiksa ulang.
        # Tanda ^ hanya ditampilkan pada halaman yang status validasinya
        # bukan OK. Data asli di result tetap tidak diubah.
        parties_display = parties.copy()

        # Tambahkan rekap suara kelurahan/desa ke setiap baris partai.
        # Sumbernya adalah tabel ranges pada Point 2, sehingga nilai
        # suara sah, tidak sah, dan total suara konsisten dengan rekap desa.
        village_vote_columns = [
            "kelurahan",
            "suara_sah",
            "suara_tidak_sah",
            "total_suara",
        ]

        if (
            "kelurahan" in parties_display.columns
            and all(column in ranges.columns for column in village_vote_columns)
        ):
            village_votes = ranges[village_vote_columns].copy()
            village_votes = village_votes.drop_duplicates(subset=["kelurahan"])

            # Hapus kolom lama jika ternyata sudah ada di data parties,
            # lalu ambil nilai resmi dari rekap kelurahan pada Point 2.
            for column in [
                "suara_sah",
                "suara_tidak_sah",
                "total_suara",
            ]:
                if column in parties_display.columns:
                    parties_display = parties_display.drop(columns=[column])

            parties_display = parties_display.merge(
                village_votes,
                on="kelurahan",
                how="left",
            )

        # Susun kolom agar informasi suara kelurahan langsung terlihat
        # setelah suara akhir partai.
        preferred_columns = [
            "kelurahan",
            "nama_partai",
            "no_partai",
            "suara_akhir_partai",
            "suara_sah",
            "suara_tidak_sah",
            "total_suara",
            "halaman",
            "provinsi",
            "dapil",
            "kab_kota",
            "kecamatan",
            "status_validasi",
        ]

        ordered_columns = [
            column
            for column in preferred_columns
            if column in parties_display.columns
        ]

        remaining_columns = [
            column
            for column in parties_display.columns
            if column not in ordered_columns
        ]

        parties_display = parties_display[
            ordered_columns + remaining_columns
        ]

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
            ] = (
                parties_display.loc[
                    perlu_dicek,
                    "halaman",
                ]
                .apply(
                    lambda value: (
                        f"^ {value}"
                        if pd.notna(value)
                        and not str(value).strip().startswith("^")
                        else value
                    )
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
                st.caption(
                    "^ = halaman dengan suara akhir partai yang perlu dilihat ulang. "
                    f"Jumlah: {jumlah_perlu_dicek} data."
                )


    # ========================================================
    # EXCEL OTOMATIS
    # ========================================================

    excel_bytes = st.session_state.get("excel_bytes")

    if excel_bytes:
        st.divider()
        st.subheader("⬇️ Excel Hasil Rekap")
        st.success(
            "Excel sudah dibuat otomatis dari PDF yang diproses. "
            "Sheet dipisahkan berdasarkan dapil dan angka divalidasi secara logika."
        )
        st.download_button(
            label="📥 Download Excel Otomatis",
            data=excel_bytes,
            file_name="hasil_rekap_kpu.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )


    # ========================================================
    # VALIDASI
    # ========================================================

    if not validation.empty:

        st.subheader(
            "4. Validasi Pembacaan"
        )


        if "status" in validation.columns:

            perlu = validation[
                validation[
                    "status"
                ]
                .astype(str)
                .str.contains(
                    "PERLU DICEK",
                    na=False,
                )
            ]

        else:

            perlu = pd.DataFrame()


        if not perlu.empty:

            st.warning(
                f"{len(perlu):,} "
                "halaman perlu diperiksa."
            )

            st.dataframe(
                perlu,
                use_container_width=True,
                hide_index=True,
            )

        else:

            st.success(
                "Semua data lolos validasi dasar."
            )