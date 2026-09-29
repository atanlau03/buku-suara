from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
from openpyxl.styles import Alignment, Font


# ============================================================
# PATH
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = BASE_DIR / "outputs"
DAPIL_DIR = OUTPUT_DIR / "dapil"
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoint"
STATE_DIR = OUTPUT_DIR / "state"
TMP_DIR = OUTPUT_DIR / "tmp"

for p in (
    DAPIL_DIR,
    CHECKPOINT_DIR,
    STATE_DIR,
    TMP_DIR,
):
    p.mkdir(parents=True, exist_ok=True)


CHECKPOINT_PATH = CHECKPOINT_DIR / "CHECKPOINT_INDONESIA.xlsx"
STATE_PATH = STATE_DIR / "processor_state.json"


# ============================================================
# KOLOM HASIL
# ============================================================

COLUMNS = [
    "nama_pdf",
    "nama_file",
    "kelurahan",
    "No partai",
    "nama_partai",
    "suara_akhir_partai",
    "suara_sah",
    "suara_tidak_sah",
    "total_suara",
    "provinsi",
    "dapil",
    "kab_kota",
    "kecamatan",
]


STATUS_COLUMNS = [
    "drive_file_id",
    "nama_pdf",
    "nama_file",
    "status",
    "provinsi",
    "dapil",
    "kab_kota",
    "kecamatan",
    "kelurahan",
    "waktu_mulai",
    "waktu_selesai",
    "jumlah_baris",
    "pesan",
    "drive_path",
]


# ============================================================
# LOCK / WORKER
# ============================================================

LOCK = threading.Lock()

# Download Google Drive dibuat satu per satu.
# OCR tetap berjalan paralel.
DRIVE_DOWNLOAD_LOCK = threading.Lock()

WORKER = None

# Maksimal PDF yang diproses bersamaan.
MAX_WORKERS = 4


# ============================================================
# UTILITAS
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(
    value: str,
    fallback="tanpa_nama",
) -> str:

    value = re.sub(
        r"[^A-Za-z0-9._ -]+",
        "_",
        str(value or ""),
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip(" ._")

    return value[:120] or fallback


def dapil_key(dapil: str) -> str:
    return safe_name(
        dapil,
        "DAPIL_TIDAK_DIKENAL",
    )


# ============================================================
# STATE
# ============================================================

def atomic_write_json(
    path: Path,
    data: dict,
):
    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    os.replace(
        tmp,
        path,
    )


def read_state():

    if not STATE_PATH.exists():

        return {
            "running": False,
            "job_id": None,
            "started_at": None,
            "finished_at": None,
            "message": "Siap.",
        }

    try:

        return json.loads(
            STATE_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return {
            "running": False,
            "job_id": None,
            "message": (
                "State rusak, aman untuk "
                "dijalankan ulang."
            ),
        }


def set_state(**updates):

    state = read_state()

    state.update(updates)

    atomic_write_json(
        STATE_PATH,
        state,
    )

    return state


# ============================================================
# GOOGLE DRIVE
# ============================================================

def extract_folder_id(
    url_or_id: str,
) -> str:

    s = str(
        url_or_id or ""
    ).strip()

    m = re.search(
        r"/folders/([A-Za-z0-9_-]+)",
        s,
    )

    if m:
        return m.group(1)

    m = re.search(
        r"[?&]id=([A-Za-z0-9_-]+)",
        s,
    )

    if m:
        return m.group(1)

    return s


def google_drive_service():

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path = (
        BASE_DIR / "credentials.json"
    )

    token_path = (
        BASE_DIR / "token.json"
    )

    scopes = [
        "https://www.googleapis.com/auth/drive.readonly"
    ]

    creds = None

    if token_path.exists():

        creds = Credentials.from_authorized_user_file(
            str(token_path),
            scopes,
        )

    if not creds or not creds.valid:

        if (
            creds
            and creds.expired
            and creds.refresh_token
        ):

            creds.refresh(
                Request()
            )

        else:

            if not credentials_path.exists():

                raise FileNotFoundError(
                    "credentials.json belum ada. "
                    "Buat OAuth Desktop Client di Google Cloud "
                    "lalu letakkan credentials.json "
                    "di folder aplikasi."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                scopes,
            )

            creds = flow.run_local_server(
                port=0
            )

        token_path.write_text(
            creds.to_json(),
            encoding="utf-8",
        )

    return build(
        "drive",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )


def list_children(
    service,
    parent_id: str,
):

    files = []

    token = None

    while True:

        response = service.files().list(
            q=(
                f"'{parent_id}' in parents "
                "and trashed = false"
            ),
            spaces="drive",
            fields=(
                "nextPageToken, "
                "files(id,name,mimeType,size,"
                "modifiedTime,parents,webViewLink)"
            ),
            pageSize=1000,
            pageToken=token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        files.extend(
            response.get(
                "files",
                [],
            )
        )

        token = response.get(
            "nextPageToken"
        )

        if not token:
            return files


def scan_pdfs(
    service,
    root_id: str,
) -> List[dict]:

    """
    Struktur Drive yang diharapkan:

        PILEG DPR
        └── DAPIL
            └── KAB/KOTA
                └── KECAMATAN
                    └── PDF

    Nama file PDF boleh random.
    Metadata folder dipakai jika tersedia.

    PENTING:
    Metadata folder bukan satu-satunya sumber.
    Jika tidak valid, nanti hasil OCR PDF akan menjadi
    fallback di process_one_pdf().
    """

    results = []

    def clean_part(value):

        return re.sub(
            r"\s+",
            " ",
            str(value or ""),
        ).strip()

    def walk(
        folder_id: str,
        path_parts: List[str],
    ):

        children = list_children(
            service,
            folder_id,
        )

        for item in children:

            name = clean_part(
                item.get(
                    "name",
                    "",
                )
            )

            mime = item.get(
                "mimeType",
                "",
            )

            # ------------------------------------------------
            # FOLDER
            # ------------------------------------------------

            if mime == (
                "application/vnd.google-apps.folder"
            ):

                walk(
                    item["id"],
                    path_parts + [name],
                )

                continue

            # ------------------------------------------------
            # HANYA PDF
            # ------------------------------------------------

            if not name.lower().endswith(".pdf"):
                continue

            folders = [
                clean_part(x)
                for x in path_parts
                if clean_part(x)
            ]

            dapil = "^"
            kab_kota = "^"
            kecamatan = "^"

            if len(folders) >= 3:

                dapil = folders[-3]
                kab_kota = folders[-2]
                kecamatan = folders[-1]

            elif len(folders) == 2:

                dapil = folders[-2]
                kab_kota = folders[-1]

            elif len(folders) == 1:

                kecamatan = folders[-1]

            results.append(
                {
                    "id": item["id"],
                    "name": name,
                    "mimeType": mime,
                    "size": item.get("size"),
                    "modifiedTime": item.get(
                        "modifiedTime"
                    ),

                    "path": " / ".join(
                        path_parts + [name]
                    ),

                    "provinsi": "^",
                    "dapil": dapil,
                    "kab_kota": kab_kota,
                    "kecamatan": kecamatan,

                    "folder_parts": folders,
                }
            )

    walk(
        root_id,
        [],
    )

    return results


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(
    service,
    file_id: str,
    dest: Path,
):

    """
    Download satu PDF dari Google Drive.

    Download dikunci agar beberapa worker tidak
    melakukan request download bersamaan.

    Setelah download selesai,
    OCR masing-masing PDF tetap berjalan paralel.
    """

    from googleapiclient.http import (
        MediaIoBaseDownload,
    )

    with DRIVE_DOWNLOAD_LOCK:

        request = service.files().get_media(
            fileId=file_id,
            acknowledgeAbuse=True,
        )

        with dest.open("wb") as fh:

            downloader = MediaIoBaseDownload(
                fh,
                request,
                chunksize=1024 * 1024,
            )

            done = False

            while not done:

                _, done = (
                    downloader.next_chunk()
                )


# ============================================================
# PDF QUALITY CHECK
# ============================================================

def quality_check(
    pdf_path: Path,
) -> tuple[bool, str, int]:

    if (
        not pdf_path.exists()
        or pdf_path.stat().st_size < 1000
    ):

        return (
            False,
            "PDF kosong/terlalu kecil.",
            0,
        )

    try:

        import fitz

        doc = fitz.open(
            pdf_path
        )

        count = len(doc)

        if count == 0:

            doc.close()

            return (
                False,
                "PDF tidak memiliki halaman.",
                0,
            )

        _ = doc.load_page(0).rect

        doc.close()

        return (
            True,
            "",
            count,
        )

    except Exception as exc:

        return (
            False,
            f"PDF rusak/tidak dapat dibuka: {exc}",
            0,
        )


# ============================================================
# OCR SUBPROCESS
# ============================================================

def _run_local_ocr_subprocess(
    pdf_path: Path,
    result_path: Path,
    dpi: int,
    timeout_seconds: int,
):

    cmd = [
        sys.executable,
        str(BASE_DIR / "pdf_worker.py"),
        str(pdf_path),
        str(result_path),
        str(dpi),
    ]

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        cwd=str(BASE_DIR),
    )


# ============================================================
# VALIDASI METADATA
# ============================================================

def _valid_metadata_value(
    value,
):
    """
    Menentukan apakah metadata dianggap valid.

    Nilai berikut dianggap gagal:
        kosong
        ^
        -
        _
        None
        nan
        tidak diketahui
        tidak dikenali
        unknown
    """

    if value is None:
        return False

    value = str(value).strip()

    if not value:
        return False

    if value in {
        "^",
        "-",
        "_",
        "None",
        "nan",
    }:
        return False

    if value.lower() in {
        "tidak diketahui",
        "tidak dikenali",
        "unknown",
    }:
        return False

    return True


def build_display_filename(item: dict) -> str:
    """
    Membuat isi kolom "Nama File" dari metadata folder Drive.

    Format:
        (KECAMATAN - DAPIL)

    Nama PDF asli disimpan terpisah pada kolom "Nama PDF".
    """

    kecamatan = str(
        item.get("kecamatan", "")
    ).strip()

    dapil = str(
        item.get("dapil", "")
    ).strip()

    valid_kecamatan = _valid_metadata_value(
        kecamatan
    )
    valid_dapil = _valid_metadata_value(
        dapil
    )

    if valid_kecamatan and valid_dapil:
        return f"({kecamatan} - {dapil})"

    if valid_kecamatan:
        return f"({kecamatan})"

    if valid_dapil:
        return f"({dapil})"

    return "^"


def metadata_for_row(
    item: dict,
    party_row: dict,
    field: str,
) -> str:
    """
    Metadata Drive/resolved menjadi sumber utama.

    Jika item sudah memiliki metadata valid, jangan ditimpa
    metadata OCR dari baris partai. OCR hanya menjadi fallback.
    """

    item_value = item.get(field)

    if _valid_metadata_value(item_value):
        return str(item_value).strip()

    party_value = party_row.get(field)

    if _valid_metadata_value(party_value):
        return str(party_value).strip()

    return "^"


def _first_valid_dataframe_value(
    dataframe: pd.DataFrame,
    column: str,
):
    """
    Mengambil nilai pertama yang valid
    dari sebuah kolom DataFrame.
    """

    if not isinstance(
        dataframe,
        pd.DataFrame,
    ):
        return None

    if dataframe.empty:
        return None

    if column not in dataframe.columns:
        return None

    for value in dataframe[column].tolist():

        if _valid_metadata_value(value):

            return str(
                value
            ).strip()

    return None


def resolve_metadata_from_ocr(
    result: dict,
    item: dict,
):
    """
    Menggabungkan metadata dari Google Drive
    dan hasil OCR.

    PRIORITAS:

    1. Metadata Drive yang valid tetap dipakai.
    2. Jika metadata Drive kosong/^,
       cari metadata dari hasil OCR.
    3. Jika keduanya tidak ada, tetap ^.

    Jadi file baru tidak bergantung pada nama file
    dan tidak hardcode berdasarkan PDF lama.
    """

    resolved = dict(item)

    candidates = []

    # --------------------------------------------------------
    # DataFrame hasil OCR yang mungkin memiliki metadata
    # --------------------------------------------------------

    for key in (
        "parties",
        "ranges",
        "summary",
        "validation",
        "tps",
        "raw",
    ):

        value = result.get(
            key
        )

        if isinstance(
            value,
            pd.DataFrame,
        ):

            candidates.append(
                value
            )

    # --------------------------------------------------------
    # METADATA WILAYAH
    # --------------------------------------------------------

    fields = [
        "provinsi",
        "dapil",
        "kab_kota",
        "kecamatan",
    ]

    for field in fields:

        current = resolved.get(
            field,
            "^",
        )

        # ----------------------------------------------------
        # Kalau metadata Drive sudah valid,
        # jangan ditimpa.
        # ----------------------------------------------------

        if _valid_metadata_value(
            current
        ):

            continue

        # ----------------------------------------------------
        # Kalau metadata Drive gagal,
        # cari dari hasil OCR.
        # ----------------------------------------------------

        found = None

        for dataframe in candidates:

            found = _first_valid_dataframe_value(
                dataframe,
                field,
            )

            if found:
                break

        if found:

            resolved[field] = found

        else:

            resolved[field] = "^"

    # --------------------------------------------------------
    # KELURAHAN
    # --------------------------------------------------------

    current_village = resolved.get(
        "kelurahan",
        "",
    )

    if not _valid_metadata_value(
        current_village
    ):

        for dataframe in candidates:

            found = _first_valid_dataframe_value(
                dataframe,
                "kelurahan",
            )

            if found:

                resolved[
                    "kelurahan"
                ] = found

                break

    return resolved


# ============================================================
# PROSES SATU PDF
# ============================================================

def process_one_pdf(
    service,
    item: dict,
    dpi: int,
    timeout_seconds: int,
):

    # Simpan nama PDF asli dan nama tampilan wilayah
    # sejak awal agar semua jalur hasil/error konsisten.
    item = dict(item)
    item["nama_pdf"] = item.get(
        "nama_pdf",
        item.get("name", ""),
    )
    item["nama_file"] = build_display_filename(
        item
    )

    job_tmp = (
        TMP_DIR / uuid.uuid4().hex
    )

    job_tmp.mkdir(
        parents=True,
        exist_ok=True,
    )

    pdf_path = (
        job_tmp
        / safe_name(
            item["name"],
            "dokumen.pdf",
        )
    )

    result_path = (
        job_tmp / "result.pkl"
    )

    started = now()

    try:

        # ====================================================
        # DOWNLOAD
        # ====================================================

        download_file(
            service,
            item["id"],
            pdf_path,
        )

        # ====================================================
        # CEK PDF
        # ====================================================

        ok, reason, pages = quality_check(
            pdf_path
        )

        if not ok:

            return {
                **item,
                "status": "PDF_RUSAK",
                "pesan": reason,
                "jumlah_baris": 0,
                "waktu_mulai": started,
                "waktu_selesai": now(),
            }

        # ====================================================
        # OCR
        # ====================================================

        try:

            completed = (
                _run_local_ocr_subprocess(
                    pdf_path,
                    result_path,
                    dpi,
                    timeout_seconds,
                )
            )

        except subprocess.TimeoutExpired:

            return {
                **item,
                "status": "SKIPPED",
                "pesan": (
                    "Melewati file karena melebihi "
                    f"batas {timeout_seconds} detik."
                ),
                "jumlah_baris": 0,
                "waktu_mulai": started,
                "waktu_selesai": now(),
            }

        # ====================================================
        # OCR GAGAL
        # ====================================================

        if (
            completed.returncode != 0
            or not result_path.exists()
        ):

            msg = (
                completed.stderr
                or completed.stdout
                or "OCR gagal tanpa pesan."
            ).strip()[-2000:]

            return {
                **item,
                "status": "GAGAL",
                "pesan": msg,
                "jumlah_baris": 0,
                "waktu_mulai": started,
                "waktu_selesai": now(),
            }

        # ====================================================
        # BACA HASIL OCR
        # ====================================================

        import pickle

        with result_path.open(
            "rb"
        ) as fh:

            result = pickle.load(
                fh
            )

        # ====================================================
        # RESOLUSI METADATA
        # ====================================================

        resolved_item = resolve_metadata_from_ocr(
            result,
            item,
        )

        # Nama PDF asli tetap terpisah.
        resolved_item["nama_pdf"] = item.get(
            "name",
            item.get("nama_pdf", ""),
        )

        # Nama File hanya berupa metadata wilayah.
        resolved_item["nama_file"] = build_display_filename(
            resolved_item
        )

        # ====================================================
        # BENTUK REPORT
        # ====================================================

        rows, note = make_report_rows(
            result,
            resolved_item,
        )

        if rows.empty:

            return {
                **resolved_item,
                "status": "DATA_TIDAK_TERBACA",
                "pesan": (
                    note
                    or
                    "OCR selesai tetapi tidak menghasilkan "
                    "baris laporan."
                ),
                "jumlah_baris": 0,
                "waktu_mulai": started,
                "waktu_selesai": now(),
            }

        # ====================================================
        # TULIS EXCEL DAPIL
        # ====================================================

        write_dapil_excel(
            resolved_item.get(
                "dapil",
                "^",
            ),
            rows,
            resolved_item,
        )

        # ====================================================
        # PESAN TAMBAHAN
        # ====================================================

        metadata_notes = []

        for field in (
            "provinsi",
            "dapil",
            "kab_kota",
            "kecamatan",
        ):

            drive_value = item.get(
                field,
                "^",
            )

            resolved_value = resolved_item.get(
                field,
                "^",
            )

            if (
                not _valid_metadata_value(
                    drive_value
                )
                and _valid_metadata_value(
                    resolved_value
                )
            ):

                metadata_notes.append(
                    f"{field} dari OCR: {resolved_value}"
                )

        final_note_parts = []

        if note:
            final_note_parts.append(
                note
            )

        if metadata_notes:
            final_note_parts.append(
                "Metadata fallback OCR digunakan: "
                + "; ".join(metadata_notes)
            )

        final_note = " | ".join(
            final_note_parts
        )

        # ====================================================
        # BERHASIL
        # ====================================================

        return {
            **resolved_item,
            "status": "BERHASIL",
            "pesan": (
                final_note
                or "Berhasil."
            ),
            "jumlah_baris": len(rows),
            "waktu_mulai": started,
            "waktu_selesai": now(),
        }

    except Exception as exc:

        return {
            **item,
            "status": "GAGAL",
            "pesan": (
                f"{type(exc).__name__}: {exc}"
            ),
            "jumlah_baris": 0,
            "waktu_mulai": started,
            "waktu_selesai": now(),
        }

    finally:

        shutil.rmtree(
            job_tmp,
            ignore_errors=True,
        )


# ============================================================
# VALIDASI ANGKA
# ============================================================

def _as_int_or_none(
    value,
):

    try:

        if pd.isna(value):
            return None

        return int(
            float(value)
        )

    except Exception:

        return None


# ============================================================
# MEMBUAT REPORT
# ============================================================

def make_report_rows(
    result: dict,
    item: dict,
):

    parties = result.get(
        "parties",
        pd.DataFrame(),
    )

    ranges = result.get(
        "ranges",
        pd.DataFrame(),
    )

    if (
        not isinstance(
            parties,
            pd.DataFrame,
        )
        or parties.empty
    ):

        return (
            pd.DataFrame(
                columns=COLUMNS
            ),
            "Tidak ditemukan data partai.",
        )

    if not isinstance(
        ranges,
        pd.DataFrame,
    ):

        ranges = pd.DataFrame()

    village_map = {}

    if not ranges.empty:

        for _, r in ranges.iterrows():

            village = str(
                r.get(
                    "kelurahan",
                    "",
                )
            ).strip()

            if not village:
                continue

            village_map[
                village.upper()
            ] = {
                "suara_sah": _as_int_or_none(
                    r.get(
                        "suara_sah"
                    )
                ),

                "suara_tidak_sah": _as_int_or_none(
                    r.get(
                        "suara_tidak_sah"
                    )
                ),

                "total_suara": _as_int_or_none(
                    r.get(
                        "total_suara"
                    )
                ),
            }

    out = []

    uncertain = 0

    for _, p in parties.iterrows():

        village = str(
            p.get(
                "kelurahan"
            )
            or ""
        ).strip()

        if not village:
            village = "^"

        party_no = _as_int_or_none(
            p.get(
                "partai"
            )
        )

        party_name = str(
            p.get(
                "nama_partai"
            )
            or ""
        ).strip()

        if not party_name:
            party_name = "^"

        party_vote = _as_int_or_none(
            p.get(
                "suara_akhir_partai"
            )
        )

        totals = village_map.get(
            village.upper(),
            {},
        )

        sah = totals.get(
            "suara_sah"
        )

        tidak = totals.get(
            "suara_tidak_sah"
        )

        total = totals.get(
            "total_suara"
        )

        # ====================================================
        # SUARA SAH / TIDAK SAH
        # ====================================================

        if (
            sah is None
            or tidak is None
        ):

            sah_out = "^"
            tidak_out = "^"

            uncertain += 1

        else:

            sah_out = sah
            tidak_out = tidak

        # ====================================================
        # TOTAL SUARA
        # ====================================================

        if (
            total is None
            and sah is not None
            and tidak is not None
        ):

            total = (
                sah + tidak
            )

        if (
            total is None
            or (
                sah is not None
                and tidak is not None
                and total != sah + tidak
            )
        ):

            total_out = "^"

            uncertain += 1

        else:

            total_out = total

        # ====================================================
        # BARIS OUTPUT
        # ====================================================

        row = {
            "nama_pdf": str(
                item.get(
                    "nama_pdf",
                    item.get("name", ""),
                )
            ).strip(),

            "nama_file": build_display_filename(
                item
            ),

            "kelurahan": village,

            "No partai": (
                party_no
                if party_no is not None
                else "^"
            ),

            "nama_partai": party_name,

            "suara_akhir_partai": (
                party_vote
                if party_vote is not None
                else "^"
            ),

            "suara_sah": sah_out,

            "suara_tidak_sah": tidak_out,

            "total_suara": total_out,

            "provinsi": metadata_for_row(
                item,
                p,
                "provinsi",
            ),

            "dapil": metadata_for_row(
                item,
                p,
                "dapil",
            ),

            "kab_kota": metadata_for_row(
                item,
                p,
                "kab_kota",
            ),

            "kecamatan": metadata_for_row(
                item,
                p,
                "kecamatan",
            ),
        }

        out.append(
            row
        )

    note = (
        ""
        if uncertain == 0
        else (
            f"{uncertain} nilai ditandai ^ "
            "karena validasi/rekonsiliasi "
            "belum meyakinkan."
        )
    )

    return (
        pd.DataFrame(
            out,
            columns=COLUMNS,
        ),
        note,
    )


# ============================================================
# EXCEL
# ============================================================

def atomic_excel_write(
    path: Path,
    dataframe: pd.DataFrame,
):

    tmp = path.with_name(
        path.stem
        + f".__writing__{uuid.uuid4().hex}.xlsx"
    )

    with pd.ExcelWriter(
        tmp,
        engine="openpyxl",
    ) as writer:

        dataframe.to_excel(
            writer,
            index=False,
            sheet_name="Data",
        )

        ws = writer.book[
            "Data"
        ]

        ws.freeze_panes = "A2"

        ws.auto_filter.ref = (
            ws.dimensions
        )

        for cell in ws[1]:

            cell.font = Font(
                bold=True
            )

            cell.alignment = Alignment(
                horizontal="center",
                vertical="center",
            )

        widths = {
            c: max(
                14,
                min(
                    36,
                    len(c) + 4,
                ),
            )
            for c in COLUMNS
        }

        for i, c in enumerate(
            COLUMNS,
            1,
        ):

            from openpyxl.utils import (
                get_column_letter,
            )

            ws.column_dimensions[
                get_column_letter(i)
            ].width = widths[c]

    os.replace(
        tmp,
        path,
    )


def write_dapil_excel(
    dapil: str,
    rows: pd.DataFrame,
    item: dict,
):

    path = (
        DAPIL_DIR
        / f"{dapil_key(dapil)}.xlsx"
    )

    with LOCK:

        if path.exists():

            old = pd.read_excel(
                path,
                sheet_name="Data",
            )

        else:

            old = pd.DataFrame(
                columns=COLUMNS
            )

        if not old.empty:

            # Excel Dapil lama mungkin belum memiliki kolom
            # Nama PDF / Nama File. Tambahkan tanpa merusak
            # data lama agar file lama tetap bisa dilanjutkan.
            for c in COLUMNS:
                if c not in old.columns:
                    old[c] = ""

            separator = pd.DataFrame(
                [
                    {
                        c: ""
                        for c in COLUMNS
                    }
                ]
            )

            combined = pd.concat(
                [
                    old[COLUMNS],
                    separator,
                    rows[COLUMNS],
                ],
                ignore_index=True,
            )

        else:

            combined = rows[
                COLUMNS
            ].copy()

        atomic_excel_write(
            path,
            combined,
        )


# ============================================================
# CHECKPOINT
# ============================================================

def write_checkpoint(
    records: List[dict],
):

    df = pd.DataFrame(
        records
    )

    for c in STATUS_COLUMNS:

        if c not in df.columns:

            df[c] = ""

    df = df[
        STATUS_COLUMNS
    ]

    tmp = (
        CHECKPOINT_DIR
        / (
            "CHECKPOINT_INDONESIA.__writing__"
            f".{os.getpid()}."
            f"{threading.get_ident()}."
            f"{uuid.uuid4().hex}.xlsx"
        )
    )

    with pd.ExcelWriter(
        tmp,
        engine="openpyxl",
    ) as writer:

        df.to_excel(
            writer,
            index=False,
            sheet_name="Checkpoint",
        )

        ws = writer.book[
            "Checkpoint"
        ]

        ws.freeze_panes = "A2"

        ws.auto_filter.ref = (
            ws.dimensions
        )

        for cell in ws[1]:

            cell.font = Font(
                bold=True
            )

    last_error = None

    for attempt in range(8):
        try:
            os.replace(
                tmp,
                CHECKPOINT_PATH,
            )
            last_error = None
            break

        except PermissionError as exc:
            last_error = exc
            time.sleep(
                0.25 * (attempt + 1)
            )

    if last_error is not None:
        # Jangan hapus temp jika target masih terkunci.
        # File ini dapat dipakai untuk recovery/debug.
        raise last_error


def read_checkpoint_records():

    if not CHECKPOINT_PATH.exists():
        return []

    try:

        df = pd.read_excel(
            CHECKPOINT_PATH,
            sheet_name="Checkpoint",
            dtype=str,
        ).fillna("")

        return df.to_dict(
            "records"
        )

    except Exception:

        return []


# ============================================================
# START JOB
# ============================================================

def start_job(
    folder: str,
    dpi: int = 300,
    timeout_seconds: int = 900,
):

    global WORKER

    with LOCK:

        state = read_state()

        if state.get(
            "running"
        ):

            return (
                False,
                "Proses masih berjalan.",
            )

        job_id = uuid.uuid4().hex

        set_state(
            running=True,
            job_id=job_id,
            started_at=now(),
            finished_at=None,
            message=(
                "Menghubungkan ke Google Drive..."
            ),
            total=0,
            done=0,
        )

        WORKER = threading.Thread(
            target=_worker,
            args=(
                job_id,
                folder,
                dpi,
                timeout_seconds,
            ),
            daemon=True,
            name="KPU-Drive-Worker",
        )

        WORKER.start()

        return (
            True,
            job_id,
        )


# ============================================================
# WORKER BATCH / PARALEL
# ============================================================

def _worker(
    job_id: str,
    folder: str,
    dpi: int,
    timeout_seconds: int,
):

    records = read_checkpoint_records()

    by_id = {
        str(
            r.get(
                "drive_file_id",
                "",
            )
        ): r
        for r in records
        if r.get(
            "drive_file_id"
        )
    }

    try:

        # ====================================================
        # GOOGLE DRIVE
        # ====================================================

        service = google_drive_service()

        root_id = extract_folder_id(
            folder
        )

        if not root_id:

            raise ValueError(
                "Link/ID folder Google Drive "
                "belum diisi."
            )

        set_state(
            message=(
                "Mencari PDF di Google Drive..."
            )
        )

        # ====================================================
        # SCAN PDF
        # ====================================================

        items = scan_pdfs(
            service,
            root_id,
        )

        # ====================================================
        # SEGARKAN METADATA FILE YANG SUDAH ADA
        # ====================================================

        for item in items:

            fid = item[
                "id"
            ]

            if fid in by_id:

                current = dict(
                    by_id[fid]
                )

                current["nama_pdf"] = item.get(
                    "name",
                    current.get(
                        "nama_pdf",
                        "",
                    ),
                )

                current["drive_path"] = item.get(
                    "path",
                    current.get(
                        "drive_path",
                        "",
                    ),
                )

                display_source = dict(
                    current
                )

                for field in (
                    "provinsi",
                    "dapil",
                    "kab_kota",
                    "kecamatan",
                ):

                    drive_value = item.get(
                        field,
                        "^",
                    )

                    if _valid_metadata_value(
                        drive_value
                    ):
                        current[field] = drive_value
                        display_source[field] = drive_value
                    else:
                        display_source[field] = current.get(
                            field,
                            "^",
                        )

                display_source["name"] = item.get(
                    "name",
                    current.get(
                        "nama_pdf",
                        "",
                    ),
                )

                current["nama_file"] = build_display_filename(
                    display_source
                )

                by_id[fid] = current

        # ====================================================
        # TAMBAHKAN FILE BARU KE CHECKPOINT
        # ====================================================

        for item in items:

            fid = item[
                "id"
            ]

            if fid not in by_id:

                by_id[fid] = {
                    "drive_file_id": fid,
                    "nama_pdf": item.get(
                        "name",
                        "",
                    ),
                    "nama_file": build_display_filename(
                        item
                    ),
                    "status": "MENUNGGU",
                    "provinsi": item.get(
                        "provinsi",
                        "^",
                    ),
                    "dapil": item.get(
                        "dapil",
                        "^",
                    ),
                    "kab_kota": item.get(
                        "kab_kota",
                        "^",
                    ),
                    "kecamatan": item.get(
                        "kecamatan",
                        "^",
                    ),
                    "kelurahan": "",
                    "waktu_mulai": "",
                    "waktu_selesai": "",
                    "jumlah_baris": 0,
                    "pesan": "",
                    "drive_path": item.get(
                        "path",
                        "",
                    ),
                }

        write_checkpoint(
            list(
                by_id.values()
            )
        )

        # ====================================================
        # STATUS YANG DIANGGAP SUDAH SELESAI
        # ====================================================

        selesai_status = {
            "BERHASIL",
            "PDF_RUSAK",
            "DATA_TIDAK_TERBACA",
        }

        pending = [
            x
            for x in items
            if by_id[
                x["id"]
            ].get(
                "status"
            ) not in selesai_status
        ]

        total_pending = len(
            pending
        )

        set_state(
            total=total_pending,
            done=0,
            message=(
                f"Ditemukan {len(items)} PDF. "
                f"{total_pending} PDF perlu diproses. "
                f"Maksimal {MAX_WORKERS} PDF "
                "berjalan bersamaan."
            ),
        )

        # ====================================================
        # TIDAK ADA PENDING
        # ====================================================

        if not pending:

            set_state(
                running=False,
                total=0,
                done=0,
                finished_at=now(),
                message=(
                    "Semua PDF sudah memiliki "
                    "status selesai."
                ),
            )

            return

        # ====================================================
        # FUNGSI UNTUK SATU ITEM
        # ====================================================

        def process_item(
            item: dict,
        ):

            fid = item[
                "id"
            ]

            # ------------------------------------------------
            # CEK STOP SEBELUM MULAI
            # ------------------------------------------------

            state = read_state()

            if not state.get(
                "running"
            ):

                return {
                    "item": item,
                    "record": {
                        "drive_file_id": fid,
                        "nama_pdf": item.get(
                            "name",
                            "",
                        ),
                        "nama_file": build_display_filename(
                            item
                        ),
                        "status": "DIHENTIKAN",
                        "provinsi": item.get(
                            "provinsi",
                            "^",
                        ),
                        "dapil": item.get(
                            "dapil",
                            "^",
                        ),
                        "kab_kota": item.get(
                            "kab_kota",
                            "^",
                        ),
                        "kecamatan": item.get(
                            "kecamatan",
                            "^",
                        ),
                        "kelurahan": "",
                        "waktu_mulai": "",
                        "waktu_selesai": now(),
                        "jumlah_baris": 0,
                        "pesan": (
                            "Antrean dihentikan "
                            "pengguna."
                        ),
                        "drive_path": item.get(
                            "path",
                            "",
                        ),
                    },
                }

            # ------------------------------------------------
            # STATUS DIPROSES
            # ------------------------------------------------

            start_time = now()

            with LOCK:

                current = (
                    by_id
                    .get(
                        fid,
                        {},
                    )
                    .copy()
                )

                current.update(
                    {
                        "drive_file_id": fid,
                        "nama_pdf": item.get(
                            "name",
                            "",
                        ),
                        "nama_file": build_display_filename(
                            item
                        ),
                        "status": "DIPROSES",
                        "waktu_mulai": start_time,
                        "waktu_selesai": "",
                        "pesan": (
                            "Sedang diunduh "
                            "dan diproses OCR lokal..."
                        ),
                        "provinsi": item.get(
                            "provinsi",
                            current.get(
                                "provinsi",
                                "^",
                            ),
                        ),
                        "dapil": item.get(
                            "dapil",
                            current.get(
                                "dapil",
                                "^",
                            ),
                        ),
                        "kab_kota": item.get(
                            "kab_kota",
                            current.get(
                                "kab_kota",
                                "^",
                            ),
                        ),
                        "kecamatan": item.get(
                            "kecamatan",
                            current.get(
                                "kecamatan",
                                "^",
                            ),
                        ),
                        "drive_path": item.get(
                            "path",
                            current.get(
                                "drive_path",
                                "",
                            ),
                        ),
                    }
                )

                by_id[
                    fid
                ] = current

            # ------------------------------------------------
            # PROSES PDF
            # ------------------------------------------------

            try:

                rec = process_one_pdf(
                    service,
                    item,
                    dpi,
                    timeout_seconds,
                )

                return {
                    "item": item,
                    "record": rec,
                }

            except Exception as exc:

                return {
                    "item": item,
                    "record": {
                        "drive_file_id": fid,
                        "nama_pdf": item.get(
                            "name",
                            "",
                        ),
                        "nama_file": build_display_filename(
                            item
                        ),
                        "status": "ERROR",
                        "provinsi": item.get(
                            "provinsi",
                            "^",
                        ),
                        "dapil": item.get(
                            "dapil",
                            "^",
                        ),
                        "kab_kota": item.get(
                            "kab_kota",
                            "^",
                        ),
                        "kecamatan": item.get(
                            "kecamatan",
                            "^",
                        ),
                        "kelurahan": "",
                        "waktu_mulai": start_time,
                        "waktu_selesai": now(),
                        "jumlah_baris": 0,
                        "pesan": (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        "drive_path": item.get(
                            "path",
                            "",
                        ),
                    },
                }

        # ====================================================
        # JALANKAN MAKSIMAL 4 PDF BERSAMAAN
        # ====================================================

        done = 0

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="KPU-PDF",
        ) as executor:

            futures = {
                executor.submit(
                    process_item,
                    item,
                ): item
                for item in pending
            }

            # =================================================
            # AMBIL HASIL BERDASARKAN FILE YANG SELESAI
            # =================================================

            for future in as_completed(
                futures
            ):

                item = futures[
                    future
                ]

                try:

                    result = future.result()

                    rec = result[
                        "record"
                    ]

                except Exception as exc:

                    rec = {
                        "drive_file_id": item[
                            "id"
                        ],
                        "nama_pdf": item.get(
                            "name",
                            "",
                        ),
                        "nama_file": build_display_filename(
                            item
                        ),
                        "status": "ERROR",
                        "provinsi": item.get(
                            "provinsi",
                            "^",
                        ),
                        "dapil": item.get(
                            "dapil",
                            "^",
                        ),
                        "kab_kota": item.get(
                            "kab_kota",
                            "^",
                        ),
                        "kecamatan": item.get(
                            "kecamatan",
                            "^",
                        ),
                        "kelurahan": "",
                        "waktu_mulai": "",
                        "waktu_selesai": now(),
                        "jumlah_baris": 0,
                        "pesan": (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        "drive_path": item.get(
                            "path",
                            "",
                        ),
                    }

                # =================================================
                # UPDATE CHECKPOINT
                # =================================================

                fid = item[
                    "id"
                ]

                with LOCK:

                    old = by_id.get(
                        fid,
                        {},
                    )

                    by_id[
                        fid
                    ] = {
                        k: rec.get(
                            k,
                            old.get(
                                k,
                                "",
                            ),
                        )
                        for k in STATUS_COLUMNS
                    }

                write_checkpoint(
                    list(
                        by_id.values()
                    )
                )

                done += 1

                # =================================================
                # STATUS REAL-TIME
                # =================================================

                set_state(
                    done=done,
                    total=total_pending,
                    message=(
                        f"{done}/{total_pending} selesai | "
                        f"{item['name']} | "
                        f"Status: "
                        f"{rec.get('status', 'UNKNOWN')} | "
                        f"{MAX_WORKERS} worker"
                    ),
                )

        # ====================================================
        # SELESAI
        # ====================================================

        set_state(
            running=False,
            done=done,
            total=total_pending,
            finished_at=now(),
            message=(
                f"Selesai. {done}/{total_pending} PDF "
                "telah diproses."
            ),
        )

    except Exception as exc:

        set_state(
            running=False,
            finished_at=now(),
            message=(
                "Gagal memulai proses: "
                f"{type(exc).__name__}: {exc}"
            ),
        )


# ============================================================
# STOP JOB
# ============================================================

def stop_job():

    """
    Worker yang sedang mengunduh atau menjalankan OCR
    tidak dipaksa dibunuh.

    Flag running=False membuat pekerjaan yang belum mulai
    tidak melanjutkan proses berikutnya.

    Subprocess OCR tetap memiliki timeout.
    """

    set_state(
        running=False,
        message=(
            "Penghentian diminta. "
            "Antrean berikutnya tidak akan diproses."
        ),
    )


# ============================================================
# OUTPUT
# ============================================================

def latest_outputs():

    dapil_files = sorted(
        DAPIL_DIR.glob(
            "*.xlsx"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    return (
        dapil_files,
        (
            CHECKPOINT_PATH
            if CHECKPOINT_PATH.exists()
            else None
        ),
    )