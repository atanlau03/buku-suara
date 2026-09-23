from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
DAPIL_DIR = OUTPUT_DIR / "dapil"
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoint"
STATE_DIR = OUTPUT_DIR / "state"
TMP_DIR = OUTPUT_DIR / "tmp"
for p in (DAPIL_DIR, CHECKPOINT_DIR, STATE_DIR, TMP_DIR):
    p.mkdir(parents=True, exist_ok=True)

CHECKPOINT_PATH = CHECKPOINT_DIR / "CHECKPOINT_INDONESIA.xlsx"
STATE_PATH = STATE_DIR / "processor_state.json"

COLUMNS = [
    "kelurahan", "No partai", "nama_partai", "suara_akhir_partai",
    "suara_sah", "suara_tidak_sah", "total_suara", "provinsi", "dapil",
    "kab_kota", "kecamatan",
]
STATUS_COLUMNS = [
    "drive_file_id", "nama_file", "status", "provinsi", "dapil", "kab_kota",
    "kecamatan", "kelurahan", "waktu_mulai", "waktu_selesai", "jumlah_baris",
    "pesan", "drive_path",
]

LOCK = threading.Lock()
WORKER = None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(value: str, fallback="tanpa_nama") -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:120] or fallback


def dapil_key(dapil: str) -> str:
    return safe_name(dapil, "DAPIL_TIDAK_DIKENAL")


def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_state():
    if not STATE_PATH.exists():
        return {"running": False, "job_id": None, "started_at": None, "finished_at": None, "message": "Siap."}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "job_id": None, "message": "State rusak, aman untuk dijalankan ulang."}


def set_state(**updates):
    state = read_state()
    state.update(updates)
    atomic_write_json(STATE_PATH, state)
    return state


def extract_folder_id(url_or_id: str) -> str:
    s = str(url_or_id or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s


def google_drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path = BASE_DIR / "credentials.json"
    token_path = BASE_DIR / "token.json"
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    "credentials.json belum ada. Buat OAuth Desktop Client di Google Cloud "
                    "lalu letakkan credentials.json di folder aplikasi."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_children(service, parent_id: str):
    files = []
    token = None
    while True:
        response = service.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,parents,webViewLink)",
            pageSize=1000,
            pageToken=token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            return files


def scan_pdfs(service, root_id: str) -> List[dict]:
    """
    Struktur Drive:
        PILEG DPR
        └── DAPIL
            └── KAB/KOTA
                └── KECAMATAN
                    └── PDF

    Nama file PDF boleh random.
    Wilayah ditentukan dari nama folder.
    """

    results = []

    def clean_part(value):
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def walk(folder_id: str, path_parts: List[str]):
        children = list_children(service, folder_id)

        for item in children:
            name = clean_part(item.get("name", ""))
            mime = item.get("mimeType", "")

            # Jika folder, masuk lebih dalam
            if mime == "application/vnd.google-apps.folder":
                walk(
                    item["id"],
                    path_parts + [name]
                )
                continue

            # Hanya proses PDF
            if not name.lower().endswith(".pdf"):
                continue

            folders = [
                clean_part(x)
                for x in path_parts
                if clean_part(x)
            ]

            # -------------------------------------------------
            # Struktur normal:
            #
            # folders[-3] = DAPIL
            # folders[-2] = KAB/KOTA
            # folders[-1] = KECAMATAN
            #
            # Contoh:
            # ["JAWA BARAT VIII", "KARAWANG", "TELAGASARI"]
            # -------------------------------------------------

            dapil = "^"
            kab_kota = "^"
            kecamatan = "^"

            if len(folders) >= 3:
                dapil = folders[-3]
                kab_kota = folders[-2]
                kecamatan = folders[-1]

            elif len(folders) == 2:
                # Misalnya user memasukkan folder KAB/KOTA
                dapil = folders[-2]
                kab_kota = folders[-1]

            elif len(folders) == 1:
                # Misalnya user langsung memasukkan folder Kecamatan
                kecamatan = folders[-1]

            results.append({
                "id": item["id"],
                "name": name,
                "mimeType": mime,
                "size": item.get("size"),
                "modifiedTime": item.get("modifiedTime"),

                # Path asli untuk audit/checkpoint
                "path": " / ".join(path_parts + [name]),

                # Metadata wilayah
                "provinsi": "^",
                "dapil": dapil,
                "kab_kota": kab_kota,
                "kecamatan": kecamatan,

                # Disimpan supaya resolver berikutnya
                # bisa menggunakan struktur folder asli.
                "folder_parts": folders,
            })

    walk(root_id, [])

    return results

def download_file(service, file_id: str, dest: Path):
    from googleapiclient.http import MediaIoBaseDownload
    request = service.files().get_media(fileId=file_id, acknowledgeAbuse=True)
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def quality_check(pdf_path: Path) -> tuple[bool, str, int]:
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        return False, "PDF kosong/terlalu kecil.", 0
    try:
        import fitz
        doc = fitz.open(pdf_path)
        count = len(doc)
        if count == 0:
            doc.close()
            return False, "PDF tidak memiliki halaman.", 0
        # Akses halaman pertama untuk mendeteksi file yang benar-benar rusak.
        _ = doc.load_page(0).rect
        doc.close()
        return True, "", count
    except Exception as exc:
        return False, f"PDF rusak/tidak dapat dibuka: {exc}", 0


def _run_local_ocr_subprocess(pdf_path: Path, result_path: Path, dpi: int, timeout_seconds: int):
    cmd = [sys.executable, str(BASE_DIR / "pdf_worker.py"), str(pdf_path), str(result_path), str(dpi)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, cwd=str(BASE_DIR))


def process_one_pdf(service, item: dict, dpi: int, timeout_seconds: int):
    job_tmp = TMP_DIR / uuid.uuid4().hex
    job_tmp.mkdir(parents=True, exist_ok=True)
    pdf_path = job_tmp / safe_name(item["name"], "dokumen.pdf")
    result_path = job_tmp / "result.pkl"
    started = now()
    try:
        download_file(service, item["id"], pdf_path)
        ok, reason, pages = quality_check(pdf_path)
        if not ok:
            return {**item, "status": "PDF_RUSAK", "pesan": reason, "jumlah_baris": 0, "waktu_mulai": started, "waktu_selesai": now()}
        try:
            completed = _run_local_ocr_subprocess(pdf_path, result_path, dpi, timeout_seconds)
        except subprocess.TimeoutExpired:
            return {**item, "status": "SKIPPED", "pesan": f"Melewati file karena melebihi batas {timeout_seconds} detik.", "jumlah_baris": 0, "waktu_mulai": started, "waktu_selesai": now()}
        if completed.returncode != 0 or not result_path.exists():
            msg = (completed.stderr or completed.stdout or "OCR gagal tanpa pesan.").strip()[-2000:]
            return {**item, "status": "GAGAL", "pesan": msg, "jumlah_baris": 0, "waktu_mulai": started, "waktu_selesai": now()}
        import pickle
        with result_path.open("rb") as fh:
            result = pickle.load(fh)
        rows, note = make_report_rows(result, item)
        if rows.empty:
            return {**item, "status": "DATA_TIDAK_TERBACA", "pesan": note or "OCR selesai tetapi tidak menghasilkan baris laporan.", "jumlah_baris": 0, "waktu_mulai": started, "waktu_selesai": now()}
        write_dapil_excel(item.get("dapil", "^"), rows, item)
        return {**item, "status": "BERHASIL", "pesan": note or "Berhasil.", "jumlah_baris": len(rows), "waktu_mulai": started, "waktu_selesai": now()}
    except Exception as exc:
        return {**item, "status": "GAGAL", "pesan": f"{type(exc).__name__}: {exc}", "jumlah_baris": 0, "waktu_mulai": started, "waktu_selesai": now()}
    finally:
        shutil.rmtree(job_tmp, ignore_errors=True)


def _as_int_or_none(value):
    try:
        if pd.isna(value):
            return None
        return int(float(value))
    except Exception:
        return None


def make_report_rows(result: dict, item: dict):
    parties = result.get("parties", pd.DataFrame())
    ranges = result.get("ranges", pd.DataFrame())
    if not isinstance(parties, pd.DataFrame) or parties.empty:
        return pd.DataFrame(columns=COLUMNS), "Tidak ditemukan data partai."
    if not isinstance(ranges, pd.DataFrame):
        ranges = pd.DataFrame()

    village_map = {}
    if not ranges.empty:
        for _, r in ranges.iterrows():
            village = str(r.get("kelurahan", "")).strip()
            if not village:
                continue
            village_map[village.upper()] = {
                "suara_sah": _as_int_or_none(r.get("suara_sah")),
                "suara_tidak_sah": _as_int_or_none(r.get("suara_tidak_sah")),
                "total_suara": _as_int_or_none(r.get("total_suara")),
            }

    out = []
    uncertain = 0
    for _, p in parties.iterrows():
        village = str(p.get("kelurahan") or "").strip() or "^"
        party_no = _as_int_or_none(p.get("partai"))
        party_name = str(p.get("nama_partai") or "").strip() or "^"
        party_vote = _as_int_or_none(p.get("suara_akhir_partai"))
        totals = village_map.get(village.upper(), {})
        sah = totals.get("suara_sah")
        tidak = totals.get("suara_tidak_sah")
        total = totals.get("total_suara")
        if sah is None or tidak is None:
            sah_out, tidak_out = "^", "^"
            uncertain += 1
        else:
            sah_out, tidak_out = sah, tidak
        if total is None and sah is not None and tidak is not None:
            total = sah + tidak
        if total is None or (sah is not None and tidak is not None and total != sah + tidak):
            total_out = "^"
            uncertain += 1
        else:
            total_out = total
        row = {
            "kelurahan": village,
            "No partai": party_no if party_no is not None else "^",
            "nama_partai": party_name,
            "suara_akhir_partai": party_vote if party_vote is not None else "^",
            "suara_sah": sah_out,
            "suara_tidak_sah": tidak_out,
            "total_suara": total_out,
            "provinsi": str(p.get("provinsi") or item.get("provinsi") or "^").strip() or "^",
            "dapil": str(p.get("dapil") or item.get("dapil") or "^").strip() or "^",
            "kab_kota": str(p.get("kab_kota") or item.get("kab_kota") or "^").strip() or "^",
            "kecamatan": str(p.get("kecamatan") or item.get("kecamatan") or "^").strip() or "^",
        }
        out.append(row)
    note = "" if uncertain == 0 else f"{uncertain} nilai ditandai ^ karena validasi/rekonsiliasi belum meyakinkan."
    return pd.DataFrame(out, columns=COLUMNS), note


def atomic_excel_write(path: Path, dataframe: pd.DataFrame):
    tmp = path.with_name(path.stem + f".__writing__{uuid.uuid4().hex}.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="Data")
        ws = writer.book["Data"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        widths = {c: max(14, min(36, len(c) + 4)) for c in COLUMNS}
        for i, c in enumerate(COLUMNS, 1):
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = widths[c]
    os.replace(tmp, path)


def write_dapil_excel(dapil: str, rows: pd.DataFrame, item: dict):
    path = DAPIL_DIR / f"{dapil_key(dapil)}.xlsx"
    with LOCK:
        if path.exists():
            old = pd.read_excel(path, sheet_name="Data")
        else:
            old = pd.DataFrame(columns=COLUMNS)
        if not old.empty:
            separator = pd.DataFrame([{c: "" for c in COLUMNS}])
            combined = pd.concat([old[COLUMNS], separator, rows[COLUMNS]], ignore_index=True)
        else:
            combined = rows[COLUMNS].copy()
        atomic_excel_write(path, combined)


def write_checkpoint(records: List[dict]):
    df = pd.DataFrame(records)
    for c in STATUS_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = df[STATUS_COLUMNS]
    tmp = CHECKPOINT_PATH.with_name("CHECKPOINT_INDONESIA.__writing__.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Checkpoint")
        ws = writer.book["Checkpoint"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True)
    os.replace(tmp, CHECKPOINT_PATH)


def read_checkpoint_records():
    if not CHECKPOINT_PATH.exists():
        return []
    try:
        df = pd.read_excel(CHECKPOINT_PATH, sheet_name="Checkpoint", dtype=str).fillna("")
        return df.to_dict("records")
    except Exception:
        return []


def start_job(folder: str, dpi: int = 300, timeout_seconds: int = 900):
    global WORKER
    with LOCK:
        state = read_state()
        if state.get("running"):
            return False, "Proses masih berjalan."
        job_id = uuid.uuid4().hex
        set_state(running=True, job_id=job_id, started_at=now(), finished_at=None, message="Menghubungkan ke Google Drive...", total=0, done=0)
        WORKER = threading.Thread(target=_worker, args=(job_id, folder, dpi, timeout_seconds), daemon=True, name="KPU-Drive-Worker")
        WORKER.start()
        return True, job_id


def _worker(job_id: str, folder: str, dpi: int, timeout_seconds: int):
    records = read_checkpoint_records()
    by_id = {str(r.get("drive_file_id", "")): r for r in records if r.get("drive_file_id")}
    try:
        service = google_drive_service()
        root_id = extract_folder_id(folder)
        if not root_id:
            raise ValueError("Link/ID folder Google Drive belum diisi.")
        set_state(message="Mencari PDF di Google Drive...")
        items = scan_pdfs(service, root_id)
        # Tambahkan file baru ke checkpoint lebih dulu.
        for item in items:
            fid = item["id"]
            if fid not in by_id:
                by_id[fid] = {"drive_file_id": fid, "nama_file": item["name"], "status": "MENUNGGU", "provinsi": item.get("provinsi", "^"), "dapil": item.get("dapil", "^"), "kab_kota": item.get("kab_kota", "^"), "kecamatan": item.get("kecamatan", "^"), "kelurahan": "", "waktu_mulai": "", "waktu_selesai": "", "jumlah_baris": 0, "pesan": "", "drive_path": item.get("path", "")}
        write_checkpoint(list(by_id.values()))
        pending = [x for x in items if by_id[x["id"]].get("status") not in {"BERHASIL", "PDF_RUSAK", "DATA_TIDAK_TERBACA"}]
        set_state(total=len(pending), done=0, message=f"Ditemukan {len(items)} PDF; {len(pending)} perlu diproses.")
        done = 0
        for item in pending:
            if not read_state().get("running"):
                break
            current = by_id[item["id"]]
            current["status"] = "DIPROSES"
            current["waktu_mulai"] = now()
            current["pesan"] = "Sedang diunduh dan diproses OCR lokal..."
            write_checkpoint(list(by_id.values()))
            rec = process_one_pdf(service, item, dpi, timeout_seconds)
            by_id[item["id"]] = {k: rec.get(k, by_id[item["id"]].get(k, "")) for k in STATUS_COLUMNS}
            write_checkpoint(list(by_id.values()))
            done += 1
            set_state(done=done, total=len(pending), message=f"{done}/{len(pending)} — {item['name']} — {rec['status']}")
        set_state(running=False, finished_at=now(), message="Selesai memproses semua file yang memenuhi antrean.")
    except Exception as exc:
        set_state(running=False, finished_at=now(), message=f"Gagal memulai proses: {type(exc).__name__}: {exc}")


def stop_job():
    # Worker yang sedang mengunduh/menjalankan OCR tidak dipaksa dibunuh;
    # flag menghentikan antrean pada file berikutnya. Subprocess tetap dibatasi timeout.
    set_state(running=False, message="Penghentian diminta. Antrean berikutnya tidak akan diproses.")


def latest_outputs():
    dapil_files = sorted(DAPIL_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return dapil_files, CHECKPOINT_PATH if CHECKPOINT_PATH.exists() else None
