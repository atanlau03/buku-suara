import json
import os
import re
import time
import google.generativeai as genai
import pandas as pd
import pymupdf
from PIL import Image

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# CATATAN KEAMANAN: sebelumnya di sini ada API key Gemini yang di-hardcode
# langsung di source code. Itu bocor begitu file/zip ini dibagikan ke siapa
# pun (termasuk ke saya barusan). API key tsb HARUS dianggap sudah bocor -->
# segera revoke/rotate di https://aistudio.google.com/ dan jangan pernah
# menaruh API key literal di source code lagi. Gunakan env var / secrets.toml
# yang TIDAK ikut ter-commit / ter-zip ke luar.


def get_pdf_page_count(file_bytes: bytes) -> int:
    doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    count = len(doc)
    doc.close()
    return count


def get_pdf_page_image(
    file_bytes: bytes, page_number: int, dpi: int = 300
) -> Image.Image:
    doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    page = doc.load_page(page_number - 1)
    zoom = dpi / 72
    mat = pymupdf.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img


def find_active_model(api_key: str) -> str:
    """Mendeteksi otomatis model Gemini yang aktif dan menghapus prefix 'models/'."""
    genai.configure(api_key=api_key)
    try:
        available_models = [
            m.name.replace("models/", "")
            for m in genai.list_models()
            if "generateContent" in m.supported_generation_methods
        ]
        # Prioritas: pakai alias "-latest" dulu (otomatis ikut model terbaru
        # yang direkomendasikan Google), baru nama spesifik per info error
        # Google (gemini-3.6-flash) sebagai cadangan.
        for target in [
            "gemini-flash-latest",
            "gemini-3.6-flash",
            "gemini-2.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
        ]:
            if target in available_models:
                return target
        if available_models:
            return available_models[0]
    except Exception as e:
        print(f"[find_active_model] gagal ambil daftar model: {e}")

    return "gemini-flash-latest"


def _generate_dengan_retry(model, prompt, img, max_percobaan=3):
    for percobaan in range(max_percobaan):
        try:
            return model.generate_content([prompt, img])
        except Exception as e:
            pesan = str(e)
            if "429" in pesan and percobaan < max_percobaan - 1:
                match = re.search(
                    r"retry_delay\s*\{\s*seconds:\s*(\d+)", pesan
                )
                tunggu = int(match.group(1)) + 3 if match else 55
                time.sleep(tunggu)
                continue
            raise
    raise RuntimeError("Gagal setelah beberapa percobaan.")


PROMPT_ADAPTIF = """
Kamu adalah sistem pembaca formulir rekapitulasi Pemilu Indonesia (C.Hasil / D.Hasil / DA1 / DB1).

TUGAS 1 - Kenali JENIS HALAMAN dari gambar:
- "PARTAI": Jika halaman memuat tabel perolehan suara nama-nama partai dan calon.
- "REKAP_SUARA": Jika halaman memuat tabel "DATA SUARA SAH DAN TIDAK SAH".

TUGAS 2 - Baca metadata wilayah & TPS dari kop formulir:
- "level": "TPS", "Kelurahan", "Kecamatan", "Kabupaten", atau "Provinsi"
- "provinsi", "kabupaten_kota", "kecamatan", "kelurahan"
- "tps": Nomor TPS jika formulir tingkat TPS (contoh: "TPS 001"). Isi "-" jika formulir tingkat Kecamatan/Kabupaten.
- "jumlah_tps": Baca angka total/jumlah TPS yang dicatat di header/rekap (contoh: 25). Jika tidak ada/tidak tertulis, isi "-".

TUGAS 3 - Ekstrak Data Sesuai Jenis Halaman:
1. Jika jenis_halaman = "PARTAI":
   - Ekstrak total suara sah per partai ke dalam list "hasil_partai".
2. Jika jenis_halaman = "REKAP_SUARA":
   - Ambil nilai dari kolom "JUMLAH AKHIR" untuk Suara Sah, Tidak Sah, dan Total.

Kembalikan HANYA JSON standar berikut:
{
  "jenis_halaman": "PARTAI",
  "level": "Kecamatan",
  "provinsi": "LAMPUNG",
  "kabupaten_kota": "TULANG BAWANG",
  "kecamatan": "RAWA JITU SELATAN",
  "kelurahan": "GEDUNG KARYA JITU",
  "tps": "-",
  "jumlah_tps": "-",
  "rekap_suara": [],
  "hasil_partai": [
    {"nama_partai": "Partai Gelora", "jumlah_suara": 20, "nomor_urut": "-"}
  ]
}
"""


def process_single_page_ocr(
    img: Image.Image,
    provinsi="-",
    kab_kota="-",
    kecamatan="-",
    kelurahan="-",
    tps="-",
    api_key=None,
) -> tuple[pd.DataFrame, dict, str]:
    active_key = api_key if api_key else GEMINI_API_KEY
    if not active_key:
        return (
            pd.DataFrame(),
            {},
            "PERINGATAN: API Key Gemini belum diisi!",
        )

    try:
        selected_model_name = find_active_model(active_key)

        generation_config = {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "max_output_tokens": 8192,
        }
        model = genai.GenerativeModel(
            model_name=selected_model_name, generation_config=generation_config
        )

        response = _generate_dengan_retry(model, PROMPT_ADAPTIF, img)
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```[a-z]*\n", "", raw_text)
            raw_text = re.sub(r"\n```$", "", raw_text)

        parsed = json.loads(raw_text)

        def _get_val(val, fallback):
            return fallback if val in [None, "-", "", "null"] else str(val)

        info_wilayah = {
            "level": parsed.get("level", "-"),
            "jenis_halaman": parsed.get("jenis_halaman", "-"),
            "provinsi": _get_val(parsed.get("provinsi"), provinsi),
            "kab_kota": _get_val(parsed.get("kabupaten_kota"), kab_kota),
            "kecamatan": _get_val(parsed.get("kecamatan"), kecamatan),
            "kelurahan": _get_val(parsed.get("kelurahan"), kelurahan),
            "tps": _get_val(parsed.get("tps"), tps),
            "jumlah_tps": parsed.get("jumlah_tps", "-"),
        }

        rows = []
        jenis = str(parsed.get("jenis_halaman", "")).upper()

        if jenis == "REKAP_SUARA" or parsed.get("rekap_suara"):
            for item in parsed.get("rekap_suara", []):
                rows.append({
                    "provinsi": info_wilayah["provinsi"],
                    "kab_kota": info_wilayah["kab_kota"],
                    "kecamatan": info_wilayah["kecamatan"],
                    "kelurahan": info_wilayah["kelurahan"],
                    "tps": info_wilayah["tps"],
                    "jumlah_tps": info_wilayah["jumlah_tps"],
                    "partai": item.get("kategori", "-"),
                    "jumlah_suara": int(item.get("jumlah_suara", 0)),
                    "nomor_urut_partai": "-",
                })
        else:
            for r in parsed.get("hasil_partai", []):
                rows.append({
                    "provinsi": info_wilayah["provinsi"],
                    "kab_kota": info_wilayah["kab_kota"],
                    "kecamatan": info_wilayah["kecamatan"],
                    "kelurahan": info_wilayah["kelurahan"],
                    "tps": info_wilayah["tps"],
                    "jumlah_tps": info_wilayah["jumlah_tps"],
                    "partai": str(r.get("nama_partai", "-")).strip(),
                    "jumlah_suara": int(r.get("jumlah_suara", 0)),
                    "nomor_urut_partai": r.get("nomor_urut", "-"),
                })

        if not rows:
            return (
                pd.DataFrame(),
                info_wilayah,
                "Gemini merespon, tetapi tidak menemukan data tabel pada"
                " gambar.",
            )

        return pd.DataFrame(rows), info_wilayah, ""

    except Exception as e:
        return pd.DataFrame(), {}, f"Terjadi Error API Gemini: {e}"