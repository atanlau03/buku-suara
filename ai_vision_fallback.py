"""
AI Vision Fallback (Gemini API) untuk Local OCR Engine Buku Suara
==================================================================

Modul ini menambahkan Gemini API (model vision Google) sebagai FALLBACK
TERAKHIR, dipakai HANYA ketika:

  1. extract_closing_vote_data() (teks linear) GAGAL membaca lengkap, DAN
  2. extract_closing_votes_positional() (OCR koordinat) JUGA gagal.

Nama file & fungsi di modul ini SENGAJA dibuat sama persis dengan yang
dipanggil oleh local_ocr_engine.py (AI_VISION_AVAILABLE,
extract_closing_data_with_ai, extract_party_votes_with_ai), supaya
tinggal ganti isi file ini tanpa perlu mengubah local_ocr_engine.py
sama sekali -- baik mau pakai Gemini, Claude, atau backend lain.

Prinsip desain (PENTING dibaca sebelum mengubah):

  - AI vision TIDAK menggantikan Tesseract. Ini fallback TERAKHIR, dipanggil
    seminimal mungkin untuk menghemat biaya & waktu.
  - Model DIINSTRUKSIKAN untuk mengisi `null` kalau angka tidak yakin
    terbaca -- BUKAN menebak. Ini mengurangi risiko "halusinasi" angka
    yang terlihat masuk akal padahal salah, yang berbahaya untuk data
    suara pemilu.
  - Respons dipaksa dalam format JSON murni (response_mime_type=
    "application/json") supaya tidak perlu menebak-nebak parsing teks
    bebas dari model.
  - Semua hasil dari modul ini TETAP harus lolos validasi matematika
    (suara_sah + suara_tidak_sah == total_suara) yang sudah ada di
    build_validation_dataframe(). Kalau tidak lolos, akan tetap muncul
    sebagai "PERLU DICEK" di tabel Validasi -- JANGAN dianggap otomatis
    benar hanya karena berasal dari AI.
  - Jika GEMINI_API_KEY tidak diset / package belum terpasang, semua
    fungsi di modul ini diam-diam mengembalikan None / [], supaya
    engine tetap jalan normal tanpa AI (mundur ke perilaku lama).

CARA MENGAKTIFKAN:
  1. pip install google-genai
  2. Dapatkan API key gratis di https://aistudio.google.com/apikey
  3. Set environment variable GEMINI_API_KEY (lihat panduan terpisah).
  4. Modul ini otomatis aktif begitu package + API key terdeteksi.

ROTASI OTOMATIS BANYAK API KEY (opsional, disarankan untuk PDF besar):
  Kalau kamu punya lebih dari satu API key (misal dari beberapa akun
  Google, untuk menghindari limit kuota gratis), pisahkan dengan koma
  di environment variable yang SAMA:

      setx GEMINI_API_KEY "AIzaSy_key_pertama,AIzaSy_key_kedua,AIzaSy_key_ketiga"

  Modul ini akan otomatis PINDAH ke key berikutnya begitu mendeteksi
  error kuota/rate-limit (429 / RESOURCE_EXHAUSTED / quota) dari key
  yang sedang dipakai, lalu MENGULANG permintaan yang sama dengan key
  baru. Kalau SEMUA key habis kuotanya, fungsi mengembalikan None/[]
  seperti biasa (engine tetap jalan, halaman itu masuk "PERLU DICEK").

CATATAN MODEL:
  Google cukup sering memperbarui daftar model Gemini yang tersedia.
  Kalau DEFAULT_MODEL di bawah suatu saat error "model not found",
  cek daftar model terkini di https://ai.google.dev/gemini-api/docs/models
  dan ganti nilainya -- tidak perlu ubah bagian lain di file ini.
"""

import io
import json
import os
import re
import time
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------
# Deteksi apakah package 'google-genai' terpasang & API key tersedia.
# Kalau salah satu tidak ada, AI_VISION_AVAILABLE = False dan semua
# fungsi di bawah otomatis jadi no-op (aman, tidak bikin crash).
# --------------------------------------------------------------

try:
    from google import genai
    from google.genai import types as genai_types
    _GENAI_PACKAGE_OK = True
except ImportError:
    _GENAI_PACKAGE_OK = False

# --------------------------------------------------------------
# ROTASI BANYAK API KEY
#
# GEMINI_API_KEY boleh berisi SATU key, atau BEBERAPA key dipisah koma:
#   "AIzaSy_key1,AIzaSy_key2,AIzaSy_key3"
#
# _API_KEYS menyimpan semua key yang valid (sudah di-strip spasi,
# entri kosong dibuang). _key_index menyimpan key mana yang lagi
# dipakai sekarang -- pindah otomatis begitu kena error kuota/limit.
# --------------------------------------------------------------

_raw_keys = os.environ.get("GEMINI_API_KEYS", "")
_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

_key_index = 0
_client_cache = {}

AI_VISION_AVAILABLE = bool(_GENAI_PACKAGE_OK and _API_KEYS)


def _get_client():
    """Ambil (atau buat & cache) client untuk key yang sedang aktif."""

    if not _API_KEYS:
        return None

    key = _API_KEYS[_key_index % len(_API_KEYS)]

    if key not in _client_cache:
        try:
            _client_cache[key] = genai.Client(api_key=key)
        except Exception:
            return None

    return _client_cache[key]


def _is_quota_error(exc):
    """Deteksi apakah exception ini karena limit/kuota habis, bukan error lain."""

    message = str(exc).upper()

    keywords = (
        "429",
        "RESOURCE_EXHAUSTED",
        "QUOTA",
        "RATE LIMIT",
        "RATE_LIMIT",
        "TOO MANY REQUESTS",
    )

    return any(k in message for k in keywords)


def _rotate_key():
    """Pindah ke API key berikutnya dalam daftar (berputar/round-robin)."""

    global _key_index
    _key_index = (_key_index + 1) % max(len(_API_KEYS), 1)


# Model default: varian "flash" dipakai karena murah & cepat, cukup
# untuk tugas ekstraksi terstruktur seperti ini. Cek daftar model
# terkini di https://ai.google.dev/gemini-api/docs/models kalau nama
# ini sudah tidak berlaku lagi.
DEFAULT_MODEL = "gemini-2.5-flash"


# ================================================================
# UTIL: ENCODE GAMBAR
# ================================================================

def _image_to_jpeg_bytes(image, max_dimension=1600):
    """
    Konversi PIL Image ke bytes JPEG untuk dikirim ke API.

    max_dimension membatasi ukuran gambar terkirim -- untuk dokumen
    resolusi rendah (misal scan 72 DPI), mengirim gambar lebih besar
    dari ini tidak menambah detail yang terbaca, cuma menambah biaya
    token dan waktu.
    """

    img = image.convert("RGB")

    w, h = img.size
    scale = min(1.0, max_dimension / max(w, h))

    if scale < 1.0:
        img = img.resize(
            (int(w * scale), int(h * scale))
        )

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=92)

    return buffer.getvalue()


def _extract_json(text):
    """
    Ambil blok JSON dari respons model. Karena kita sudah memaksa
    response_mime_type="application/json", ini seharusnya langsung
    valid -- fungsi ini jaga-jaga saja kalau ada karakter tambahan.
    """

    if not text:
        return None

    text = text.strip()

    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"```\s*$", "", text)

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"[\{\[].*[\}\]]", text, flags=re.S)

    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None

    return None


def _safe_int(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else None

    return None


def _call_gemini(image_bytes, prompt, model=None, max_output_tokens=1000):
    """
    Panggilan generik ke Gemini dengan gambar + prompt, minta JSON.

    Kalau key yang sedang aktif kena error kuota/rate-limit, otomatis
    pindah ke key berikutnya dalam daftar dan ULANGI permintaan yang
    sama. Dicoba sampai SEMUA key sudah dicoba sekali; kalau semuanya
    gagal karena kuota, exception terakhir dilempar ke pemanggil
    (yang sudah membungkusnya dengan try/except -> None/[]).
    """

    model = model or DEFAULT_MODEL

    content_parts = [
        genai_types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        ),
        prompt,
    ]

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
        temperature=0,
    )

    last_error = None

    for attempt in range(max(len(_API_KEYS), 1)):

        client = _get_client()

        if client is None:
            raise RuntimeError("Tidak ada API key Gemini yang valid.")

        try:
            response = client.models.generate_content(
                model=model,
                contents=content_parts,
                config=config,
            )

            return response.text

        except Exception as exc:

            last_error = exc

            if _is_quota_error(exc) and len(_API_KEYS) > 1:
                # Key ini kena limit -- pindah ke key berikutnya dan coba lagi.
                _rotate_key()
                time.sleep(0.5)
                continue

            # Bukan error kuota (atau cuma ada 1 key) -> tidak ada
            # gunanya diulang, langsung lempar ke pemanggil.
            raise

    # Semua key sudah dicoba dan semuanya kena limit.
    raise last_error


# ================================================================
# EKSTRAKSI: HALAMAN PENUTUP DESA (SAH / TIDAK SAH / TOTAL)
# ================================================================

def extract_closing_data_with_ai(image, expected_tps=None, model=None):
    """
    Kirim gambar halaman penutup desa ke Gemini vision, minta baca
    jumlah_akhir_suara_sah, jumlah_akhir_suara_tidak_sah, dan
    jumlah_akhir_total_suara.

    Return dict {"suara_sah": int|None, "suara_tidak_sah": int|None,
    "total_suara": int|None} atau None kalau AI tidak tersedia / gagal
    total.

    PENTING: hasil ini TIDAK otomatis dipercaya -- kode pemanggil harus
    tetap menjalankan validasi matematika sebelum menganggapnya benar.
    """

    if not AI_VISION_AVAILABLE:
        return None

    try:
        image_bytes = _image_to_jpeg_bytes(image)

        tps_hint = (
            f"Halaman ini berisi data dari {expected_tps} TPS."
            if expected_tps
            else ""
        )

        prompt = f"""Ini adalah halaman penutup rekapitulasi suara Pemilu Indonesia (formulir KPU).
{tps_hint}

Tugasmu: baca baris "JUMLAH SELURUH SUARA SAH" (baris A), "JUMLAH SUARA TIDAK SAH" (baris B), dan "JUMLAH SELURUH SUARA SAH DAN TIDAK SAH" (baris C).

ATURAN PENTING:
- Kalau tabel terpecah jadi 2 bagian (ada kolom "JUMLAH PINDAHAN" di tabel pertama, "JUMLAH AKHIR" di tabel kedua), AMBIL ANGKA DARI KOLOM "JUMLAH AKHIR" (tabel kedua/terakhir), BUKAN "JUMLAH PINDAHAN".
- Kalau HANYA ADA SATU tabel (tidak ada kolom pindahan), angka paling kanan di baris tersebut adalah jawabannya.
- KALAU ANGKA TIDAK JELAS TERBACA (buram, tertutup, atau kamu tidak yakin), ISI DENGAN null. JANGAN MENEBAK. Ini data resmi pemilu, kesalahan tebakan lebih berbahaya daripada mengaku tidak tahu.

Balas dengan JSON persis format ini:
{{"suara_sah": <angka atau null>, "suara_tidak_sah": <angka atau null>, "total_suara": <angka atau null>}}"""

        text = _call_gemini(image_bytes, prompt, model=model, max_output_tokens=300)

        data = _extract_json(text)

        if not data:
            return None

        return {
            "suara_sah": _safe_int(data.get("suara_sah")),
            "suara_tidak_sah": _safe_int(data.get("suara_tidak_sah")),
            "total_suara": _safe_int(data.get("total_suara")),
        }

    except Exception:
        return None


# ================================================================
# EKSTRAKSI: SUARA PARTAI
# ================================================================

def extract_party_votes_with_ai(image, party_names, model=None):
    """
    Kirim gambar halaman tabel partai ke Gemini vision, minta baca
    "JUMLAH AKHIR" / "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON" untuk
    setiap partai yang tercetak di halaman itu.

    party_names: dict {nomor: nama_partai} (pakai PARTY_NAMES dari
    local_ocr_engine.py) supaya model tahu daftar partai resmi dan
    tidak salah eja/nomor.

    Return list of {"partai": int, "nama_partai": str,
    "suara_akhir_partai": int} untuk partai yang berhasil terbaca,
    atau [] kalau gagal/tidak tersedia.
    """

    if not AI_VISION_AVAILABLE:
        return []

    try:
        image_bytes = _image_to_jpeg_bytes(image)

        party_list_text = "\n".join(
            f"{num}. {name}" for num, name in sorted(party_names.items())
        )

        prompt = f"""Ini halaman rekapitulasi suara partai politik Pemilu Indonesia (formulir KPU / lampiran Model D/C-Hasil).

Daftar resmi nomor & nama partai:
{party_list_text}

Tugasmu: untuk SETIAP partai yang tabelnya muncul di halaman ini, baca angka pada baris "JUMLAH AKHIR" atau "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON (A.1+A.2)".

ATURAN PENTING:
- Kalau tabel partai itu terpecah (ada kolom "JUMLAH PINDAHAN"), AMBIL ANGKA DARI KOLOM "JUMLAH AKHIR" (paling kanan), BUKAN "JUMLAH PINDAHAN".
- KALAU ANGKA TIDAK JELAS TERBACA, JANGAN MASUKKAN partai itu ke hasil sama sekali (lebih baik kosong daripada menebak).
- Cocokkan nomor & nama partai dengan daftar resmi di atas.

Balas dengan JSON array, format:
[{{"partai": <nomor>, "suara_akhir_partai": <angka>}}, ...]

Kalau tidak ada partai yang bisa dibaca dengan yakin di halaman ini, balas: []"""

        text = _call_gemini(image_bytes, prompt, model=model, max_output_tokens=1500)

        data = _extract_json(text)

        if not isinstance(data, list):
            return []

        results = []

        for item in data:
            if not isinstance(item, dict):
                continue

            party_no = _safe_int(item.get("partai"))
            vote = _safe_int(item.get("suara_akhir_partai"))

            if party_no is None or vote is None:
                continue

            if party_no not in party_names:
                continue

            results.append(
                {
                    "partai": party_no,
                    "nama_partai": party_names.get(party_no, f"Partai {party_no}"),
                    "suara_akhir_partai": vote,
                }
            )

        return results

    except Exception:
        return []