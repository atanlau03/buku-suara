"""
gdrive_batch.py -- Pemrosesan batch PER DAPIL dari Google Drive.

Struktur folder yang diasumsikan (sesuai Drive Anda):

    ROOT/
      <Nama Dapil>/
        <Nama Kabupaten/Kota>/
          <Nama Kecamatan>/
            <file PDF data pemilu kecamatan>.pdf

Alur:
 1. Terhubung ke Google Drive (atau folder lokal, untuk Drive Desktop / uji coba).
 2. Pilih SATU dapil -> telusuri semua kabupaten -> kecamatan -> PDF.
 3. Setiap PDF diperiksa dulu: apakah text layer-nya layak dibaca engine?
      - layak     -> diproses engine (process_pdf_local_engine)
      - tidak     -> DILEWATI (dicatat beserta alasannya, tidak di-OCR)
 4. Setiap kecamatan langsung dicatat ke file CHECKPOINT (Excel). Kalau proses
    berhenti di tengah jalan, jalankan lagi: yang sudah SELESAI tidak diulang.
 5. Hasil tiap kecamatan disimpan, lalu digabung menjadi satu Excel per dapil.

STATUS pada checkpoint:
    SELESAI        sudah terbaca. Tidak diproses ulang.
    DILEWATI       text layer buruk / format tidak didukung. Tidak diproses ulang
                   (kecuali --retry-skipped atau status diubah manual jadi ULANG).
    ERROR          gagal unduh / engine error. Otomatis dicoba lagi di run berikutnya.
    TIDAK_ADA_PDF  folder kecamatan tidak berisi PDF.
    ULANG          isi manual di Excel untuk memaksa satu baris diproses ulang.

------------------------------------------------------------------
SETUP GOOGLE DRIVE (sekali saja)
------------------------------------------------------------------
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2

 Cara A -- OAuth (akun Google Anda sendiri, paling mudah):
    1. console.cloud.google.com -> buat project -> aktifkan "Google Drive API".
    2. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
       -> tipe "Desktop app" -> unduh JSON, simpan sebagai credentials.json
       di folder yang sama dengan file ini.
    3. Run pertama membuka browser untuk login; token disimpan di token.json.
 Cara B -- Service account: bagikan (share) folder ROOT ke email service account,
    lalu jalankan dengan --service-account service_account.json.

Hak akses yang diminta hanya BACA (drive.readonly).

------------------------------------------------------------------
CARA PAKAI
------------------------------------------------------------------
    python gdrive_batch.py --root "<URL atau ID folder ROOT>" --list-dapil
    python gdrive_batch.py --root "<URL atau ID folder ROOT>" --dapil "JAWA BARAT VIII"

    # Folder lokal (mis. hasil unduhan / Google Drive for Desktop):
    python gdrive_batch.py --local "D:\\Pemilu" --dapil "JAWA BARAT VIII"

    # Kenapa satu file dilewati? (unduh filenya dulu, lalu:)
    python gdrive_batch.py --diagnose "C:\\path\\file.pdf"

Dari Python / Streamlit:
    from gdrive_batch import DriveSource, run_dapil
    src = DriveSource()   # atau LocalSource()
    hasil = run_dapil(src, root_id, "JAWA BARAT VIII",
                      progress_callback=lambda v, m: ...)
"""

import argparse
import importlib
import importlib.util
import io
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import fitz
import pandas as pd

# Nama modul engine (tanpa .py), dicoba berurutan. Tambahkan bila nama Anda lain.
ENGINE_MODULE_CANDIDATES = ("local_ocr_engine", "ocr_engine_buku_suara")


def _load_engine():
    for name in ENGINE_MODULE_CANDIDATES:
        # find_spec dulu supaya error DI DALAM engine (mis. pytesseract belum
        # terpasang) tidak tertelan dan tersamar sebagai "modul tidak ada".
        if importlib.util.find_spec(name) is not None:
            return importlib.import_module(name)
    raise ImportError(
        "Modul engine tidak ditemukan. Letakkan gdrive_batch.py satu folder "
        f"dengan salah satu dari: {', '.join(ENGINE_MODULE_CANDIDATES)}"
    )


engine = _load_engine()

# Satu proses batch pada satu waktu (melindungi checkpoint dari tabrakan,
# mis. tombol Mulai ditekan dari dua tab browser).
_RUN_LOCK = threading.Lock()


def is_batch_running():
    """True bila ada proses batch yang sedang berjalan di server ini
    (termasuk yang dimulai dari sesi/tab browser lain)."""
    return _RUN_LOCK.locked()


# ============================================================
# KONSTANTA
# ============================================================

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

STATUS_DONE = "SELESAI"
STATUS_SKIP = "DILEWATI"
STATUS_ERROR = "ERROR"
STATUS_EMPTY = "TIDAK_ADA_PDF"
STATUS_RETRY = "ULANG"

CHECKPOINT_COLUMNS = [
    "dapil", "kabupaten", "kecamatan", "nama_file", "file_id",
    "ukuran_mb", "status", "alasan",
    "jumlah_halaman", "halaman_bertext", "halaman_scan",
    "jumlah_kelurahan", "jumlah_tps",
    "kelurahan_perlu_cek", "kelurahan_perlu_cek_nama",
    "file_hasil", "diproses_pada", "durasi_detik",
]

RESULT_SHEET_ORDER = ["summary", "ranges", "tps", "parties", "validation", "db"]


def _log(msg):
    print(msg, flush=True)


# ============================================================
# SUMBER DATA: GOOGLE DRIVE / FOLDER LOKAL
# ============================================================

@dataclass
class Node:
    id: str
    name: str
    is_folder: bool
    size: int = 0
    mime: str = ""


def is_pdf(node):
    return (
        not node.is_folder
        and (
            node.mime == "application/pdf"
            or node.name.lower().endswith(".pdf")
        )
    )


def _retry(fn, tries=5, base=2.0):
    """Ulangi panggilan API bila error sementara (rate limit / 5xx / koneksi)."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            transient = (
                status in (429, 500, 502, 503, 504)
                or (status == 403 and "ateLimit" in str(e))
                or isinstance(e, (ConnectionError, TimeoutError))
            )
            if not transient or attempt == tries - 1:
                raise
            time.sleep(base ** attempt + random.random())


def parse_drive_id(value):
    """Terima URL folder Drive atau ID polos, kembalikan ID-nya."""
    value = (value or "").strip()
    m = (
        re.search(r"/folders/([A-Za-z0-9_-]+)", value)
        or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    )
    result = m.group(1) if m else value

    if not result:
        raise ValueError(
            "Link/ID folder Google Drive masih kosong atau tidak valid."
        )

    return result


class LocalSource:
    """Membaca struktur folder yang sama dari disk lokal."""

    def list_children(self, folder_id):
        folder_id = (folder_id or "").strip()

        if not folder_id:
            raise FileNotFoundError(
                "Folder lokal induk masih kosong. Isi kolom 'Folder lokal induk' "
                "terlebih dahulu (path lengkap ke folder yang berisi folder-folder dapil)."
            )

        if not os.path.isdir(folder_id):
            raise FileNotFoundError(
                f"Folder '{folder_id}' tidak ditemukan atau bukan folder."
            )

        nodes = []
        for name in sorted(os.listdir(folder_id), key=str.lower):
            path = os.path.join(folder_id, name)
            isdir = os.path.isdir(path)
            nodes.append(Node(
                id=path,
                name=name,
                is_folder=isdir,
                size=0 if isdir else os.path.getsize(path),
            ))
        return nodes

    def download(self, node):
        with open(node.id, "rb") as f:
            return f.read()


class DriveSource:
    """Google Drive API v3 (baca saja)."""

    # Mengunci proses LOGIN INTERAKTIF (bukan pemakaian API setelahnya) supaya
    # tidak terbuka dua jendela/link login sekaligus bila dua bagian aplikasi
    # sama-sama butuh kredensial pada saat bersamaan sebelum token.json ada.
    # Panggilan kedua akan menunggu, lalu memakai token yang baru saja
    # dibuat oleh panggilan pertama -- tanpa perlu login ulang.
    _AUTH_LOCK = threading.Lock()

    def __init__(
        self,
        credentials_file="credentials.json",
        token_file="token.json",
        service_account_file=None,
        service=None,
    ):
        self.service = service or self._build_service(
            credentials_file, token_file, service_account_file
        )

    @staticmethod
    def _build_service(credentials_file, token_file, service_account_file):
        from googleapiclient.discovery import build

        if service_account_file:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                service_account_file, scopes=DRIVE_SCOPES
            )
        else:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow

            with DriveSource._AUTH_LOCK:

                creds = None

                if os.path.exists(token_file):
                    creds = Credentials.from_authorized_user_file(
                        token_file, DRIVE_SCOPES
                    )

                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                    else:
                        if not os.path.exists(credentials_file):
                            raise FileNotFoundError(
                                f"{credentials_file} tidak ditemukan. Lihat bagian "
                                "SETUP GOOGLE DRIVE di atas file gdrive_batch.py."
                            )
                        flow = InstalledAppFlow.from_client_secrets_file(
                            credentials_file, DRIVE_SCOPES
                        )
                        creds = flow.run_local_server(port=0)

                    with open(token_file, "w") as f:
                        f.write(creds.to_json())

        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_children(self, folder_id):
        folder_id = (folder_id or "").strip()

        if not folder_id:
            raise ValueError(
                "Folder induk Google Drive masih kosong. Isi kolom 'Folder induk "
                "Google Drive (URL atau ID)' terlebih dahulu."
            )

        nodes = []
        page_token = None

        while True:

            def call():
                return self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=(
                        "nextPageToken, files(id, name, mimeType, size, "
                        "shortcutDetails(targetId, targetMimeType))"
                    ),
                    pageSize=1000,
                    pageToken=page_token,
                    orderBy="name",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()

            resp = _retry(call)

            for f in resp.get("files", []):
                file_id = f["id"]
                mime = f.get("mimeType", "")

                if mime == SHORTCUT_MIME:
                    detail = f.get("shortcutDetails") or {}
                    file_id = detail.get("targetId", file_id)
                    mime = detail.get("targetMimeType", mime)

                nodes.append(Node(
                    id=file_id,
                    name=f.get("name", ""),
                    is_folder=(mime == FOLDER_MIME),
                    size=int(f.get("size") or 0),
                    mime=mime,
                ))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return nodes

    def download(self, node):
        from googleapiclient.http import MediaIoBaseDownload

        def call():
            request = self.service.files().get_media(
                fileId=node.id, supportsAllDrives=True
            )
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buffer, request, chunksize=8 * 1024 * 1024
            )
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        return _retry(call)


# ============================================================
# PENELUSURAN: DAPIL -> KABUPATEN -> KECAMATAN -> PDF
# ============================================================

@dataclass
class Job:
    dapil: str
    kabupaten: str
    kecamatan: str
    kecamatan_folder_id: str
    file: Optional[Node] = None
    multi_file: bool = False

    @property
    def key(self):
        if self.file is not None:
            return self.file.id
        return f"EMPTY::{self.kecamatan_folder_id}"


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip().upper()


def safe_name(s, default="tanpa_nama"):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    return (s or default)[:80]


def list_dapil(source, root_id):
    return [n for n in source.list_children(root_id) if n.is_folder]


def resolve_dapil(source, root_id, name):
    folders = list_dapil(source, root_id)
    wanted = _norm(name)

    exact = [f for f in folders if _norm(f.name) == wanted]
    if exact:
        return exact[0]

    partial = [f for f in folders if wanted and wanted in _norm(f.name)]
    if len(partial) == 1:
        return partial[0]

    available = ", ".join(f.name for f in folders) or "(kosong)"

    if len(partial) > 1:
        raise ValueError(
            f"Nama dapil '{name}' ambigu. Cocok dengan: "
            f"{', '.join(f.name for f in partial)}"
        )

    raise ValueError(
        f"Dapil '{name}' tidak ditemukan. Dapil yang ada: {available}"
    )


def discover_jobs(source, dapil_node, log=_log):
    jobs = []

    kabupaten_list = [
        n for n in source.list_children(dapil_node.id) if n.is_folder
    ]

    for kab in kabupaten_list:
        kecamatan_list = [
            n for n in source.list_children(kab.id) if n.is_folder
        ]

        if not kecamatan_list:
            log(f"   ! Peringatan: '{kab.name}' tidak berisi folder kecamatan.")

        for kec in kecamatan_list:
            pdfs = [n for n in source.list_children(kec.id) if is_pdf(n)]

            if not pdfs:
                jobs.append(Job(dapil_node.name, kab.name, kec.name, kec.id))
                continue

            for pdf in pdfs:
                jobs.append(Job(
                    dapil=dapil_node.name,
                    kabupaten=kab.name,
                    kecamatan=kec.name,
                    kecamatan_folder_id=kec.id,
                    file=pdf,
                    multi_file=len(pdfs) > 1,
                ))

    return jobs


# ============================================================
# PEMERIKSAAN TEXT LAYER
# ============================================================

def check_text_layer(pdf_bytes, min_chars=100, min_text_ratio=0.5):
    """
    Cek cepat (tanpa OCR) apakah PDF layak dibaca engine.

    Syarat layak:
      1. PDF bisa dibuka, tidak terenkripsi, punya halaman.
      2. Minimal `min_text_ratio` halaman punya text layer (>= min_chars huruf).
      3. Minimal satu halaman penutup desa terdeteksi dari text layer
         (memakai engine.is_closing_page, yaitu aturan yang sama dengan engine).

    Return: (ok, alasan, info)
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return False, f"PDF tidak dapat dibuka ({type(e).__name__})", {}

    try:
        if doc.needs_pass:
            return False, "PDF terenkripsi / berpassword", {}

        total = len(doc)
        if total == 0:
            return False, "PDF tidak memiliki halaman", {}

        text_pages = 0
        scan_pages = 0  # halaman yang hampir seluruhnya berupa gambar
        total_chars = 0
        closing_pages = 0

        for i in range(total):
            try:
                page = doc.load_page(i)
                text = page.get_text("text") or ""
            except Exception:
                page, text = None, ""

            chars = len(text.strip())
            total_chars += chars

            if chars >= min_chars:
                text_pages += 1

            # Logo/latar kecil TIDAK dihitung: halaman scan = ada gambar yang
            # menutup >= 40% luas halaman.
            try:
                if page is not None:
                    area = page.rect.width * page.rect.height
                    biggest = max(
                        (fitz.Rect(i["bbox"]).get_area() for i in page.get_image_info()),
                        default=0,
                    )
                    if area > 0 and biggest / area >= 0.4:
                        scan_pages += 1
            except Exception:
                pass

            if engine.is_closing_page(text):
                closing_pages += 1

        info = {
            "halaman": total,
            "halaman_bertext": text_pages,
            "halaman_scan": scan_pages,
            "total_huruf": total_chars,
            "halaman_penutup": closing_pages,
        }

        if text_pages / total < min_text_ratio:
            if scan_pages / total >= 0.5:
                jenis = (
                    f"{scan_pages} halaman berupa gambar penuh -> hasil scan"
                )
            else:
                jenis = (
                    "bukan gambar penuh juga (teks mungkin berupa garis/"
                    "vektor atau font tidak terbaca)"
                )

            return False, (
                f"Text layer minim: hanya {text_pages} dari {total} halaman "
                f"berisi teks; {jenis}"
            ), info

        if closing_pages == 0:
            return False, (
                "Tidak ada halaman penutup desa yang terbaca dari text layer "
                "(format/kualitas tidak didukung)"
            ), info

        return True, "", info

    finally:
        doc.close()


# ============================================================
# CHECKPOINT (EXCEL)
# ============================================================

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _autofit(ws, max_width=60):
    for col in ws.columns:
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(longest + 2, 10), max_width)
    ws.freeze_panes = "A2"


class Checkpoint:
    """
    File Excel yang mencatat status setiap file kecamatan.

    Sheet "checkpoint"   : semua baris (dipakai sistem untuk melanjutkan proses).
    Sheet "sulit_dibaca" : hanya DILEWATI + ERROR (daftar kerja untuk Anda).
    """

    def __init__(self, path, log=_log):
        self.path = path
        self.log = log
        self.rows = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return

        try:
            df = pd.read_excel(self.path, sheet_name="checkpoint", dtype=str)
        except Exception as e:
            # Jangan menimpa diam-diam: file mungkin rusak / diedit salah.
            raise RuntimeError(
                f"Checkpoint '{self.path}' tidak bisa dibaca ({e}). "
                "Perbaiki atau ganti namanya agar tidak tertimpa."
            )

        df = df.where(pd.notna(df), None)

        for rec in df.to_dict("records"):
            key = rec.get("file_id")
            if key:
                rec["status"] = _norm(rec.get("status"))
                self.rows[key] = rec

    def get(self, key):
        return self.rows.get(key)

    def drop(self, key):
        self.rows.pop(key, None)

    def should_skip(self, key, retry_skipped=False):
        prev = self.rows.get(key)
        if not prev:
            return False
        status = prev.get("status")
        if status == STATUS_DONE:
            return True
        if status == STATUS_SKIP and not retry_skipped:
            return True
        return False  # ERROR, ULANG, TIDAK_ADA_PDF, lainnya -> dikerjakan

    def record(self, row):
        self.rows[row["file_id"]] = row
        self.save()

    def save(self):
        df = pd.DataFrame(list(self.rows.values()), columns=CHECKPOINT_COLUMNS)
        df = df.sort_values(
            ["dapil", "kabupaten", "kecamatan", "nama_file"],
            kind="stable", na_position="last",
        )
        hard = df[df["status"].isin([STATUS_SKIP, STATUS_ERROR])]

        tmp = self.path[:-5] + ".tmp.xlsx" if self.path.lower().endswith(".xlsx") else self.path + ".tmp.xlsx"

        with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="checkpoint", index=False)
            hard.to_excel(writer, sheet_name="sulit_dibaca", index=False)
            for ws in writer.book.worksheets:
                _autofit(ws)

        for _ in range(3):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(2)  # kemungkinan file sedang dibuka di Excel

        alt = self.path[:-5] + f"_cadangan_{datetime.now():%H%M%S}.xlsx"
        os.replace(tmp, alt)
        self.log(
            f"   ! '{self.path}' sedang terkunci (dibuka di Excel?). "
            f"Checkpoint disimpan ke '{alt}'."
        )


# ============================================================
# PROSES SATU FILE
# ============================================================

def assess_result(hasil):
    """Ringkas kualitas hasil engine untuk dicatat di checkpoint."""
    ranges = hasil.get("ranges")
    parties = hasil.get("parties")
    validation = hasil.get("validation")
    tps = hasil.get("tps")

    n_kel = 0 if ranges is None else len(ranges)
    n_tps = 0 if tps is None else len(tps)
    expected_parties = len(engine.PARTY_NAMES)

    review = set()

    if validation is not None and len(validation):
        for _, r in validation.iterrows():
            if str(r.get("status")).strip().upper() != "OK":
                review.add(str(r.get("kelurahan")))

    if parties is not None and len(parties):
        psum = parties.groupby("kelurahan")["suara_akhir_partai"].sum()
        pcnt = parties.groupby("kelurahan")["partai"].nunique()
    else:
        psum = pd.Series(dtype=float)
        pcnt = pd.Series(dtype=float)

    if n_kel:
        for _, r in ranges.iterrows():
            kel = r.get("kelurahan")
            sah = r.get("suara_sah")

            if (
                pcnt.get(kel, 0) != expected_parties
                or pd.isna(sah)
                or psum.get(kel) != sah
            ):
                review.add(str(kel))

    return {
        "jumlah_kelurahan": n_kel,
        "jumlah_tps": n_tps,
        "kelurahan_perlu_cek": len(review),
        "kelurahan_perlu_cek_nama": "; ".join(sorted(review))[:300],
    }


def _tag(df, meta):
    df = df.copy()
    for i, (k, v) in enumerate(meta.items()):
        df.insert(i, k, v)
    return df


def _write_result(hasil, meta, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name in RESULT_SHEET_ORDER:
            df = hasil.get(name)
            if isinstance(df, pd.DataFrame):
                _tag(df, meta).to_excel(writer, sheet_name=name, index=False)


def process_job(source, job, out_dir, dpi=300, log=_log, on_result=None):
    started = time.time()

    row = {c: None for c in CHECKPOINT_COLUMNS}
    row.update(
        dapil=job.dapil,
        kabupaten=job.kabupaten,
        kecamatan=job.kecamatan,
        nama_file=job.file.name if job.file else None,
        file_id=job.key,
        diproses_pada=_now(),
    )

    def finish(status, reason=""):
        row["status"] = status
        row["alasan"] = reason
        row["durasi_detik"] = round(time.time() - started, 1)
        return row

    if job.file is None:
        return finish(STATUS_EMPTY, "Folder kecamatan tidak berisi file PDF")

    try:
        data = source.download(job.file)
    except Exception as e:
        return finish(STATUS_ERROR, f"Gagal mengunduh: {type(e).__name__}: {e}"[:300])

    row["ukuran_mb"] = round(len(data) / 1048576, 2)

    ok, reason, info = check_text_layer(data)
    row["jumlah_halaman"] = info.get("halaman")
    row["halaman_bertext"] = info.get("halaman_bertext")
    row["halaman_scan"] = info.get("halaman_scan")

    if not ok:
        return finish(STATUS_SKIP, reason)

    try:
        hasil = engine.process_pdf_local_engine(
            data,
            file_name=job.file.name,
            progress_callback=None,
            dpi_val=dpi,
        )
    except Exception as e:
        return finish(STATUS_ERROR, f"Engine error: {type(e).__name__}: {e}"[:300])

    quality = assess_result(hasil)
    row.update(quality)

    if quality["jumlah_kelurahan"] == 0 or quality["jumlah_tps"] == 0:
        return finish(STATUS_SKIP, "Engine tidak menemukan kelurahan/TPS yang dapat dibaca")

    stem = safe_name(os.path.splitext(job.file.name)[0])
    base = f"{safe_name(job.kecamatan)}__{stem}" if job.multi_file else safe_name(job.kecamatan)
    result_path = os.path.join(
        out_dir, safe_name(job.dapil), safe_name(job.kabupaten), base + ".xlsx"
    )

    meta = {
        "sumber_dapil": job.dapil,
        "sumber_kabupaten": job.kabupaten,
        "sumber_kecamatan": job.kecamatan,
        "sumber_file": job.file.name,
    }

    try:
        _write_result(hasil, meta, result_path)
    except Exception as e:
        return finish(STATUS_ERROR, f"Gagal menyimpan hasil: {type(e).__name__}: {e}"[:300])

    # Hook opsional (mis. simpan ke database / arsip Dashboard).
    if on_result is not None:
        try:
            on_result(job, hasil, result_path)
        except Exception as e:
            return finish(
                STATUS_ERROR,
                f"Gagal menyimpan ke database/arsip: {type(e).__name__}: {e}"[:300],
            )

    row["file_hasil"] = result_path
    note = ""
    if quality["kelurahan_perlu_cek"]:
        note = f"{quality['kelurahan_perlu_cek']} kelurahan perlu dicek"
    return finish(STATUS_DONE, note)


# ============================================================
# GABUNG HASIL PER DAPIL
# ============================================================

def merge_dapil_results(dapil_dir, out_path, log=_log):
    frames = defaultdict(list)

    for root, _, files in os.walk(dapil_dir):
        for fn in sorted(files):
            if not fn.lower().endswith(".xlsx") or fn.startswith(("GABUNGAN_", "~$")):
                continue

            path = os.path.join(root, fn)

            try:
                sheets = pd.read_excel(path, sheet_name=None)
            except Exception as e:
                log(f"   ! Lewati '{path}' saat menggabung: {e}")
                continue

            for name, df in sheets.items():
                if len(df):
                    frames[name].append(df)

    if not frames:
        return None

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name in RESULT_SHEET_ORDER:
            if name in frames:
                pd.concat(frames[name], ignore_index=True).to_excel(
                    writer, sheet_name=name, index=False
                )

    return out_path


# ============================================================
# FUNGSI UTAMA: SATU DAPIL SEKALIGUS
# ============================================================

def run_dapil(
    source,
    root_id,
    dapil_name,
    out_dir="hasil_pemilu",
    checkpoint_path="checkpoint_pemilu.xlsx",
    dpi=300,
    retry_skipped=False,
    progress_callback=None,
    log=_log,
    on_result=None,
    should_stop=None,
):
    """
    on_result(job, hasil, result_path) : dipanggil untuk setiap kecamatan yang
                                         berhasil dibaca (mis. simpan ke DB).
    should_stop()                      : return True untuk berhenti dengan rapi
                                         setelah kecamatan yang sedang berjalan.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("Sudah ada proses batch yang sedang berjalan.")

    try:
        return _run_dapil_locked(
            source, root_id, dapil_name, out_dir, checkpoint_path, dpi,
            retry_skipped, progress_callback, log, on_result, should_stop,
        )
    finally:
        _RUN_LOCK.release()


def _run_dapil_locked(
    source, root_id, dapil_name, out_dir, checkpoint_path, dpi,
    retry_skipped, progress_callback, log, on_result, should_stop,
):
    root_id = parse_drive_id(root_id) if not isinstance(source, LocalSource) else root_id

    checkpoint = Checkpoint(checkpoint_path, log=log)

    dapil_node = resolve_dapil(source, root_id, dapil_name)
    log(f"Dapil: {dapil_node.name}")
    log("Menelusuri kabupaten / kecamatan ...")

    jobs = discover_jobs(source, dapil_node, log=log)
    total = len(jobs)
    log(f"Ditemukan {total} berkas/folder kecamatan.")

    stats = Counter()
    stopped = False

    for index, job in enumerate(jobs, start=1):
        label = f"{job.kabupaten} / {job.kecamatan}"

        if should_stop is not None and should_stop():
            stopped = True
            log("Dihentikan oleh pengguna. Kemajuan tersimpan di checkpoint.")
            break

        if checkpoint.should_skip(job.key, retry_skipped=retry_skipped):
            prev = checkpoint.get(job.key).get("status")
            stats[f"lewat_{prev}"] += 1
            log(f"[{index}/{total}] {label}: sudah tercatat {prev}, dilewati.")

        else:
            log(f"[{index}/{total}] {label}: memproses ...")
            row = process_job(source, job, out_dir, dpi=dpi, log=log, on_result=on_result)
            checkpoint.record(row)

            if job.file is not None:
                checkpoint.drop(f"EMPTY::{job.kecamatan_folder_id}")

            stats[row["status"]] += 1
            detail = f" ({row['alasan']})" if row.get("alasan") else ""
            log(f"      -> {row['status']}{detail}")

        engine.report_progress(
            progress_callback,
            index / max(total, 1),
            f"{index}/{total}: {label}",
        )

    dapil_dir = os.path.join(out_dir, safe_name(dapil_node.name))
    merged = None

    if os.path.isdir(dapil_dir):
        merged = merge_dapil_results(
            dapil_dir,
            os.path.join(dapil_dir, f"GABUNGAN_{safe_name(dapil_node.name)}.xlsx"),
            log=log,
        )

    log("Dihentikan." if stopped else "Selesai.")
    log(f"Ringkasan: {dict(stats)}")
    if merged:
        log(f"Hasil gabungan dapil: {merged}")
    log(f"Checkpoint: {checkpoint_path}")

    return {
        "dapil": dapil_node.name,
        "total": total,
        "stats": dict(stats),
        "merged_path": merged,
        "checkpoint_path": checkpoint_path,
        "stopped": stopped,
        "rows": [checkpoint.rows[j.key] for j in jobs if j.key in checkpoint.rows],
    }


# ============================================================
# CLI
# ============================================================

def diagnose_pdf(path, out=print):
    """Diagnosis satu PDF lokal: kenapa ia lolos / dilewati."""
    with open(path, "rb") as f:
        data = f.read()

    ok, reason, info = check_text_layer(data)

    out(f"Berkas            : {path}")
    out(f"Ukuran            : {len(data) / 1048576:.2f} MB")
    out(f"Jumlah halaman    : {info.get('halaman', '?')}")
    out(f"Halaman berteks   : {info.get('halaman_bertext', '?')}  (>= 100 huruf)")
    out(f"Halaman scan      : {info.get('halaman_scan', '?')}  (gambar menutup >= 40% halaman)")
    out(f"Total huruf       : {info.get('total_huruf', '?')}")
    out(f"Halaman penutup   : {info.get('halaman_penutup', '?')}  (terdeteksi dari teks)")
    out(f"Keputusan         : {'LOLOS' if ok else 'DILEWATI'}")

    if not ok:
        out(f"Alasan            : {reason}")
        if info.get("halaman_scan", 0) and info.get("halaman_bertext", 0) == 0:
            out("Kesimpulan        : PDF ini hasil scan (gambar saja). Engine berbasis "
                "text layer tidak bisa membacanya; perlu jalur OCR/AI vision.")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        first = (doc.load_page(0).get_text("text") or "").strip()
        out("Cuplikan teks hal. 1: " + (repr(first[:200]) if first else "(kosong)"))
        doc.close()
    except Exception:
        pass

    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Proses satu dapil dari Google Drive (atau folder lokal)."
    )

    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--root", help="URL / ID folder ROOT di Google Drive (berisi folder dapil).")
    src.add_argument("--local", help="Folder lokal ROOT (berisi folder dapil).")

    ap.add_argument("--dapil", help="Nama dapil yang dikerjakan.")
    ap.add_argument("--list-dapil", action="store_true", help="Tampilkan daftar dapil lalu keluar.")
    ap.add_argument("--out", default="hasil_pemilu", help="Folder hasil (default: hasil_pemilu).")
    ap.add_argument("--checkpoint", default="checkpoint_pemilu.xlsx", help="File checkpoint Excel.")
    ap.add_argument("--credentials", default="credentials.json")
    ap.add_argument("--token", default="token.json")
    ap.add_argument("--service-account", default=None)
    ap.add_argument("--retry-skipped", action="store_true", help="Coba lagi file berstatus DILEWATI.")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--diagnose", metavar="PDF", help="Diagnosis satu file PDF lokal (tanpa Drive).")

    args = ap.parse_args(argv)

    if args.diagnose:
        diagnose_pdf(args.diagnose)
        return 0

    if not (args.root or args.local):
        ap.error("wajib salah satu: --root, --local, atau --diagnose")

    if args.local:
        source = LocalSource()
        root = args.local
    else:
        source = DriveSource(
            credentials_file=args.credentials,
            token_file=args.token,
            service_account_file=args.service_account,
        )
        root = parse_drive_id(args.root)

    if args.list_dapil or not args.dapil:
        print("Dapil yang tersedia:")
        for f in list_dapil(source, root):
            print(f"  - {f.name}")
        return 0

    run_dapil(
        source, root, args.dapil,
        out_dir=args.out,
        checkpoint_path=args.checkpoint,
        dpi=args.dpi,
        retry_skipped=args.retry_skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())