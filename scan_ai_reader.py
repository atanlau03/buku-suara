"""
Pembaca PDF scan lewat AI vision (jalur terakhir, setelah text layer dan OCR).
Semua hasil bertanda PERLU DICEK; hitungan matematika jadi penentu.
"""
import hashlib
import io
import json
from pathlib import Path

import fitz
import pandas as pd
from PIL import Image

import ai_vision_fallback as ai
import local_ocr_engine as engine

CACHE_DIR = Path(__file__).resolve().parent / "ai_cache"
CACHE_DIR.mkdir(exist_ok=True)

PROMPT = """Ini satu halaman formulir rekapitulasi Pemilu Indonesia (Model D.HASIL KECAMATAN-DPR), hasil scan, mungkin miring.
Tentukan jenis halaman dan baca angkanya. Semua angka diambil dari kolom "JUMLAH AKHIR" (paling kanan).

Jenis:
- "partai": tabel per partai (baris A.1 nama partai, A.2 daftar caleg, baris B "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON").
- "rekap_desa": rekap suara sah / tidak sah / total untuk satu desa.
- "lainnya": halaman lain (sampul, DPT, dsb).

Balas JSON persis:
{"jenis": "...", "desa": "<nama KELURAHAN/DESA di header atau null>",
 "partai": [{"nomor": <1-18>, "a1": <angka|null>, "caleg": [<angka per caleg>], "b": <angka baris B|null>}],
 "sah": <angka|null>, "tidak_sah": <angka|null>, "total": <angka|null>}

ATURAN:
- "nomor" HARUS angka, bukan nama partai.
- "b" diambil dari baris berlabel "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON", BUKAN dari baris caleg.
- Halaman miring: cocokkan angka dengan baris yang sejajar secara visual.
- Kalau ragu, isi null. JANGAN menebak. Untuk jenis selain "partai" kosongkan "partai": []."""


def _cache_file(pdf_bytes):
    return CACHE_DIR / (hashlib.md5(pdf_bytes).hexdigest() + ".json")


def _read_page(doc, i, dpi):
    pix = doc[i].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    teks = ai._call_gemini(
        ai._image_to_jpeg_bytes(img, max_dimension=2400),
        PROMPT,
        max_output_tokens=6000,
    )
    data = ai._extract_json(teks)
    return data if isinstance(data, dict) else None


def _cocok(p):
    a1, caleg, b = p.get("a1"), p.get("caleg") or [], p.get("b")
    if a1 is None or b is None or any(v is None for v in caleg):
        return None
    return a1 + sum(caleg) == b


def read_scan_pdf(pdf_bytes, file_name="scan.pdf", dpi=200, max_pages=300,
                  log=print, progress=None):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = min(len(doc), max_pages)
    cache_path = _cache_file(pdf_bytes)
    cache = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}

    gagal_beruntun = 0

    for i in range(total):
        key = str(i)
        if key not in cache:
            try:
                cache[key] = _read_page(doc, i, dpi)
                gagal_beruntun = 0
            except Exception as e:
                gagal_beruntun += 1
                log(f"      ! AI hal {i+1} gagal: {type(e).__name__}: {str(e)[:120]}")
                cache[key] = None
                if gagal_beruntun >= 5:
                    raise RuntimeError("AI gagal 5 halaman berturut-turut (kuota/model?)")
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        if progress:
            progress((i + 1) / total, f"AI membaca halaman {i+1}/{total}")

    doc.close()

    # --- gabungkan per desa ---
    partai = {}      # (desa, nomor) -> entri terbaik
    rekap = {}       # desa -> {sah, tidak_sah, total, halaman}
    halaman_desa = {}

    for i in range(total):
        d = cache.get(str(i))
        if not d:
            continue
        desa = (d.get("desa") or "").strip().upper() or None
        if not desa:
            continue
        halaman_desa.setdefault(desa, []).append(i + 1)

        for p in d.get("partai") or []:
            nomor = ai._safe_int(p.get("nomor"))
            if nomor not in engine.PARTY_NAMES:
                continue
            p["halaman"] = i + 1
            lama = partai.get((desa, nomor))
            if lama is None or (_cocok(p) and not _cocok(lama)) or (lama.get("b") is None and p.get("b") is not None):
                partai[(desa, nomor)] = p

        if d.get("jenis") == "rekap_desa":
            rekap[desa] = {k: ai._safe_int(d.get(k)) for k in ("sah", "tidak_sah", "total")}

    p_rows, r_rows, v_rows, db_rows = [], [], [], []

    for desa in sorted(halaman_desa):
        jumlah_partai = 0
        for nomor in sorted(engine.PARTY_NAMES):
            p = partai.get((desa, nomor))
            if not p:
                continue
            suara = ai._safe_int(p.get("b"))
            cocok = _cocok(p)
            if suara is None:
                continue
            jumlah_partai += suara
            p_rows.append({
                "kelurahan": desa, "no_partai": nomor, "partai": nomor,
                "nama_partai": engine.PARTY_NAMES[nomor],
                "suara_akhir_partai": suara, "halaman": p["halaman"],
                "status_validasi": "PERLU DICEK",
                "cocok_hitungan_partai": cocok, "sumber": "AI",
                "provinsi": None, "dapil": None, "kab_kota": None, "kecamatan": None,
            })
            db_rows.append({
                "provinsi": "-", "dapil": "-", "kab_kota": "-", "kecamatan": "-",
                "kelurahan": desa, "tps": "REKAP DESA",
                "partai": engine.PARTY_NAMES[nomor], "nomor_urut_partai": nomor,
                "jumlah_suara": suara,
            })

        rk = rekap.get(desa, {})
        sah, tdk, tot = rk.get("sah"), rk.get("tidak_sah"), rk.get("total")
        h = halaman_desa[desa]
        r_rows.append({
            "kelurahan": desa, "halaman": f"{min(h)}-{max(h)}", "total_tps": 0,
            "suara_sah": sah, "suara_tidak_sah": tdk, "total_suara": tot,
        })
        hitung = (sah + tdk) if sah is not None and tdk is not None else None
        ok = (
            sah is not None and tot is not None and hitung == tot
            and jumlah_partai == sah
        )
        v_rows.append({
            "kelurahan": desa, "suara_sah": sah, "suara_tidak_sah": tdk,
            "total_suara": tot, "hasil_perhitungan": hitung,
            "jumlah_suara_partai": jumlah_partai,
            "status": "OK" if ok else "PERLU DICEK",
        })

    parties_df = pd.DataFrame(p_rows)
    return {
        "summary": pd.DataFrame([{
            "file_name": file_name, "jumlah_halaman": len(doc) if False else total,
            "jumlah_kelurahan": len(r_rows), "jumlah_tps": 0,
            "jumlah_partai": parties_df["no_partai"].nunique() if len(parties_df) else 0,
            "perlu_review": sum(1 for v in v_rows if v["status"] != "OK"),
        }]),
        "db": pd.DataFrame(db_rows),
        "ranges": pd.DataFrame(r_rows),
        "tps": pd.DataFrame(),
        "parties": parties_df,
        "validation": pd.DataFrame(v_rows),
        "raw": pd.DataFrame(),
    }