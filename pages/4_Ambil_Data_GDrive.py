"""
AMBIL DATA DARI GOOGLE DRIVE (BATCH PER DAPIL)
==============================================

Google Drive / Folder lokal
 ↓
Pilih 1 Dapil
 ↓
Kabupaten → Kecamatan → PDF
 ↓
Cek Text Layer  (buruk → DILEWATI, dicatat di checkpoint)
 ↓
OCR Lokal (engine yang sama dengan Pemindai Data)
 ↓
Database + Arsip Dashboard + Excel per dapil

PENYIMPANAN PERMANEN
--------------------
* Pengaturan (link folder Drive, lokasi hasil, daftar dapil, dapil terakhir
  yang dipilih) tersimpan otomatis di  gdrive_settings.json  dan dimuat lagi
  setiap halaman dibuka -- termasuk setelah refresh / pindah halaman.
* Riwayat dapil yang sudah diproses dibaca langsung dari file checkpoint dan
  Excel hasil di disk, bukan dari memori sesi. Jadi hasilnya tetap bisa dibuka
  tanpa memproses ulang atau mengunggah apa pun.

Letakkan file ini di folder  pages/  (sejajar dengan 1_Pemindai_Data.py).
gdrive_batch.py diletakkan di folder proyek (sejajar dengan local_ocr_engine.py).
"""

import json
import os
import re
import sys
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

from dotenv import load_dotenv
load_dotenv()

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
# DATABASE + MODUL BATCH
# ============================================================

from database import (
    import_dataframe,
    init_db,
)

from gdrive_batch import (
    DriveSource,
    LocalSource,
    is_batch_running,
    list_dapil,
    parse_drive_id,
    run_dapil,
    safe_name,
    STATUS_DONE,
    STATUS_SKIP,
    STATUS_ERROR,
    STATUS_EMPTY,
)


init_db()


# ============================================================
# FOLDER
# ============================================================

SCAN_RESULTS_DIR = BASE_DIR / "scan_results"
SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_OUT_DIR = BASE_DIR / "hasil_pemilu"
DEFAULT_CHECKPOINT = BASE_DIR / "checkpoint_pemilu.xlsx"
SETTINGS_PATH = BASE_DIR / "gdrive_settings.json"

XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Buku Suara - Ambil Data dari Google Drive",
    layout="wide",
)


# ============================================================
# HEADER
# ============================================================

st.title(
    "Ambil Data dari Google Drive"
)

st.caption(
    "Drive → Dapil → Kabupaten → Kecamatan → Cek Text Layer → "
    "OCR Lokal → Database → Checkpoint"
)


# ============================================================
# PENGATURAN PERMANEN
#
# Kunci widget -> (nama di file JSON, nilai bawaan).
#
# Catatan Streamlit: nilai widget DIHAPUS dari session_state saat
# halaman ditinggalkan (pindah ke Dashboard, dsb). Karena itu setiap
# kali halaman dibuka, kunci yang hilang diisi lagi dari file
# pengaturan -- itulah yang membuat link folder tidak perlu diisi ulang.
# ============================================================

SETTING_FIELDS = {
    "gd_source_type": ("tipe", "Google Drive"),
    "gd_root_drive": ("root_drive", os.getenv("GDRIVE_ROOT_FOLDER", "")),
    "gd_root_local": ("root_local", ""),
    "gd_cred": ("credentials", str(BASE_DIR / "credentials.json")),
    "gd_token": ("token", str(BASE_DIR / "token.json")),
    "gd_sa": ("service_account", ""),
    "gd_out": ("out_dir", str(DEFAULT_OUT_DIR)),
    "gd_cp": ("checkpoint", str(DEFAULT_CHECKPOINT)),
    "gd_retry": ("retry_skipped", False),
    "gd_savedb": ("save_db", True),
    "gd_dpi": ("dpi", 300),
}


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    temp_path = SETTINGS_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(SETTINGS_PATH)


def coerce_setting(session_key, value, default):
    """Pastikan tipe nilai dari file cocok dengan widget-nya."""

    try:
        if isinstance(default, bool):
            return bool(value)

        if session_key == "gd_dpi":
            return min(400, max(220, int(value)))

        if session_key == "gd_source_type":
            return (
                value
                if value in ("Google Drive", "Folder lokal")
                else default
            )

        return str(value) if value is not None else default

    except Exception:
        return default


_missing_keys = [
    key
    for key in SETTING_FIELDS
    if key not in st.session_state
]

if _missing_keys:

    _saved = load_settings()

    for _key in _missing_keys:

        _name, _default = SETTING_FIELDS[_key]

        st.session_state[_key] = coerce_setting(
            _key,
            _saved.get(_name, _default),
            _default,
        )

if "gd_dapil_list" not in st.session_state:

    _saved = load_settings()

    st.session_state["gd_dapil_list"] = [
        str(name)
        for name in _saved.get("dapil_list", [])
        if name
    ]

    st.session_state["gd_last_dapil"] = _saved.get("last_dapil")

    st.session_state["gd_settings_last"] = _saved


# ============================================================
# STATE
#
# Semua halaman berbagi st.session_state, jadi semua kunci di
# halaman ini diawali "gd_" supaya tidak bentrok dengan
# halaman Pemindai Data (bg_status, scan_result, dst).
# ============================================================

def new_status():
    return {
        "running": False,
        "finished": False,
        "progress": 0,
        "message": "Siap memproses.",
        "log": [],
        "cancel": False,
        "result": None,
        "error": None,
    }


if "gd_status" not in st.session_state:
    st.session_state["gd_status"] = new_status()

if "gd_notified" not in st.session_state:
    st.session_state["gd_notified"] = False


# ============================================================
# ARSIP HASIL (format sama dengan 1_Pemindai_Data.py)
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
# SUMBER DATA
# ============================================================

def make_source(cfg):
    """
    Buat objek sumber data BARU.

    Klien Google API tidak aman dipakai bersamaan dari dua thread,
    jadi thread halaman dan thread proses masing-masing membuat
    sumber sendiri (token.json dipakai bersama, tanpa login ulang).
    """

    if cfg["tipe"] == "Folder lokal":
        return LocalSource()

    return DriveSource(
        credentials_file=cfg["credentials"],
        token_file=cfg["token"],
        service_account_file=cfg["service_account"] or None,
    )


def resolve_root(cfg):
    if cfg["tipe"] == "Folder lokal":
        return cfg["root"].strip()

    return parse_drive_id(cfg["root"])


# ============================================================
# BACKGROUND WORKER
# ============================================================

def gd_worker(status, cfg):

    status["running"] = True
    status["finished"] = False
    status["progress"] = 0
    status["message"] = "Menghubungkan ke sumber data..."

    def log(message):
        status["log"].append(str(message))
        del status["log"][:-300]

    def progress_callback(value, message=""):
        status["progress"] = min(
            99,
            int(float(value) * 100),
        )
        status["message"] = str(message or "")

    def on_result(job, hasil, result_path):
        """Simpan ke arsip Dashboard + database, seperti Pemindai Data."""

        unique_name = (
            f"{job.dapil}__{job.kabupaten}__"
            f"{job.kecamatan}__{job.file.name}"
        )

        save_scan_result(
            unique_name,
            hasil,
            cfg["dpi"],
        )

        db_result = hasil.get("db")

        if (
            isinstance(db_result, pd.DataFrame)
            and not db_result.empty
        ):
            import_dataframe(
                db_result,
                sumber="Scan PDF (OCR Lokal)",
                nama_file_asal=unique_name,
            )

    try:

        source = make_source(cfg)

        result = run_dapil(
            source,
            resolve_root(cfg),
            cfg["dapil"],
            out_dir=cfg["out_dir"],
            checkpoint_path=cfg["checkpoint"],
            dpi=cfg["dpi"],
            retry_skipped=cfg["retry_skipped"],
            progress_callback=progress_callback,
            log=log,
            on_result=on_result if cfg["save_db"] else None,
            should_stop=lambda: status.get("cancel", False),
        )

        status["result"] = result

        if result.get("stopped"):
            status["message"] = (
                "Dihentikan. Kemajuan tersimpan di checkpoint; "
                "jalankan lagi untuk melanjutkan."
            )
        else:
            status["message"] = (
                f"Dapil {result['dapil']} selesai diproses."
            )

    except Exception as error:

        status["error"] = f"{type(error).__name__}: {error}"
        status["message"] = f"GAGAL: {status['error']}"

    finally:

        status["progress"] = 100
        status["running"] = False
        status["finished"] = True


# ============================================================
# SUMBER DATA (UI)
#
# Semua widget SELALU dirender (yang tidak dipakai hanya
# di-disable) supaya nilainya tidak hilang saat tipe sumber diganti.
# ============================================================

st.subheader(
    "Sumber Data"
)

tipe = st.radio(
    "Ambil data dari",
    ["Google Drive", "Folder lokal"],
    horizontal=True,
    key="gd_source_type",
    help=(
        "Pilih 'Folder lokal' bila folder Drive sudah diunduh atau "
        "memakai Google Drive for Desktop."
    ),
)

use_drive = tipe == "Google Drive"

root_drive = st.text_input(
    "Folder induk Google Drive (URL atau ID)",
    key="gd_root_drive",
    disabled=not use_drive,
    help=(
        "Folder yang berisi folder-folder dapil. Tersimpan otomatis, "
        "jadi cukup diisi sekali."
    ),
)

root_local = st.text_input(
    "Folder lokal induk (berisi folder-folder dapil)",
    key="gd_root_local",
    disabled=use_drive,
)

with st.expander("Autentikasi Google Drive"):

    credentials_path = st.text_input(
        "File credentials OAuth",
        key="gd_cred",
        disabled=not use_drive,
    )

    token_path = st.text_input(
        "File token (dibuat otomatis saat login pertama)",
        key="gd_token",
        disabled=not use_drive,
    )

    service_account_path = st.text_input(
        "File service account (opsional, kosongkan bila memakai OAuth)",
        key="gd_sa",
        disabled=not use_drive,
    )

    st.caption(
        "credentials.json: "
        + ("ditemukan" if os.path.exists(credentials_path) else "BELUM ADA")
        + " | token.json: "
        + ("sudah ada (tidak perlu login ulang)" if os.path.exists(token_path) else "belum ada (login saat pertama kali memuat dapil)")
        + ". Langkah pembuatan credentials ada di bagian atas gdrive_batch.py."
    )

root_text = root_drive if use_drive else root_local

base_cfg = {
    "tipe": tipe,
    "root": root_text,
    "credentials": credentials_path,
    "token": token_path,
    "service_account": service_account_path,
}


st.caption(
    "Link folder, lokasi hasil, dan daftar dapil tersimpan otomatis "
    f"di {SETTINGS_PATH.name}."
)


if st.button(
    "Muat daftar dapil",
    key="gd_btn_load",
    disabled=not root_text.strip(),
    help="Baca ulang daftar dapil dari folder induk (mis. bila ada dapil baru).",
):

    try:

        with st.spinner(
            "Menghubungkan dan membaca daftar dapil "
            "(login Google akan terbuka di browser pada pemakaian pertama)..."
        ):

            names = [
                node.name
                for node in list_dapil(
                    make_source(base_cfg),
                    resolve_root(base_cfg),
                )
            ]

        st.session_state["gd_dapil_list"] = names

        if not names:
            st.warning(
                "Tidak ada folder dapil di lokasi tersebut."
            )

    except Exception as error:

        st.error(
            f"Gagal membaca sumber data: {type(error).__name__}: {error}"
        )


dapil_names = st.session_state["gd_dapil_list"]

dapil_selected = None

if dapil_names:

    # Pulihkan pilihan terakhir bila nilai widget hilang / tidak valid.
    if st.session_state.get("gd_dapil_select") not in dapil_names:
        last = st.session_state.get("gd_last_dapil")
        st.session_state["gd_dapil_select"] = (
            last if last in dapil_names else dapil_names[0]
        )

    dapil_selected = st.selectbox(
        "Dapil yang dikerjakan (satu dapil per proses)",
        dapil_names,
        key="gd_dapil_select",
    )

    st.session_state["gd_last_dapil"] = dapil_selected

else:

    st.info(
        "Isi folder induk lalu klik 'Muat daftar dapil'."
    )


# ============================================================
# PENGATURAN
# ============================================================

with st.expander("Pengaturan lanjutan"):

    retry_skipped = st.checkbox(
        "Coba lagi file yang sebelumnya berstatus DILEWATI",
        key="gd_retry",
    )

    save_db = st.checkbox(
        "Simpan hasil ke database dan arsip Dashboard",
        key="gd_savedb",
        help=(
            "Sama seperti Pemindai Data. Hilangkan centang bila hanya "
            "ingin menghasilkan file Excel."
        ),
    )

    dpi = st.slider(
        "DPI PDF → gambar (hanya dipakai bila engine perlu OCR ulang)",
        min_value=220,
        max_value=400,
        step=10,
        key="gd_dpi",
    )

    out_dir = st.text_input(
        "Folder hasil (Excel per kecamatan dan gabungan per dapil)",
        key="gd_out",
    )

    checkpoint_path = st.text_input(
        "File checkpoint (Excel)",
        key="gd_cp",
    )


# ------------------------------------------------------------
# SIMPAN OTOMATIS bila ada yang berubah.
# ------------------------------------------------------------

_current_settings = {
    name: st.session_state[key]
    for key, (name, _default) in SETTING_FIELDS.items()
}

_current_settings["dapil_list"] = list(
    st.session_state.get("gd_dapil_list", [])
)

_current_settings["last_dapil"] = st.session_state.get("gd_last_dapil")

if _current_settings != st.session_state.get("gd_settings_last"):

    try:
        save_settings(_current_settings)
        st.session_state["gd_settings_last"] = _current_settings

    except Exception as error:
        st.warning(
            f"Pengaturan tidak bisa disimpan ke {SETTINGS_PATH.name}: {error}"
        )


st.divider()


# ============================================================
# MULAI PROSES
# ============================================================

status = st.session_state["gd_status"]

is_running = status.get("running", False)

server_busy = (not is_running) and is_batch_running()

if server_busy:

    st.info(
        "Masih ada proses batch yang berjalan di server (mungkin dari "
        "sesi atau tab sebelumnya). Kemajuannya tercatat di checkpoint; "
        "muat ulang halaman ini setelah selesai untuk melihat hasilnya."
    )

root_missing = not root_text.strip()

if root_missing:
    st.warning(
        (
            "Isi kolom 'Folder induk Google Drive' terlebih dahulu."
            if use_drive
            else "Isi kolom 'Folder lokal induk' terlebih dahulu."
        )
    )

if st.button(
    "Mulai Proses Dapil",
    type="primary",
    width="stretch",
    disabled=is_running or server_busy or not dapil_selected or root_missing,
    key="gd_btn_start",
):

    fresh = new_status()
    fresh["running"] = True
    fresh["message"] = "Memulai..."

    st.session_state["gd_status"] = fresh
    st.session_state["gd_notified"] = False

    cfg = dict(base_cfg)
    cfg.update(
        {
            "dapil": dapil_selected,
            "dpi": int(dpi),
            "retry_skipped": bool(retry_skipped),
            "save_db": bool(save_db),
            "out_dir": out_dir,
            "checkpoint": checkpoint_path,
        }
    )

    thread = threading.Thread(
        target=gd_worker,
        args=(fresh, cfg),
        daemon=True,
    )

    add_script_run_ctx(thread)
    thread.start()

    st.rerun()


# ============================================================
# PROGRESS
#
# Pola sama dengan Pemindai Data: fragment me-refresh dirinya
# tiap 1 detik, dan saat proses baru selesai memicu rerun
# seluruh halaman SEKALI supaya riwayat di bawah ikut diperbarui.
# ============================================================

@st.fragment(
    run_every="1s"
)
def render_progress_section():

    state = st.session_state.get(
        "gd_status",
        {},
    )

    if state.get("running"):

        st.markdown("---")

        with st.status(
            "📥 **Memproses dapil...**",
            expanded=True,
        ):

            st.write(
                "**Status:** "
                + str(state.get("message", "Memproses..."))
            )

            st.progress(
                min(100, int(state.get("progress", 0)))
            )

            recent = state.get("log", [])[-12:]

            if recent:
                st.code(
                    "\n".join(recent),
                    language=None,
                )

            if st.button(
                "Hentikan setelah kecamatan ini",
                key="gd_btn_stop",
            ):
                state["cancel"] = True

            if state.get("cancel"):
                st.caption(
                    "Permintaan berhenti diterima; menunggu kecamatan "
                    "yang sedang berjalan selesai."
                )

            st.caption(
                "Setiap kecamatan langsung dicatat ke checkpoint, jadi "
                "proses aman dihentikan dan dilanjutkan kapan saja."
            )

    elif state.get("finished"):

        message = state.get("message", "Selesai.")

        if state.get("error"):
            st.error(message)
        elif (state.get("result") or {}).get("stopped"):
            st.warning(message)
        else:
            st.success(message)

        if not st.session_state.get(
            "gd_notified",
            False,
        ):

            st.session_state["gd_notified"] = True

            finished_dapil = (state.get("result") or {}).get("dapil")

            if finished_dapil:
                # Dibaca oleh bagian riwayat di bawah pada rerun berikut.
                st.session_state["gd_focus_dapil"] = finished_dapil

            st.rerun(
                scope="app"
            )


render_progress_section()


# ============================================================
# BACA FILE HASIL DARI DISK (di-cache berdasarkan waktu ubah file)
# ============================================================

NUMERIC_COLUMNS = [
    "ukuran_mb",
    "jumlah_halaman",
    "halaman_bertext",
    "halaman_scan",
    "jumlah_kelurahan",
    "jumlah_tps",
    "kelurahan_perlu_cek",
    "durasi_detik",
]


@st.cache_data(show_spinner=False)
def _read_checkpoint(path, mtime):
    return pd.read_excel(path, sheet_name="checkpoint", dtype=str)


def load_checkpoint_df(path):

    if not path or not os.path.exists(path):
        return pd.DataFrame()

    try:
        return _read_checkpoint(path, os.path.getmtime(path))

    except Exception as error:
        st.warning(
            f"File checkpoint tidak bisa dibaca: {error}"
        )
        return pd.DataFrame()


@st.cache_data(show_spinner=False, max_entries=4)
def _read_bytes(path, mtime):
    with open(path, "rb") as handle:
        return handle.read()


@st.cache_data(show_spinner=False)
def _read_sheet_names(path, mtime):
    return list(pd.ExcelFile(path).sheet_names)


@st.cache_data(show_spinner=False, max_entries=6)
def _read_sheet(path, mtime, sheet):
    return pd.read_excel(path, sheet_name=sheet)


def normalize_rows(rows):
    """Rapikan tipe kolom supaya aman ditampilkan (data dari checkpoint
    berupa teks)."""

    df = pd.DataFrame(rows)

    for column in df.columns:

        if column in NUMERIC_COLUMNS:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        else:
            df[column] = (
                df[column]
                .fillna("")
                .astype(str)
            )

    if "status" in df.columns:
        df["status"] = df["status"].str.strip().str.upper()

    return df


def show_columns(df, columns):
    return df[[c for c in columns if c in df.columns]]


def merged_path_for(dapil, group, fallback_out_dir):
    """Lokasi Excel gabungan dapil. Diturunkan dari lokasi hasil yang
    tercatat di checkpoint (tetap benar walau folder hasil diubah)."""

    if "file_hasil" in group.columns:

        for value in group["file_hasil"].dropna():

            value = str(value).strip()

            if value:
                return (
                    Path(value).parent.parent
                    / f"GABUNGAN_{safe_name(dapil)}.xlsx"
                )

    return (
        Path(fallback_out_dir)
        / safe_name(dapil)
        / f"GABUNGAN_{safe_name(dapil)}.xlsx"
    )


def summarize_history(checkpoint_df, fallback_out_dir):

    df = checkpoint_df.copy()

    df["status"] = (
        df["status"].fillna("").str.strip().str.upper()
    )

    if "kelurahan_perlu_cek" in df.columns:
        review = pd.to_numeric(
            df["kelurahan_perlu_cek"],
            errors="coerce",
        ).fillna(0)
    else:
        review = pd.Series(0, index=df.index)

    df["_review"] = review > 0

    rows = []

    for dapil, group in df.groupby("dapil", dropna=True):

        counts = group["status"].value_counts()

        merged = merged_path_for(dapil, group, fallback_out_dir)

        rows.append(
            {
                "dapil": dapil,
                "kecamatan_total": len(group),
                "selesai": int(counts.get(STATUS_DONE, 0)),
                "dilewati": int(counts.get(STATUS_SKIP, 0)),
                "error": int(counts.get(STATUS_ERROR, 0)),
                "tanpa_pdf": int(counts.get(STATUS_EMPTY, 0)),
                "perlu_dicek": int(group["_review"].sum()),
                "terakhir_diproses": (
                    group["diproses_pada"].fillna("").max()
                    if "diproses_pada" in group.columns
                    else ""
                ),
                "hasil_gabungan": "ada" if merged.exists() else "belum ada",
            }
        )

    history = pd.DataFrame(rows)

    return history.sort_values(
        "terakhir_diproses",
        ascending=False,
    ).reset_index(drop=True)


# ============================================================
# LAPORAN SATU DAPIL (dari file, bukan dari memori sesi)
# ============================================================

def render_dapil_report(dapil, group, fallback_out_dir):

    rows_df = normalize_rows(group.to_dict("records"))

    counts = rows_df["status"].value_counts()

    review_mask = (
        rows_df.get(
            "kelurahan_perlu_cek",
            pd.Series(0, index=rows_df.index),
        )
        .fillna(0)
        .gt(0)
    )

    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric("Kecamatan Terbaca", int(counts.get(STATUS_DONE, 0)))
    c2.metric("Dilewati (text layer buruk)", int(counts.get(STATUS_SKIP, 0)))
    c3.metric("Error", int(counts.get(STATUS_ERROR, 0)))
    c4.metric("Folder Tanpa PDF", int(counts.get(STATUS_EMPTY, 0)))
    c5.metric("Terbaca, Perlu Dicek", int(review_mask.sum()))


    st.subheader(
        "1. Status per Kecamatan"
    )

    st.dataframe(
        show_columns(
            rows_df,
            [
                "kabupaten",
                "kecamatan",
                "nama_file",
                "status",
                "alasan",
                "jumlah_halaman",
                "jumlah_kelurahan",
                "jumlah_tps",
                "kelurahan_perlu_cek",
                "diproses_pada",
            ],
        ),
        width="stretch",
        hide_index=True,
    )


    hard = rows_df[
        rows_df["status"].isin([STATUS_SKIP, STATUS_ERROR])
    ]

    if not hard.empty:

        st.subheader(
            "2. Sulit Dibaca (perlu penanganan manual)"
        )

        st.warning(
            f"{len(hard):,} berkas tidak terbaca otomatis. Daftar yang "
            "sama tersimpan di sheet 'sulit_dibaca' pada file checkpoint. "
            "Ubah status di Excel menjadi ULANG untuk memprosesnya lagi."
        )

        st.dataframe(
            show_columns(
                hard,
                [
                    "kabupaten",
                    "kecamatan",
                    "nama_file",
                    "status",
                    "alasan",
                    "jumlah_halaman",
                    "halaman_bertext",
                    "halaman_scan",
                ],
            ),
            width="stretch",
            hide_index=True,
        )


    check = rows_df[review_mask]

    if not check.empty:

        st.subheader(
            "3. Terbaca, tetapi Ada Kelurahan yang Perlu Dicek"
        )

        st.dataframe(
            show_columns(
                check,
                [
                    "kabupaten",
                    "kecamatan",
                    "kelurahan_perlu_cek",
                    "kelurahan_perlu_cek_nama",
                ],
            ),
            width="stretch",
            hide_index=True,
        )


    st.subheader(
        "Hasil Tersimpan"
    )

    merged = merged_path_for(dapil, group, fallback_out_dir)

    d1, d2 = st.columns(2)

    if merged.exists():

        d1.download_button(
            "Unduh hasil gabungan dapil (Excel)",
            data=_read_bytes(str(merged), os.path.getmtime(merged)),
            file_name=merged.name,
            mime=XLSX_MIME,
            key="gd_dl_merged",
            width="stretch",
        )

    else:

        d1.info(
            "Excel gabungan dapil belum ada (belum ada kecamatan "
            "yang selesai terbaca)."
        )

    checkpoint_file = st.session_state.get("gd_cp", "")

    if checkpoint_file and os.path.exists(checkpoint_file):

        d2.download_button(
            "Unduh file checkpoint (Excel)",
            data=_read_bytes(
                checkpoint_file,
                os.path.getmtime(checkpoint_file),
            ),
            file_name=os.path.basename(checkpoint_file),
            mime=XLSX_MIME,
            key="gd_dl_checkpoint",
            width="stretch",
        )

    st.caption(
        f"Excel per kecamatan: {merged.parent}"
        " | Data juga sudah masuk database dan arsip Dashboard "
        "(bila opsi simpan ke database aktif)."
    )


    if merged.exists() and st.checkbox(
        "Tampilkan pratinjau data hasil",
        key="gd_preview",
    ):

        mtime = os.path.getmtime(merged)

        sheets = _read_sheet_names(str(merged), mtime)

        sheet = st.selectbox(
            "Sheet",
            sheets,
            index=(sheets.index("parties") if "parties" in sheets else 0),
        )

        preview = _read_sheet(str(merged), mtime, sheet)

        if "sumber_kecamatan" in preview.columns:

            picked = st.multiselect(
                "Filter kecamatan (kosong = semua)",
                sorted(preview["sumber_kecamatan"].dropna().unique()),
            )

            if picked:
                preview = preview[
                    preview["sumber_kecamatan"].isin(picked)
                ]

        st.caption(f"{len(preview):,} baris")

        st.dataframe(
            preview,
            width="stretch",
            hide_index=True,
        )


# ============================================================
# RIWAYAT DAPIL (PERMANEN)
# ============================================================

st.divider()

st.header(
    "Riwayat Dapil yang Sudah Diproses"
)

checkpoint_df = load_checkpoint_df(checkpoint_path)

if checkpoint_df.empty or "dapil" not in checkpoint_df.columns:

    st.info(
        "Belum ada dapil yang diproses. Hasil setiap dapil akan muncul "
        "di sini dan tetap tersimpan walau halaman ditutup."
    )

else:

    history = summarize_history(checkpoint_df, out_dir)

    st.dataframe(
        history,
        width="stretch",
        hide_index=True,
    )

    options = list(history["dapil"])

    focus = st.session_state.pop("gd_focus_dapil", None)

    if focus in options:
        st.session_state["gd_view_dapil"] = focus

    elif st.session_state.get("gd_view_dapil") not in options:
        st.session_state["gd_view_dapil"] = (
            dapil_selected if dapil_selected in options else options[0]
        )

    view_dapil = st.selectbox(
        "Buka hasil dapil",
        options,
        key="gd_view_dapil",
    )

    st.subheader(
        f"Hasil Dapil {view_dapil}"
    )

    render_dapil_report(
        view_dapil,
        checkpoint_df[checkpoint_df["dapil"] == view_dapil],
        out_dir,
    )