"""
pdf_hybrid_engine.py
====================

Hybrid Gemini Multimodal Engine untuk formulir KPU DA1 / D.Hasil Kecamatan.

Tujuan:
- Membaca PDF scan/image-only per halaman.
- Gemini melihat GAMBAR halaman, bukan hanya teks OCR.
- Mengenali struktur dokumen secara dinamis.
- Tidak hardcode provinsi/kabupaten/kecamatan/desa.
- Menentukan desa, TPS, partai, suara sah/tidak sah/total.
- Mencegah double-counting dengan memisahkan sumber PARTAI dan REKAP_SUARA.
- Validasi silang: TPS, partai, subtotal, total desa.
- Hasil tetap kompatibel dengan UI Pemindai_Data:
    summary, db, ranges, tps, parties, validation, raw
- Export Excel.

Catatan:
- API key Gemini dibaca dari argumen atau GEMINI_API_KEY.
- Model dapat diganti melalui GEMINI_MODEL.
- Jika angka tidak terbaca: null, bukan tebakan.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import fitz
import pandas as pd
from PIL import Image

from village_resolver import (
    build_auto_roster,
    extract_desa_candidate_from_text,
    VillageCarryState,
)


MODEL_DEFAULT = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_DPI = 240
MAX_RETRIES = 6
MIN_CONFIDENCE = 0.70


# ============================================================
# UTILITAS
# ============================================================

def get_pdf_page_count(file_bytes: bytes) -> int:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        return len(doc)
    finally:
        doc.close()


def get_pdf_page_image(file_bytes: bytes, page_number: int, dpi: int = DEFAULT_DPI) -> Image.Image:
    """page_number 1-based."""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        page = doc.load_page(page_number - 1)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        return img
    finally:
        doc.close()


def _clean_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    # Ambil objek JSON pertama yang valid.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    return {}


def _int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return None

    s = str(value).strip().upper()

    # Jangan menerima tanda tanya/teks yang jelas tidak pasti.
    if any(x in s for x in ["?", "TIDAK TERBACA", "UNKNOWN", "NULL", "N/A"]):
        return None

    # Normalisasi OCR angka.
    s = s.replace("O", "0").replace("I", "1").replace("L", "1")

    # Hilangkan separator ribuan.
    s = re.sub(r"[.\s]", "", s)

    m = re.search(r"-?\d+", s)
    if not m:
        return None

    try:
        return int(m.group(0))
    except Exception:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_key(value: Any) -> str:
    s = _str(value).upper()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _page_text_for_resolver(data: Dict[str, Any]) -> str:
    wilayah = data.get("wilayah") or {}
    parts = [
        f"PROVINSI: {_str(wilayah.get('provinsi'))}",
        f"KABUPATEN/KOTA: {_str(wilayah.get('kab_kota'))}",
        f"KECAMATAN: {_str(wilayah.get('kecamatan'))}",
        f"KELURAHAN/DESA: {_str(wilayah.get('kelurahan'))}",
    ]
    return "\n".join(parts)


# ============================================================
# PROMPT GEMINI
# ============================================================

SYSTEM_PROMPT = r"""
Anda adalah mesin ekstraksi dokumen KPU Indonesia yang sangat ketat.

Tugas Anda adalah membaca SATU HALAMAN hasil scan formulir KPU dari GAMBAR
yang diberikan. Jangan mengarang angka. Jangan menggunakan pengetahuan
eksternal untuk mengisi angka yang tidak terlihat.

PRINSIP UTAMA:
1. Baca visual halaman: tabel, header, nomor TPS, nomor partai, angka.
2. Pertahankan angka persis seperti yang terlihat.
3. Jika sebuah angka tidak jelas/tertutup/rusak: null.
4. Jangan menjumlahkan angka sendiri untuk mengganti angka yang tidak terbaca.
5. Jika halaman adalah lanjutan dari desa sebelumnya dan nama desa tidak
   tertulis, gunakan konteks halaman sebelumnya hanya sebagai petunjuk,
   bukan sebagai bukti baru.
6. Bedakan halaman PARTAI dari halaman REKAP_SUARA.
7. Halaman tanda tangan, daftar hadir, lampiran, berita acara, dll jangan
   dianggap sebagai data suara.

FORMAT KPU BISA BERBEDA DETAILNYA, tetapi struktur berikut harus dicari:
- PROVINSI
- KABUPATEN/KOTA
- KECAMATAN
- KELURAHAN/DESA
- DAPIL
- TPS
- PARTAI
- JUMLAH SUARA
- SUARA SAH
- SUARA TIDAK SAH
- TOTAL SUARA

PAGE_ROLE:
- COVER
- IDENTITAS
- PARTAI
- REKAP_SUARA
- LAINNYA

ATURAN PARTAI:
- Masukkan data partai hanya jika halaman memang merupakan tabel partai.
- "jumlah_akhir" adalah angka akhir untuk partai tersebut pada halaman,
  bila memang tercetak.
- Jangan memakai "jumlah_akhir" dari halaman lain.
- Jangan menjumlahkan baris TPS untuk menciptakan jumlah_akhir jika angka
  jumlah akhir tercetak tetapi tidak terbaca.
- Jika halaman hanya memuat dua partai, jangan membuat partai lain.

ATURAN TPS:
- Setiap TPS harus memiliki nomor.
- Ambil suara sah, tidak sah, total jika tercetak.
- Jika total tidak tercetak tetapi suara sah dan tidak sah terlihat jelas,
  boleh beri total_suara = null. Python akan melakukan validasi/rekonsiliasi
  secara terpisah.
- Jangan menganggap nomor halaman sebagai nomor TPS.

CONFIDENCE:
- 0.95+ jika teks dan angka sangat jelas.
- 0.80-0.94 jika ada sedikit noise tetapi struktur/angka jelas.
- 0.70-0.79 jika sebagian terbaca.
- <0.70 jika meragukan.

KELUARKAN JSON SAJA.
"""

JSON_SCHEMA_EXAMPLE = r"""
{
  "page_role": "PARTAI",
  "wilayah": {
    "provinsi": null,
    "dapil": null,
    "kab_kota": null,
    "kecamatan": null,
    "kelurahan": null
  },
  "jumlah_tps": null,
  "tps": [
    {
      "no_tps": null,
      "suara_sah": null,
      "suara_tidak_sah": null,
      "total_suara": null,
      "confidence": 0.0
    }
  ],
  "partai": [
    {
      "nomor_partai": null,
      "nama_partai": null,
      "jumlah_akhir": null,
      "confidence": 0.0
    }
  ],
  "suara_sah": null,
  "suara_tidak_sah": null,
  "total_suara": null,
  "confidence": 0.0,
  "warnings": []
}
"""


def _build_prompt(known_context: Dict[str, Any]) -> str:
    context = json.dumps(known_context or {}, ensure_ascii=False, indent=2)

    return f"""
{SYSTEM_PROMPT}

KONTEKS ADMINISTRATIF DARI HALAMAN SEBELUMNYA (HANYA PETUNJUK):
{context}

ATURAN DESA/KELURAHAN:
- JANGAN menyalin nama desa dari konteks.
- Selalu cari tulisan KELURAHAN/DESA pada GAMBAR halaman ini.
- Jika terlihat nama desa baru, WAJIB gunakan nama desa baru tersebut.
- Jika nama desa tidak terlihat, kosongkan field desa; Python akan melakukan carry-forward.

SCHEMA YANG WAJIB:
{JSON_SCHEMA_EXAMPLE}

PERHATIKAN KHUSUS:
- Nama desa dapat hanya muncul di halaman pertama sebuah blok.
- Jangan membuat desa baru hanya karena typo OCR.
- Jangan membawa desa sebelumnya jika halaman jelas sudah masuk identitas/
  lampiran/topik lain.
- Jika nama desa terlihat langsung pada halaman ini, prioritaskan bacaan
  langsung tersebut.
- Nomor partai adalah nomor pada kolom partai, bukan nomor halaman.
- Jangan mengubah angka 0 menjadi null.
- Jangan mengisi angka yang tidak terlihat.
"""


# ============================================================
# GEMINI CALL
# ============================================================

def _call_gemini(
    api_key: str,
    image: Image.Image,
    known_context: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    if not api_key:
        raise ValueError("API key Gemini belum diberikan.")

    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise ImportError(
            "Package google-generativeai belum terpasang. "
            "Install: pip install google-generativeai"
        ) from exc

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(
        MODEL_DEFAULT,
        generation_config={
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "max_output_tokens": 8192,
        },
    )

    prompt = _build_prompt(known_context)
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content([prompt, image])
            raw = getattr(response, "text", "") or ""
            data = _clean_json(raw)

            if data:
                return data, raw

            last_error = RuntimeError("Gemini mengembalikan JSON kosong.")
        except Exception as exc:
            last_error = exc
            msg = str(exc).lower()

            # Backoff lebih panjang untuk rate limit/quota.
            if "429" in msg or "quota" in msg or "rate" in msg:
                delay = min(30, 2 ** attempt)
            else:
                delay = min(12, attempt * 2)

            if attempt < MAX_RETRIES:
                time.sleep(delay)

    raise RuntimeError(f"Gagal membaca halaman dengan Gemini: {last_error}")


# ============================================================
# NORMALISASI HASIL
# ============================================================

def _normalise(data: Dict[str, Any], page: int) -> Dict[str, Any]:
    data = data if isinstance(data, dict) else {}

    wilayah = data.get("wilayah")
    if not isinstance(wilayah, dict):
        wilayah = {}

    page_role = _norm_key(data.get("page_role"))
    allowed_roles = {"COVER", "IDENTITAS", "PARTAI", "REKAP_SUARA", "LAINNYA"}
    if page_role not in allowed_roles:
        page_role = "LAINNYA"

    tps_out = []
    for item in data.get("tps") or []:
        if not isinstance(item, dict):
            continue

        no_tps = _int(item.get("no_tps"))
        sah = _int(item.get("suara_sah"))
        tidak_sah = _int(item.get("suara_tidak_sah"))
        total = _int(item.get("total_suara"))

        # Hanya hitung total jika kedua komponennya benar-benar ada.
        # Ini bukan pengganti OCR; hanya normalisasi struktur.
        if total is None and sah is not None and tidak_sah is not None:
            total = sah + tidak_sah

        if no_tps is None:
            continue

        tps_out.append({
            "no_tps": no_tps,
            "suara_sah": sah,
            "suara_tidak_sah": tidak_sah,
            "total_suara": total,
            "confidence": _float(item.get("confidence")),
        })

    partai_out = []
    for item in data.get("partai") or []:
        if not isinstance(item, dict):
            continue

        nomor = _int(item.get("nomor_partai"))
        nama = _str(item.get("nama_partai"))
        jumlah = _int(item.get("jumlah_akhir"))

        # Jangan memasukkan record partai yang tidak punya nomor dan nama.
        if nomor is None and not nama:
            continue

        partai_out.append({
            "nomor_partai": nomor,
            "nama_partai": nama,
            "jumlah_akhir": jumlah,
            "confidence": _float(item.get("confidence")),
        })

    return {
        "halaman": page,
        "page_role": page_role,
        "wilayah": {
            "provinsi": _str(wilayah.get("provinsi")),
            "dapil": _str(wilayah.get("dapil")),
            "kab_kota": _str(wilayah.get("kab_kota")),
            "kecamatan": _str(wilayah.get("kecamatan")),
            "kelurahan": _str(wilayah.get("kelurahan")),
        },
        "jumlah_tps": _int(data.get("jumlah_tps")),
        "tps": tps_out,
        "partai": partai_out,
        "suara_sah": _int(data.get("suara_sah")),
        "suara_tidak_sah": _int(data.get("suara_tidak_sah")),
        "total_suara": _int(data.get("total_suara")),
        "confidence": _float(data.get("confidence")),
        "warnings": data.get("warnings") if isinstance(data.get("warnings"), list) else [],
    }


# ============================================================
# RESOLUSI WILAYAH 2 PASS
# ============================================================

def _resolve_wilayah_dua_tahap(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pass 1:
      ambil nama desa yang dibaca langsung dengan confidence cukup.

    Pass 2:
      cocokkan ke roster dan carry-forward secara berurutan.

    Penting:
      carry-forward tidak boleh dipakai untuk halaman LAINNYA/COVER
      jika struktur halaman menunjukkan topik berbeda.
    """
    direct_readings = []

    for rec in records:
        wilayah = rec.get("wilayah") or {}
        candidate = _str(wilayah.get("kelurahan"))

        # Hanya bacaan langsung dari Gemini.
        conf = _float(rec.get("confidence"))
        if candidate and conf >= 0.70:
            direct_readings.append((candidate, conf))

    roster = build_auto_roster(direct_readings)

    state = VillageCarryState(
        roster=roster,
        similarity_min=0.62,
        max_carry_pages=12,
    )

    prev_global = {
        "provinsi": "",
        "dapil": "",
        "kab_kota": "",
        "kecamatan": "",
    }

    resolved = []

    for rec in records:
        rec = dict(rec)
        wilayah = dict(rec.get("wilayah") or {})

        # Wilayah administratif tingkat atas boleh diwarisi.
        for key in ("provinsi", "dapil", "kab_kota", "kecamatan"):
            value = _str(wilayah.get(key))
            if value:
                prev_global[key] = value
            elif prev_global[key]:
                wilayah[key] = prev_global[key]

        candidate = _str(wilayah.get("kelurahan"))
        page_role = rec.get("page_role")

        # Untuk halaman non-data/lampiran, jangan memaksakan carry desa.
        if page_role in {"COVER", "LAINNYA"} and not candidate:
            resolved_info = {
                "kelurahan": "",
                "confidence": 0.0,
                "sumber": "TIDAK_DITEMUKAN",
            }
        else:
            resolved_info = state.resolve(candidate)

        wilayah["kelurahan"] = resolved_info["kelurahan"]
        rec["wilayah"] = wilayah
        rec["village_confidence"] = resolved_info["confidence"]
        rec["village_source"] = resolved_info["sumber"]

        # Confidence akhir memperhitungkan desa.
        rec["confidence_final"] = min(
            _float(rec.get("confidence")),
            _float(resolved_info["confidence"], 0.0)
            if resolved_info["kelurahan"]
            else 0.0
        )

        resolved.append(rec)

    return resolved


# ============================================================
# BANGUN DATA TPS
# ============================================================

def _build_tps_rows(records: List[Dict[str, Any]], filename: str) -> List[Dict[str, Any]]:
    """
    TPS hanya diambil dari halaman REKAP_SUARA.
    Ini penting agar angka TPS tidak terduplikasi dari halaman PARTAI.
    """
    rows = []

    for rec in records:
        if rec.get("page_role") != "REKAP_SUARA":
            continue

        wilayah = rec.get("wilayah") or {}
        desa = _str(wilayah.get("kelurahan"))
        if not desa:
            continue

        for t in rec.get("tps") or []:
            no_tps = _int(t.get("no_tps"))
            if no_tps is None:
                continue

            sah = _int(t.get("suara_sah"))
            tidak = _int(t.get("suara_tidak_sah"))
            total = _int(t.get("total_suara"))

            rows.append({
                "file": filename,
                "halaman": rec.get("halaman"),
                "provinsi": _str(wilayah.get("provinsi")),
                "dapil": _str(wilayah.get("dapil")),
                "kab_kota": _str(wilayah.get("kab_kota")),
                "kecamatan": _str(wilayah.get("kecamatan")),
                "kelurahan": desa,
                "no_tps": no_tps,
                "suara_sah": sah,
                "suara_tidak_sah": tidak,
                "total_suara": total,
                "confidence": _float(t.get("confidence")),
            })

    return rows


# ============================================================
# BANGUN DATA PARTAI
# ============================================================

def _build_party_rows(records: List[Dict[str, Any]], filename: str) -> List[Dict[str, Any]]:
    """
    Party totals hanya berasal dari halaman PARTAI.

    Jika halaman PARTAI berisi beberapa TPS + JUMLAH AKHIR,
    yang digunakan untuk rekap desa adalah JUMLAH AKHIR.
    Jadi halaman TPS/REKAP_SUARA tidak dihitung lagi sebagai suara partai.
    """
    rows = []

    for rec in records:
        if rec.get("page_role") != "PARTAI":
            continue

        wilayah = rec.get("wilayah") or {}
        desa = _str(wilayah.get("kelurahan"))
        if not desa:
            continue

        for p in rec.get("partai") or []:
            nomor = _int(p.get("nomor_partai"))
            nama = _str(p.get("nama_partai"))
            jumlah = _int(p.get("jumlah_akhir"))

            # Record tanpa jumlah tidak boleh dianggap 0.
            if jumlah is None:
                continue

            rows.append({
                "file": filename,
                "halaman": rec.get("halaman"),
                "provinsi": _str(wilayah.get("provinsi")),
                "dapil": _str(wilayah.get("dapil")),
                "kab_kota": _str(wilayah.get("kab_kota")),
                "kecamatan": _str(wilayah.get("kecamatan")),
                "kelurahan": desa,
                "nomor_partai": nomor,
                "nama_partai": nama,
                "jumlah_suara": jumlah,
                "confidence": _float(p.get("confidence")),
            })

    return rows


# ============================================================
# RANGE DESA
# ============================================================

def _ranges(records: List[Dict[str, Any]], filename: str) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)

    for rec in records:
        wilayah = rec.get("wilayah") or {}
        desa = _str(wilayah.get("kelurahan"))

        if not desa:
            continue

        # Hanya halaman data yang relevan untuk range desa.
        if rec.get("page_role") not in {"PARTAI", "REKAP_SUARA"}:
            continue

        key = (
            _norm_key(wilayah.get("provinsi")),
            _norm_key(wilayah.get("dapil")),
            _norm_key(wilayah.get("kab_kota")),
            _norm_key(wilayah.get("kecamatan")),
            _norm_key(desa),
        )
        grouped[key].append(rec)

    output = []

    for key, items in grouped.items():
        items = sorted(items, key=lambda x: int(x.get("halaman") or 0))

        # Pecah jika ada gap besar; ini mencegah satu desa "menelan"
        # lampiran/desa lain.
        chunks = []
        current = []

        for rec in items:
            page = int(rec.get("halaman") or 0)

            if current:
                prev = int(current[-1].get("halaman") or 0)
                if page - prev > 2:
                    chunks.append(current)
                    current = []

            current.append(rec)

        if current:
            chunks.append(current)

        for chunk in chunks:
            first = chunk[0]
            last = chunk[-1]
            wilayah = first.get("wilayah") or {}

            confs = [
                _float(x.get("confidence_final"))
                for x in chunk
                if x.get("confidence_final") is not None
            ]
            conf = min(confs) if confs else 0.0

            output.append({
                "file": filename,
                "provinsi": _str(wilayah.get("provinsi")),
                "dapil": _str(wilayah.get("dapil")),
                "kab_kota": _str(wilayah.get("kab_kota")),
                "kecamatan": _str(wilayah.get("kecamatan")),
                "kelurahan": _str(wilayah.get("kelurahan")),
                "halaman_mulai": int(first.get("halaman") or 0),
                "halaman_selesai": int(last.get("halaman") or 0),
                "jumlah_halaman": len(chunk),
                "confidence": round(conf, 3),
                "status": "OK" if conf >= MIN_CONFIDENCE else "⚠ PERLU DICEK",
            })

    return sorted(output, key=lambda x: x["halaman_mulai"])


# ============================================================
# VALIDASI
# ============================================================

def _validate(
    records: List[Dict[str, Any]],
    tps_rows: List[Dict[str, Any]],
    party_rows: List[Dict[str, Any]],
    filename: str,
) -> List[Dict[str, Any]]:
    checks = []

    def add(level, desa, jenis, status, detail, halaman=None):
        checks.append({
            "file": filename,
            "halaman": halaman,
            "kelurahan": desa,
            "jenis_validasi": jenis,
            "status": status,
            "detail": detail,
        })

    # --------------------------------------------------------
    # 1. Validasi per TPS
    # --------------------------------------------------------
    seen_tps = set()

    for row in tps_rows:
        desa = row["kelurahan"]
        no = row["no_tps"]
        key = (_norm_key(desa), no)

        if key in seen_tps:
            add(
                "ERROR", desa, "DUPLIKAT_TPS", "GAGAL",
                f"TPS {no:03d} muncul lebih dari sekali.",
                row["halaman"],
            )
        else:
            seen_tps.add(key)

        sah = row.get("suara_sah")
        tidak = row.get("suara_tidak_sah")
        total = row.get("total_suara")

        if sah is not None and tidak is not None and total is not None:
            if sah + tidak == total:
                add("INFO", desa, "SAH_PLUS_TIDAK_SAH", "OK",
                    f"TPS {no:03d}: {sah}+{tidak}={total}.", row["halaman"])
            else:
                add("ERROR", desa, "SAH_PLUS_TIDAK_SAH", "GAGAL",
                    f"TPS {no:03d}: {sah}+{tidak}!={total}.", row["halaman"])

        if row.get("confidence", 0) < MIN_CONFIDENCE:
            add("WARNING", desa, "CONFIDENCE_TPS", "PERLU_DICEK",
                f"Confidence TPS {no:03d} rendah: {row.get('confidence')}.",
                row["halaman"])

    # --------------------------------------------------------
    # 2. Agregasi TPS per desa
    # --------------------------------------------------------
    tps_by_village = defaultdict(list)
    for row in tps_rows:
        tps_by_village[_norm_key(row["kelurahan"])].append(row)

    # --------------------------------------------------------
    # 3. Agregasi partai per desa
    # --------------------------------------------------------
    party_by_village = defaultdict(list)
    for row in party_rows:
        party_by_village[_norm_key(row["kelurahan"])].append(row)

    # --------------------------------------------------------
    # 4. Cari total desa dari halaman REKAP_SUARA
    # --------------------------------------------------------
    recap_by_village = defaultdict(list)
    for rec in records:
        if rec.get("page_role") != "REKAP_SUARA":
            continue
        desa = _norm_key((rec.get("wilayah") or {}).get("kelurahan"))
        if desa:
            recap_by_village[desa].append(rec)

    all_villages = set(tps_by_village) | set(party_by_village) | set(recap_by_village)

    for desa_key in sorted(all_villages):
        desa_display = desa_key

        tps = tps_by_village.get(desa_key, [])
        parties = party_by_village.get(desa_key, [])
        recaps = recap_by_village.get(desa_key, [])

        # Ambil total desa yang tercetak pada recap.
        recap_sah = None
        recap_tidak = None
        recap_total = None
        recap_page = None

        for rec in recaps:
            if rec.get("suara_sah") is not None:
                recap_sah = rec.get("suara_sah")
            if rec.get("suara_tidak_sah") is not None:
                recap_tidak = rec.get("suara_tidak_sah")
            if rec.get("total_suara") is not None:
                recap_total = rec.get("total_suara")
            recap_page = rec.get("halaman")

        # Sum TPS.
        sum_sah = sum(
            r["suara_sah"] for r in tps if r.get("suara_sah") is not None
        )
        sum_tidak = sum(
            r["suara_tidak_sah"] for r in tps if r.get("suara_tidak_sah") is not None
        )
        sum_total = sum(
            r["total_suara"] for r in tps if r.get("total_suara") is not None
        )

        known_sah = sum(1 for r in tps if r.get("suara_sah") is not None)
        known_tidak = sum(1 for r in tps if r.get("suara_tidak_sah") is not None)
        known_total = sum(1 for r in tps if r.get("total_suara") is not None)

        # Jangan mengklaim sama jika ada TPS yang belum terbaca.
        if tps and known_sah == len(tps) and recap_sah is not None:
            status = "OK" if sum_sah == recap_sah else "GAGAL"
            add(
                "INFO" if status == "OK" else "ERROR",
                desa_display,
                "SUM_TPS_SAH_VS_REKAP",
                status,
                f"Σ TPS sah={sum_sah}, rekap={recap_sah}.",
                recap_page,
            )

        if tps and known_tidak == len(tps) and recap_tidak is not None:
            status = "OK" if sum_tidak == recap_tidak else "GAGAL"
            add(
                "INFO" if status == "OK" else "ERROR",
                desa_display,
                "SUM_TPS_TIDAK_SAH_VS_REKAP",
                status,
                f"Σ TPS tidak sah={sum_tidak}, rekap={recap_tidak}.",
                recap_page,
            )

        if tps and known_total == len(tps) and recap_total is not None:
            status = "OK" if sum_total == recap_total else "GAGAL"
            add(
                "INFO" if status == "OK" else "ERROR",
                desa_display,
                "SUM_TPS_TOTAL_VS_REKAP",
                status,
                f"Σ TPS total={sum_total}, rekap={recap_total}.",
                recap_page,
            )

        # ----------------------------------------------------
        # 5. SUM PARTAI vs SUARA SAH
        # ----------------------------------------------------
        # Partai dapat muncul di beberapa halaman, tetapi nomor partai
        # yang sama hanya boleh satu kali per desa.
        party_map = {}
        duplicate_party = set()

        for p in parties:
            key = p.get("nomor_partai")
            if key is None:
                key = _norm_key(p.get("nama_partai"))

            if key in party_map:
                duplicate_party.add(key)
            else:
                party_map[key] = p

        if duplicate_party:
            add(
                "ERROR",
                desa_display,
                "DUPLIKAT_PARTAI",
                "GAGAL",
                f"Partai terdeteksi ganda: {sorted(map(str, duplicate_party))}.",
            )

        known_party_values = [
            p.get("jumlah_suara")
            for p in party_map.values()
            if p.get("jumlah_suara") is not None
        ]

        if known_party_values and recap_sah is not None and len(known_party_values) == len(party_map):
            sum_party = sum(known_party_values)
            status = "OK" if sum_party == recap_sah else "GAGAL"
            add(
                "INFO" if status == "OK" else "ERROR",
                desa_display,
                "SUM_PARTAI_VS_SUARA_SAH",
                status,
                f"Σ partai={sum_party}, suara sah rekap={recap_sah}.",
                recap_page,
            )
        elif known_party_values and recap_sah is None:
            add(
                "WARNING",
                desa_display,
                "SUM_PARTAI_VS_SUARA_SAH",
                "PERLU_DICEK",
                "Suara sah rekap belum tersedia sehingga belum dapat dibandingkan.",
            )

        # ----------------------------------------------------
        # 6. Confidence desa/halaman
        # ----------------------------------------------------
        relevant = [
            r for r in records
            if _norm_key((r.get("wilayah") or {}).get("kelurahan")) == desa_key
        ]

        for rec in relevant:
            if _float(rec.get("confidence_final")) < MIN_CONFIDENCE:
                add(
                    "WARNING",
                    desa_display,
                    "CONFIDENCE_HALAMAN",
                    "PERLU_DICEK",
                    f"Halaman {rec.get('halaman')} confidence rendah.",
                    rec.get("halaman"),
                )

            if rec.get("village_source") == "LANGSUNG_TIDAK_DIKENALI":
                add(
                    "WARNING",
                    desa_display,
                    "RESOLUSI_DESA",
                    "PERLU_DICEK",
                    "Nama desa dibaca langsung tetapi tidak cocok dengan roster.",
                    rec.get("halaman"),
                )

    return checks


# ============================================================
# DB ROWS
# ============================================================

def _build_db_rows(
    tps_rows: List[Dict[str, Any]],
    party_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows = []

    for r in tps_rows:
        rows.append({
            **r,
            "jenis_data": "TPS",
        })

    for r in party_rows:
        rows.append({
            **r,
            "jenis_data": "PARTAI",
        })

    return rows


# ============================================================
# SUMMARY
# ============================================================

def _summary(
    records: List[Dict[str, Any]],
    tps_rows: List[Dict[str, Any]],
    party_rows: List[Dict[str, Any]],
    validation_rows: List[Dict[str, Any]],
    filename: str,
) -> Dict[str, Any]:
    villages = sorted({
        _str((r.get("wilayah") or {}).get("kelurahan"))
        for r in records
        if _str((r.get("wilayah") or {}).get("kelurahan"))
    })

    parties = sorted({
        (
            r.get("nomor_partai"),
            _str(r.get("nama_partai")),
        )
        for r in party_rows
    })

    need_check = sum(
        1 for x in validation_rows
        if x.get("status") in {"GAGAL", "PERLU_DICEK"}
    )

    return {
        "file": filename,
        "jumlah_halaman": len(records),
        "jumlah_kelurahan": len(villages),
        "jumlah_tps": len(tps_rows),
        "jumlah_partai": len(parties),
        "halaman_perlu_dicek": sum(
            1 for r in records
            if _float(r.get("confidence_final")) < MIN_CONFIDENCE
        ),
        "validasi_perlu_dicek": need_check,
        "desa": villages,
        "status": "OK" if need_check == 0 else "⚠ PERLU DICEK",
    }


# ============================================================
# MAIN
# ============================================================

def proses_pdf_batch_hybrid(
    file_bytes: bytes,
    nama_file: str,
    api_key: Optional[str] = None,
    progress_callback=None,
    dpi: int = DEFAULT_DPI,
) -> Dict[str, Any]:
    """
    Fungsi utama yang dipanggil Pemindai_Data.py.

    Return:
    {
      summary: {...},
      db: [...],
      ranges: [...],
      tps: [...],
      parties: [...],
      validation: [...],
      raw: [...]
    }
    """
    api_key = api_key or os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise ValueError("GEMINI_API_KEY belum tersedia.")

    page_count = get_pdf_page_count(file_bytes)
    records = []
    known_context = {}

    for page in range(1, page_count + 1):
        try:
            image = get_pdf_page_image(file_bytes, page, dpi=dpi)
            data, raw = _call_gemini(api_key, image, known_context)
            rec = _normalise(data, page)
            rec["_raw_gemini"] = raw
            rec["_error"] = None
        except Exception as exc:
            # Jangan hentikan seluruh PDF hanya karena satu halaman gagal.
            rec = _normalise({}, page)
            rec["_raw_gemini"] = ""
            rec["_error"] = str(exc)
            rec["warnings"] = [f"Halaman gagal diproses: {exc}"]

        records.append(rec)

        # Konteks halaman berikutnya hanya mengambil informasi yang benar-benar
        # terbaca pada halaman ini.
        wilayah = rec.get("wilayah") or {}
        known_context = {
            "provinsi": wilayah.get("provinsi") or known_context.get("provinsi"),
            "dapil": wilayah.get("dapil") or known_context.get("dapil"),
            "kab_kota": wilayah.get("kab_kota") or known_context.get("kab_kota"),
            "kecamatan": wilayah.get("kecamatan") or known_context.get("kecamatan"),
            "kelurahan": wilayah.get("kelurahan") or known_context.get("kelurahan"),
            "page_role": rec.get("page_role"),
        }

        if progress_callback:
            try:
                progress_callback(page, page_count, nama_file)
            except TypeError:
                try:
                    progress_callback(page / max(1, page_count))
                except Exception:
                    pass
            except Exception:
                pass

    # Dua pass desa.
    records = _resolve_wilayah_dua_tahap(records)

    # Data final.
    tps_rows = _build_tps_rows(records, nama_file)
    party_rows = _build_party_rows(records, nama_file)

    validation_rows = _validate(
        records,
        tps_rows,
        party_rows,
        nama_file,
    )

    ranges = _ranges(records, nama_file)

    db_rows = _build_db_rows(tps_rows, party_rows)

    summary = _summary(
        records,
        tps_rows,
        party_rows,
        validation_rows,
        nama_file,
    )

    # Raw dibuat mudah diekspor ke Excel.
    raw_rows = []
    for rec in records:
        wilayah = rec.get("wilayah") or {}
        raw_rows.append({
            "file": nama_file,
            "halaman": rec.get("halaman"),
            "page_role": rec.get("page_role"),
            "provinsi": wilayah.get("provinsi"),
            "dapil": wilayah.get("dapil"),
            "kab_kota": wilayah.get("kab_kota"),
            "kecamatan": wilayah.get("kecamatan"),
            "kelurahan": wilayah.get("kelurahan"),
            "village_source": rec.get("village_source"),
            "confidence": rec.get("confidence"),
            "confidence_final": rec.get("confidence_final"),
            "jumlah_tps": rec.get("jumlah_tps"),
            "suara_sah": rec.get("suara_sah"),
            "suara_tidak_sah": rec.get("suara_tidak_sah"),
            "total_suara": rec.get("total_suara"),
            "warnings": json.dumps(rec.get("warnings") or [], ensure_ascii=False),
            "error": rec.get("_error"),
            "gemini_json": rec.get("_raw_gemini", ""),
        })

    return {
        "summary": summary,
        "db": db_rows,
        "ranges": ranges,
        "tps": tps_rows,
        "parties": party_rows,
        "validation": validation_rows,
        "raw": raw_rows,
        "records": records,
    }


# ============================================================
# EXCEL
# ============================================================

def export_hasil_excel(
    hasil: Dict[str, Any],
    output_path: Optional[str] = None,
) -> bytes:
    """
    Jika output_path diberikan -> simpan file.
    Selalu mengembalikan bytes XLSX agar bisa dipakai Streamlit download_button.
    """
    output = io.BytesIO()

    summary = hasil.get("summary") or {}
    ranges = hasil.get("ranges") or []
    tps = hasil.get("tps") or []
    parties = hasil.get("parties") or []
    validation = hasil.get("validation") or []
    raw = hasil.get("raw") or []

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([summary]).to_excel(
            writer, sheet_name="01_RINGKASAN", index=False
        )

        pd.DataFrame(ranges).to_excel(
            writer, sheet_name="02_RANGE_KELURAHAN", index=False
        )

        pd.DataFrame(tps).to_excel(
            writer, sheet_name="03_DATA_TPS", index=False
        )

        # Rekap partai per desa.
        party_df = pd.DataFrame(parties)

        if not party_df.empty:
            group_cols = [
                "file",
                "provinsi",
                "dapil",
                "kab_kota",
                "kecamatan",
                "kelurahan",
                "nomor_partai",
                "nama_partai",
            ]

            rekap_partai = (
                party_df
                .groupby(group_cols, dropna=False, as_index=False)["jumlah_suara"]
                .sum()
            )
        else:
            rekap_partai = pd.DataFrame()

        rekap_partai.to_excel(
            writer, sheet_name="04_REKAP_PARTAI", index=False
        )

        pd.DataFrame(validation).to_excel(
            writer, sheet_name="05_VALIDASI", index=False
        )

        pd.DataFrame(raw).to_excel(
            writer, sheet_name="06_OCR_RAW", index=False
        )

    data = output.getvalue()

    if output_path:
        with open(output_path, "wb") as f:
            f.write(data)

    return data


# ============================================================
# ALIAS BACKWARD COMPATIBILITY
# ============================================================

process_pdf_hybrid = proses_pdf_batch_hybrid
