"""
Local OCR Engine Buku Suara

Alur pembacaan:

1. Membaca PDF.
2. Mencari halaman akhir setiap kelurahan/desa.
3. Mengambil data TPS dari halaman akhir desa.
4. Menentukan rentang halaman desa berdasarkan halaman akhir sebelumnya.
5. Membaca seluruh rentang halaman desa untuk mencari suara akhir setiap partai.
6. Satu partai dapat berlanjut ke lebih dari satu halaman.
7. Suara akhir partai diprioritaskan dari bagian JUMLAH AKHIR.
8. Menghasilkan DataFrame yang siap dipakai oleh Pemindai Data,
   Dashboard, dan Database.

Tidak menggunakan Gemini atau AI eksternal.

PRINSIP PENTING:
- Logic lama dipertahankan.
- Hasil lama yang sudah masuk akal tidak diganti sembarangan.
- Text Layer hanya dipakai jika terlihat relevan secara struktur.
- OCR menjadi fallback ketika Text Layer kosong/tidak berguna.
- Validasi angka tetap menggunakan hubungan:
      suara_sah + suara_tidak_sah = total_suara
- JUMLAH AKHIR diprioritaskan.
- JUMLAH PINDAHAN tidak dianggap sebagai hasil akhir.

PERBAIKAN POSISIONAL:
Untuk desa dengan jumlah TPS banyak, tabel SUARA SAH /
SUARA TIDAK SAH / TOTAL dapat dicetak dalam 2 blok kolom.
OCR linear dapat mengacak urutan angka.

Untuk itu tersedia:
    extract_closing_votes_positional()

Fungsi tersebut menggunakan pytesseract.image_to_data()
untuk mendapatkan posisi X/Y kata dan mengambil angka dari
kolom paling kanan pada baris terkait.

Fallback positional hanya digunakan bila hasil parser utama
belum lengkap.
"""

import io
import os
import re
from collections import defaultdict

import fitz
import pandas as pd
import pytesseract

from PIL import Image, ImageOps, ImageEnhance


# ============================================================
# KONFIGURASI
# ============================================================

DEFAULT_DPI = 300

PARTY_NAMES = {
    1: "Partai Kebangkitan Bangsa",
    2: "Partai Gerakan Indonesia Raya",
    3: "Partai Demokrasi Indonesia Perjuangan",
    4: "Partai Golongan Karya",
    5: "Partai NasDem",
    6: "Partai Buruh",
    7: "Partai Gelombang Rakyat Indonesia",
    8: "Partai Keadilan Sejahtera",
    9: "Partai Kebangkitan Nusantara",
    10: "Partai Hati Nurani Rakyat",
    11: "Partai Garda Republik Indonesia",
    12: "Partai Amanat Nasional",
    13: "Partai Bulan Bintang",
    14: "Partai Demokrat",
    15: "Partai Solidaritas Indonesia",
    16: "Partai Persatuan Indonesia",
    17: "Partai Persatuan Pembangunan",
    18: "Partai Ummat",
}


# ============================================================
# TESSERACT
# ============================================================

def get_tesseract():
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"
        ),
    ]

    for path in candidates:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return path

    return None


def get_ocr_language():
    try:
        langs = pytesseract.get_languages(config="")

        if "ind" in langs:
            return "ind"

        if "eng" in langs:
            return "eng"

    except Exception:
        pass

    return "eng"


TESSERACT_PATH = get_tesseract()
OCR_LANGUAGE = get_ocr_language()


# ============================================================
# PDF
# ============================================================

def open_pdf(file_bytes):
    return fitz.open(
        stream=file_bytes,
        filetype="pdf"
    )


def render_page(doc, page_index, dpi=300):
    page = doc.load_page(page_index)

    scale = dpi / 72.0

    matrix = fitz.Matrix(
        scale,
        scale
    )

    pix = page.get_pixmap(
        matrix=matrix,
        alpha=False
    )

    image = Image.open(
        io.BytesIO(
            pix.tobytes("png")
        )
    ).convert("RGB")

    return image


# ============================================================
# OCR PREPROCESSING
# ============================================================

def preprocess_image(image):
    """
    Preprocessing dasar yang sudah dipakai sistem lama.

    Sengaja tidak dibuat terlalu agresif karena dokumen KPU
    mempunyai garis tabel, kotak, angka, dan teks kecil yang
    dapat rusak jika threshold terlalu ekstrem.
    """

    gray = ImageOps.grayscale(image)

    gray = ImageEnhance.Contrast(
        gray
    ).enhance(1.7)

    return gray


# ============================================================
# OCR IMAGE
# ============================================================

def ocr_image(
    image,
    psm=6
):
    image = preprocess_image(image)

    config = (
        f"--oem 3 --psm {psm}"
    )

    try:
        text = pytesseract.image_to_string(
            image,
            lang=OCR_LANGUAGE,
            config=config
        )

        return text or ""

    except Exception:
        return ""


# ============================================================
# TEXT LAYER VALIDATION
# ============================================================

def text_layer_quality(text):
    """
    Menilai apakah Text Layer PDF terlihat seperti teks KPU
    yang berguna.

    PENTING:
    Fungsi ini bukan penentu benar/salah isi data.

    Tujuannya hanya menjawab:
        "Apakah Text Layer ini layak dipakai sebagai sumber
         teks awal, atau kemungkinan besar hanya garbage?"

    Kita sengaja menggunakan kriteria konservatif.
    """

    if not text:
        return 0

    normalized = normalize_upper(text)

    if not normalized:
        return 0

    score = 0

    # Panjang teks
    if len(normalized) >= 100:
        score += 1

    if len(normalized) >= 300:
        score += 1

    # Struktur umum formulir KPU
    structural_keywords = [
        "TPS",
        "PARTAI",
        "SUARA",
        "KELURAHAN",
        "DESA",
        "KECAMATAN",
        "PROVINSI",
        "JUMLAH",
    ]

    found_keywords = 0

    for keyword in structural_keywords:
        if keyword in normalized:
            found_keywords += 1

    if found_keywords >= 2:
        score += 2

    if found_keywords >= 4:
        score += 1

    # Struktur yang sangat khas formulir rekap
    if "JUMLAH AKHIR" in normalized:
        score += 2

    if "SUARA SAH" in normalized:
        score += 1

    if "TIDAK SAH" in normalized:
        score += 1

    if re.search(r"\bTPS\s*0*\d{1,3}\b", normalized):
        score += 1

    return score


def text_layer_is_usable(text):
    """
    Text Layer dianggap cukup berguna jika:
    - panjangnya memadai DAN
    - memiliki struktur yang masuk akal.

    Ini sengaja tidak menggantikan hasil OCR lama secara agresif.
    """

    if not text:
        return False

    if len(text.strip()) < 100:
        return False

    return text_layer_quality(text) >= 3


# ============================================================
# OCR PAGE
# ============================================================

def ocr_page(
    doc,
    page_index,
    dpi=300
):
    """
    Membaca satu halaman.

    Urutan:
    1. Ambil Text Layer.
    2. Jika Text Layer cukup baik -> gunakan Text Layer.
    3. Jika Text Layer kosong/meragukan -> OCR image.
    4. Untuk OCR, tetap menggunakan PSM 6 dan 11 seperti sistem lama.
    5. Hasil OCR terpanjang dipakai, seperti behavior lama.

    Prinsip penting:
    Text Layer yang sudah terlihat valid tidak diganti hanya karena
    OCR menghasilkan teks lebih panjang.
    """

    page = doc.load_page(
        page_index
    )

    text_layer = page.get_text(
        "text"
    ) or ""

    text_layer = text_layer.strip()

    # --------------------------------------------------------
    # PERTAHANKAN TEXT LAYER YANG SUDAH BAGUS
    # --------------------------------------------------------

    if text_layer_is_usable(
        text_layer
    ):
        return text_layer

    # --------------------------------------------------------
    # Jika Tesseract tidak tersedia,
    # kembalikan Text Layer apa adanya.
    # --------------------------------------------------------

    if TESSERACT_PATH is None:
        return text_layer

    # --------------------------------------------------------
    # OCR fallback
    # --------------------------------------------------------

    image = render_page(
        doc,
        page_index,
        dpi=dpi
    )

    texts = []

    for psm in (6, 11):

        txt = ocr_image(
            image,
            psm=psm
        )

        if txt:
            texts.append(
                txt
            )

    if not texts:
        return text_layer

    ocr_text = max(
        texts,
        key=len
    )

    # --------------------------------------------------------
    # Jika Text Layer ada tetapi pendek/meragukan, bandingkan
    # secara sederhana.
    #
    # Jangan mengganti Text Layer yang secara struktur sudah
    # cukup baik.
    # --------------------------------------------------------

    if text_layer:
        layer_score = text_layer_quality(
            text_layer
        )

        ocr_score = text_layer_quality(
            ocr_text
        )

        if (
            layer_score >= 3
            and
            layer_score >= ocr_score
        ):
            return text_layer

    return ocr_text


# ============================================================
# CALLBACK PROGRESS
# ============================================================

def report_progress(
    callback,
    value,
    message=""
):
    if callback is None:
        return

    try:
        callback(
            float(value),
            str(message or "")
        )

        return

    except TypeError:
        pass

    except Exception:
        return

    try:
        callback(
            float(value)
        )

    except Exception:
        pass


# ============================================================
# NORMALISASI TEXT
# ============================================================

def normalize_text(text):
    if not text:
        return ""

    text = text.replace(
        "\x00",
        " "
    )

    text = text.replace(
        "\r",
        "\n"
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def normalize_upper(text):
    return normalize_text(
        text
    ).upper()


# ============================================================
# ANGKA
# ============================================================

def clean_number(value):
    if value is None:
        return None

    value = str(
        value
    ).strip()

    value = value.replace(
        ".",
        ""
    )

    value = value.replace(
        ",",
        ""
    )

    value = re.sub(
        r"[^0-9]",
        "",
        value
    )

    if not value:
        return None

    try:
        return int(value)

    except Exception:
        return None


def numbers_from_line(line):
    if not line:
        return []

    found = re.findall(
        r"(?<![A-Za-z0-9])(?:\d{1,3}(?:\.\d{3})+|\d+)(?![A-Za-z0-9])",
        line
    )

    result = []

    for value in found:
        number = clean_number(
            value
        )

        if number is not None:
            result.append(
                number
            )

    return result


# ============================================================
# DETEKSI TPS
# ============================================================

def extract_tps_numbers(text):
    if not text:
        return []

    t = normalize_upper(
        text
    )

    found = re.findall(
        r"\bTPS\s*0*(\d{1,3})",
        t
    )

    result = []

    for value in found:
        try:
            number = int(value)

            if 1 <= number <= 999:
                result.append(
                    number
                )

        except Exception:
            pass

    return sorted(
        set(result)
    )


# ============================================================
# DETEKSI HALAMAN AKHIR DESA
# ============================================================

def is_closing_page(text):
    t = normalize_upper(
        text
    )

    if not t:
        return False

    has_section_v = (
        "DATA SUARA SAH DAN TIDAK SAH"
        in t
    )

    has_valid = (
        "JUMLAH SELURUH SUARA SAH"
        in t
    )

    has_invalid = (
        "JUMLAH SUARA TIDAK SAH"
        in t
    )

    has_total = (
        "JUMLAH SELURUH SUARA SAH DAN TIDAK SAH"
        in t
        or
        "JUMLAH SELURUH SUARA SAH DAN SUARA TIDAK SAH"
        in t
    )

    tps_count = len(
        extract_tps_numbers(t)
    )

    return (
        has_section_v
        and has_valid
        and has_invalid
        and has_total
        and tps_count >= 2
    )


# ============================================================
# NAMA DESA
# ============================================================

def extract_village_name(text):
    if not text:
        return None

    lines = [
        x.strip()
        for x in text.splitlines()
        if x.strip()
    ]

    for line in lines:

        clean = re.sub(
            r"\s+",
            " ",
            line
        ).strip()

        upper = clean.upper()

        if (
            "KELURAHAN/DESA"
            in upper
            or
            "KELURAHAN / DESA"
            in upper
        ):

            value = re.sub(
                r"(?i).*KELURAHAN\s*/\s*DESA",
                "",
                clean
            )

            value = re.sub(
                r"^[.…\s:.-]+",
                "",
                value
            )

            value = re.sub(
                r"[.…]+",
                " ",
                value
            ).strip(
                " :.-"
            )

            if len(value) >= 3:
                return value

    for line in lines:

        upper = line.upper()

        if upper.startswith(
            "DESA "
        ):

            value = line[5:].strip(
                " :.-"
            )

            if len(value) >= 3:
                return value

    return None


# ============================================================
# MENCARI BARIS
# ============================================================

def find_line(
    lines,
    patterns
):
    for index, line in enumerate(
        lines
    ):

        upper = line.upper()

        for pattern in patterns:

            if pattern in upper:
                return index, line

    return None, None


def extract_last_numbers(
    line,
    expected=1
):
    nums = numbers_from_line(
        line
    )

    if len(nums) < expected:
        return []

    return nums[-expected:]


# ============================================================
# DATA SUARA HALAMAN AKHIR
# BERBASIS TEKS LINEAR
# ============================================================

def extract_closing_vote_data(text):
    """
    Mengambil suara sah, tidak sah, dan total dari halaman akhir.

    Tetap menggunakan prinsip:

    - tabel pertama = kemungkinan JUMLAH PINDAHAN
    - tabel terakhir = JUMLAH AKHIR
    - nilai final berasal dari kemunculan terakhir
    """

    lines = [
        x.strip()
        for x in normalize_text(text).splitlines()
        if x.strip()
    ]

    tps_numbers = extract_tps_numbers(
        text
    )

    count = len(
        tps_numbers
    )

    empty = {
        "tps": tps_numbers,
        "suara_sah": [None] * count,
        "suara_tidak_sah": [None] * count,
        "total_suara": [None] * count,
        "jumlah_akhir_suara_sah": None,
        "jumlah_akhir_suara_tidak_sah": None,
        "jumlah_akhir_total_suara": None,
    }

    if count == 0:
        return empty

    def is_row_a(u):
        return (
            "JUMLAH SELURUH SUARA SAH" in u
            and "TIDAK SAH" not in u
        )

    def is_row_b(u):
        return (
            "JUMLAH SUARA TIDAK SAH"
            in u
        )

    def is_row_c(u):
        return (
            "JUMLAH SELURUH SUARA SAH DAN TIDAK SAH"
            in u
            or
            "JUMLAH SELURUH SUARA SAH DAN SUARA TIDAK SAH"
            in u
        )

    def occurrences_of(predicate):
        return [
            i
            for i, line in enumerate(lines)
            if predicate(line.upper())
        ]

    sah_occurrences = occurrences_of(
        is_row_a
    )

    invalid_occurrences = occurrences_of(
        is_row_b
    )

    total_occurrences = occurrences_of(
        is_row_c
    )

    all_boundaries = (
        set(sah_occurrences)
        |
        set(invalid_occurrences)
        |
        set(total_occurrences)
    )

    label_strip_pattern = (
        r".*?(JUMLAH SELURUH SUARA SAH DAN SUARA TIDAK SAH"
        r"|JUMLAH SELURUH SUARA SAH DAN TIDAK SAH"
        r"|JUMLAH SUARA TIDAK SAH"
        r"|JUMLAH SELURUH SUARA SAH)"
    )

    def numbers_on_row(line_idx):

        line = lines[
            line_idx
        ]

        cleaned = re.sub(
            label_strip_pattern,
            " ",
            line,
            flags=re.I,
        )

        cleaned = re.sub(
            r"\([^)]*\)",
            " ",
            cleaned,
        )

        nums = numbers_from_line(
            cleaned
        )

        j = line_idx + 1

        while (
            j < len(lines)
            and j not in all_boundaries
        ):

            candidate = lines[
                j
            ].strip()

            if re.fullmatch(
                r"[\d.,\s]+",
                candidate
            ):

                nums.extend(
                    numbers_from_line(
                        candidate
                    )
                )

                j += 1

            else:
                break

        return nums

    def collect_final(
        occurrence_indices
    ):

        if not occurrence_indices:
            return [], None

        per_tps_values = []
        final_value = None

        total_occ = len(
            occurrence_indices
        )

        for pos, line_idx in enumerate(
            occurrence_indices
        ):

            nums = numbers_on_row(
                line_idx
            )

            if not nums:
                continue

            is_first = (
                pos == 0
            )

            is_last = (
                pos == total_occ - 1
            )

            if (
                is_first
                and
                is_last
            ):

                per_tps_values.extend(
                    nums[:-1]
                )

                final_value = nums[-1]

            elif is_first:

                per_tps_values.extend(
                    nums[:-1]
                )

            elif is_last:

                tail = nums[1:]

                if tail:

                    per_tps_values.extend(
                        tail[:-1]
                    )

                    final_value = tail[-1]

            else:

                per_tps_values.extend(
                    nums[1:-1]
                )

        return (
            per_tps_values,
            final_value
        )

    sah_values, sah_final = collect_final(
        sah_occurrences
    )

    invalid_values, invalid_final = collect_final(
        invalid_occurrences
    )

    total_values, total_final = collect_final(
        total_occurrences
    )

    def pad_to_count(values):

        values = list(
            values
        )[:count]

        while len(values) < count:
            values.append(None)

        return values

    sah_values = pad_to_count(
        sah_values
    )

    invalid_values = pad_to_count(
        invalid_values
    )

    total_values = pad_to_count(
        total_values
    )

    # --------------------------------------------------------
    # VALIDASI MATEMATIKA
    # --------------------------------------------------------

    if (
        sah_final is None
        and sah_values
        and all(
            v is not None
            for v in sah_values
        )
    ):

        sah_final = sum(
            sah_values
        )

    if (
        invalid_final is None
        and invalid_values
        and all(
            v is not None
            for v in invalid_values
        )
    ):

        invalid_final = sum(
            invalid_values
        )

    if (
        total_final is None
        and sah_final is not None
        and invalid_final is not None
    ):

        total_final = (
            sah_final
            +
            invalid_final
        )

    for i in range(count):

        if (
            total_values[i] is None
            and sah_values[i] is not None
            and invalid_values[i] is not None
        ):

            total_values[i] = (
                sah_values[i]
                +
                invalid_values[i]
            )

    return {
        "tps": tps_numbers,
        "suara_sah": sah_values,
        "suara_tidak_sah": invalid_values,
        "total_suara": total_values,
        "jumlah_akhir_suara_sah": sah_final,
        "jumlah_akhir_suara_tidak_sah": invalid_final,
        "jumlah_akhir_total_suara": total_final,
    }


# ============================================================
# DATA SUARA HALAMAN AKHIR
# BERBASIS KOORDINAT OCR
# ============================================================

def ocr_table_data(
    image,
    psm=6
):
    """
    OCR dengan koordinat kata.
    """

    try:

        img = preprocess_image(
            image
        )

        data = pytesseract.image_to_data(
            img,
            lang=OCR_LANGUAGE,
            config=f"--oem 3 --psm {psm}",
            output_type=pytesseract.Output.DICT,
        )

    except Exception:
        return []

    words = []

    total = len(
        data.get(
            "text",
            []
        )
    )

    for i in range(
        total
    ):

        txt = str(
            data["text"][i]
        ).strip()

        if not txt:
            continue

        try:
            conf = float(
                data["conf"][i]
            )
        except Exception:
            conf = -1

        words.append(
            {
                "text": txt,
                "x": int(
                    data["left"][i]
                ),
                "y": int(
                    data["top"][i]
                ),
                "w": int(
                    data["width"][i]
                ),
                "h": int(
                    data["height"][i]
                ),
                "conf": conf,
            }
        )

    return words


def _table_rows(
    words,
    y_tol=12
):
    """
    Kelompokkan kata menjadi baris
    berdasarkan posisi Y asli.
    """

    rows = []

    for w in sorted(
        words,
        key=lambda z: (
            z["y"],
            z["x"]
        )
    ):

        cy = (
            w["y"]
            +
            w["h"] / 2
        )

        target = None

        for row in rows:

            if abs(
                cy - row["cy"]
            ) <= y_tol:

                target = row
                break

        if target is None:

            target = {
                "cy": cy,
                "words": []
            }

            rows.append(
                target
            )

        target["words"].append(
            w
        )

    for row in rows:

        row["words"].sort(
            key=lambda z: z["x"]
        )

        row["text"] = " ".join(
            z["text"]
            for z in row["words"]
        )

    return rows


def _numeric_token(token):
    """
    Hanya melakukan koreksi karakter OCR yang sangat konservatif:

        O/o -> 0
        I/l -> 1

    Tidak melakukan:
        S -> 5
        B -> 8
        G -> 6

    karena koreksi agresif dapat menghasilkan angka palsu.
    """

    token = str(
        token
    ).strip()

    if not re.fullmatch(
        r"[0-9OolI.,-]+",
        token
    ):
        return None

    token = (
        token
        .replace("O", "0")
        .replace("o", "0")
        .replace("I", "1")
        .replace("l", "1")
    )

    token = (
        token
        .replace(".", "")
        .replace(",", "")
    )

    token = re.sub(
        r"[^0-9]",
        "",
        token
    )

    if not token:
        return None

    try:
        return int(token)

    except Exception:
        return None


def extract_closing_votes_positional(
    image
):
    """
    Ambil nilai suara berdasarkan posisi tabel.

    Angka paling kanan pada baris terkait dianggap sebagai
    kolom JUMLAH AKHIR.
    """

    words = ocr_table_data(
        image,
        psm=6
    )

    if not words:

        return {
            "suara_sah": None,
            "suara_tidak_sah": None,
            "total_suara": None,
        }

    rows = _table_rows(
        words,
        y_tol=max(
            8,
            int(
                image.height / 180
            )
        ),
    )

    out = {
        "suara_sah": None,
        "suara_tidak_sah": None,
        "total_suara": None,
    }

    for row in rows:

        u = row[
            "text"
        ].upper()

        if (
            "SUARA SAH" in u
            and
            "TIDAK SAH" not in u
        ):

            key = "suara_sah"

        elif "SUARA TIDAK SAH" in u:

            key = "suara_tidak_sah"

        elif (
            "JUMLAH SELURUH SUARA SAH DAN"
            in u
            or
            re.search(
                r"\bTOTAL\b",
                u
            )
        ):

            key = "total_suara"

        else:
            continue

        candidates = []

        for w in row[
            "words"
        ]:

            n = _numeric_token(
                w["text"]
            )

            if n is not None:

                candidates.append(
                    (
                        w["x"] + w["w"],
                        n
                    )
                )

        if candidates:

            candidates.sort(
                key=lambda z: z[0]
            )

            out[key] = candidates[
                -1
            ][1]

    if (
        out["total_suara"] is None
        and
        out["suara_sah"] is not None
        and
        out["suara_tidak_sah"] is not None
    ):

        out["total_suara"] = (
            out["suara_sah"]
            +
            out["suara_tidak_sah"]
        )

    return out


def refine_closing_data_with_positional(
    doc,
    page_index,
    closing_data,
    dpi=300,
):
    """
    Positional OCR hanya digunakan bila hasil utama belum lengkap.

    Ini penting agar hasil lama yang sudah benar tidak diganti
    oleh OCR alternatif secara sembarangan.
    """

    votes_incomplete = (
        closing_data.get(
            "jumlah_akhir_suara_sah"
        ) is None
        or
        closing_data.get(
            "jumlah_akhir_suara_tidak_sah"
        ) is None
    )

    if not votes_incomplete:
        return closing_data

    try:

        image = render_page(
            doc,
            page_index,
            dpi=max(
                dpi,
                300
            ),
        )

        positional = (
            extract_closing_votes_positional(
                image
            )
        )

    except Exception:
        return closing_data

    for key, pos_key in (
        (
            "jumlah_akhir_suara_sah",
            "suara_sah"
        ),
        (
            "jumlah_akhir_suara_tidak_sah",
            "suara_tidak_sah"
        ),
        (
            "jumlah_akhir_total_suara",
            "total_suara"
        ),
    ):

        if (
            positional.get(
                pos_key
            )
            is not None
        ):

            closing_data[
                key
            ] = positional[
                pos_key
            ]

    # --------------------------------------------------------
    # VALIDASI ULANG TOTAL
    # --------------------------------------------------------

    sah = closing_data.get(
        "jumlah_akhir_suara_sah"
    )

    tidak = closing_data.get(
        "jumlah_akhir_suara_tidak_sah"
    )

    if (
        sah is not None
        and
        tidak is not None
    ):

        calculated_total = (
            sah
            +
            tidak
        )

        if (
            closing_data.get(
                "jumlah_akhir_total_suara"
            )
            != calculated_total
        ):

            closing_data[
                "jumlah_akhir_total_suara"
            ] = calculated_total

    return closing_data


# ============================================================
# PARTY
# ============================================================

def detect_party_number(
    line
):
    if not line:
        return None

    upper = line.upper()

    patterns = [
        r"\b(\d{1,2})\s*\.\s*PARTAI",
        r"PARTAI\s*(\d{1,2})",
        r"A\.1\s*(\d{1,2})\s*\.",
        r"\b24\s*\.\s*PARTAI",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            upper
        )

        if match:

            try:

                number = int(
                    match.group(1)
                )

                if number == 24:
                    return 18

                if number in PARTY_NAMES:
                    return number

            except Exception:
                pass

    return None


def detect_party_heading(
    lines,
    start_index
):
    for i in range(
        start_index,
        -1,
        -1
    ):

        line = lines[
            i
        ]

        number = detect_party_number(
            line
        )

        if number is not None:
            return number

    return None


def is_party_final_label(
    line
):
    if not line:
        return False

    upper = line.upper()

    patterns = [
        "JUMLAH AKHIR",
        "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON",
        "JUMLAH SUARA SAH PARTAI POLITIK"
    ]

    return any(
        pattern in upper
        for pattern in patterns
    )


def clean_party_final_line(
    line
):
    if not line:
        return ""

    value = line

    value = re.sub(
        r"\([^)]*\)",
        " ",
        value
    )

    value = re.sub(
        r"(?i).*?JUMLAH SUARA SAH PARTAI POLITIK DAN CALON",
        " ",
        value
    )

    value = re.sub(
        r"(?i).*?JUMLAH SUARA SAH PARTAI POLITIK",
        " ",
        value
    )

    value = re.sub(
        r"(?i)^.*?JUMLAH AKHIR",
        " ",
        value
    )

    return value.strip()


def find_explicit_final_number(
    lines,
    start_index,
    end_index
):
    """
    Mencari angka setelah JUMLAH AKHIR.

    Tidak mengambil angka dari JUMLAH PINDAHAN.
    """

    for i in range(
        start_index,
        min(
            end_index,
            len(lines)
        )
    ):

        line = lines[
            i
        ]

        upper = line.upper()

        if "JUMLAH AKHIR" not in upper:
            continue

        cleaned = re.sub(
            r"(?i).*?JUMLAH AKHIR",
            " ",
            line
        )

        nums = numbers_from_line(
            cleaned
        )

        if nums:
            return nums[-1]

        for j in range(
            i + 1,
            min(
                i + 6,
                end_index,
                len(lines)
            )
        ):

            nums = numbers_from_line(
                lines[j]
            )

            if nums:
                return nums[-1]

    return None


def extract_party_results(
    text
):
    """
    Parser satu blok teks.
    """

    lines = [
        x.strip()
        for x in normalize_text(
            text
        ).splitlines()
        if x.strip()
    ]

    results = []

    expected_tps = len(
        extract_tps_numbers(
            text
        )
    )

    headings = []

    for i, line in enumerate(
        lines
    ):

        number = detect_party_number(
            line
        )

        if number is not None:

            headings.append(
                (
                    i,
                    number
                )
            )

    for pos, (
        start_i,
        party_number
    ) in enumerate(
        headings
    ):

        end_i = (
            headings[pos + 1][0]
            if pos + 1 < len(headings)
            else len(lines)
        )

        block_lines = lines[
            start_i:end_i
        ]

        final_idx = None

        for i, line in enumerate(
            block_lines
        ):

            upper = line.upper()

            if (
                "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON"
                in upper
                or
                "JUMLAH SUARA SAH PARTAI POLITIK"
                in upper
            ):

                final_idx = i
                break

        if final_idx is None:
            continue

        explicit_final = find_explicit_final_number(
            block_lines,
            final_idx,
            len(block_lines)
        )

        if explicit_final is not None:

            final_vote = explicit_final

        else:

            tail = []

            final_line = block_lines[
                final_idx
            ]

            after_label = clean_party_final_line(
                final_line
            )

            tail.extend(
                numbers_from_line(
                    after_label
                )
            )

            for line in block_lines[
                final_idx + 1:
            ]:

                if re.match(
                    r"^A\.1\b",
                    line,
                    flags=re.I
                ):
                    break

                if (
                    "JUMLAH PINDAHAN"
                    in line.upper()
                ):
                    continue

                nums = numbers_from_line(
                    line
                )

                if nums:
                    tail.extend(
                        nums
                    )

                if (
                    expected_tps
                    and
                    len(tail)
                    >= expected_tps + 1
                ):
                    break

            if (
                expected_tps
                and
                len(tail)
                >= expected_tps + 1
            ):

                final_vote = tail[
                    expected_tps
                ]

            elif tail:

                final_vote = tail[
                    -1
                ]

            else:
                continue

        results.append(
            {
                "partai": party_number,
                "nama_partai": PARTY_NAMES.get(
                    party_number,
                    f"Partai {party_number}"
                ),
                "suara_akhir_partai": final_vote,
            }
        )

    cleaned = {}

    for row in results:

        cleaned[
            row["partai"]
        ] = row

    return [
        cleaned[key]
        for key in sorted(
            cleaned
        )
    ]


# ============================================================
# PARTY MULTI HALAMAN
# ============================================================

def collect_row_numbers(
    all_lines,
    start_idx,
    boundary_idx
):
    """
    Ambil angka berurutan setelah label.
    """

    nums = []

    j = start_idx

    while j < boundary_idx:

        line = all_lines[
            j
        ].strip()

        if re.fullmatch(
            r"[\d.,]+",
            line
        ):

            n = clean_number(
                line
            )

            if n is not None:
                nums.append(
                    n
                )

            j += 1

        else:
            break

    return nums


def extract_party_results_multipage(
    page_texts,
    expected_tps=0
):
    all_lines = []

    for text in page_texts:

        normalized = normalize_text(
            text
        )

        lines = [
            x.strip()
            for x in normalized.splitlines()
            if x.strip()
        ]

        all_lines.extend(
            lines
        )

    if not all_lines:
        return []

    headings = []

    for i, line in enumerate(
        all_lines
    ):

        number = detect_party_number(
            line
        )

        if number is not None:

            headings.append(
                (
                    i,
                    number
                )
            )

    filtered_headings = []

    for item in headings:

        li, pn = item

        if filtered_headings:

            pli, ppn = filtered_headings[
                -1
            ]

            if (
                pn == ppn
                and
                li - pli <= 3
            ):
                continue

        filtered_headings.append(
            item
        )

    headings = filtered_headings

    candidates = defaultdict(
        list
    )

    for pos, (
        start_i,
        party_number
    ) in enumerate(
        headings
    ):

        next_heading_i = (
            headings[pos + 1][0]
            if pos + 1 < len(headings)
            else len(all_lines)
        )

        final_indices = []

        for i in range(
            start_i,
            next_heading_i
        ):

            upper = all_lines[
                i
            ].upper()

            if (
                "JUMLAH AKHIR"
                in upper
                or
                "JUMLAH SUARA SAH PARTAI POLITIK DAN CALON"
                in upper
                or
                "JUMLAH SUARA SAH PARTAI POLITIK"
                in upper
            ):

                final_indices.append(
                    i
                )

        if not final_indices:
            continue

        final_idx = final_indices[
            -1
        ]

        final_line = all_lines[
            final_idx
        ]

        same_line_nums = numbers_from_line(
            clean_party_final_line(
                final_line
            )
        )

        row_nums = collect_row_numbers(
            all_lines,
            final_idx + 1,
            next_heading_i
        )

        row_nums = (
            same_line_nums
            +
            row_nums
        )

        final_vote = (
            row_nums[-1]
            if row_nums
            else None
        )

        if final_vote is not None:

            candidates[
                party_number
            ].append(
                {
                    "partai": party_number,
                    "nama_partai": PARTY_NAMES.get(
                        party_number,
                        f"Partai {party_number}"
                    ),
                    "suara_akhir_partai": final_vote,
                    "_final_index": final_idx,
                }
            )

    cleaned = {}

    for party_number, items in candidates.items():

        if not items:
            continue

        selected = sorted(
            items,
            key=lambda x: x.get(
                "_final_index",
                -1
            )
        )[-1]

        cleaned[
            party_number
        ] = selected

    return [
        cleaned[key]
        for key in sorted(
            cleaned
        )
    ]


# ============================================================
# PARTY DARI HALAMAN
# ============================================================

def extract_party_results_from_page(
    doc,
    page_index,
    dpi=300
):
    """
    Membaca satu halaman.

    Fallback dan kompatibilitas.
    """

    page = doc.load_page(
        page_index
    )

    text = page.get_text(
        "text"
    ) or ""

    text = normalize_text(
        text
    )

    results = extract_party_results(
        text
    )

    if results:
        return (
            results,
            text
        )

    image = render_page(
        doc,
        page_index,
        dpi=dpi
    )

    text = ocr_image(
        image,
        psm=6
    )

    results = extract_party_results(
        text
    )

    return (
        results,
        text
    )


# ============================================================
# MENENTUKAN AWAL DESA
# ============================================================

def determine_village_start(
    closing_pages,
    first_village_start
):
    starts = []

    previous_closing = None

    for closing_page in closing_pages:

        if previous_closing is None:

            start = first_village_start

        else:

            start = (
                previous_closing
                +
                1
            )

        starts.append(
            start
        )

        previous_closing = closing_page

    return starts


def find_first_village_page(
    page_texts
):
    for index, text in enumerate(
        page_texts
    ):

        village = extract_village_name(
            text
        )

        if village:
            return index

    return 0


# ============================================================
# BUILD BLOCK DESA
# ============================================================

def build_village_blocks(
    page_texts,
    closing_pages
):
    if not closing_pages:
        return []

    closing_pages = sorted(
        set(
            closing_pages
        )
    )

    first_start = find_first_village_page(
        page_texts
    )

    blocks = []

    previous_end = None

    for i, closing_page in enumerate(
        closing_pages
    ):

        if previous_end is None:

            start_page = first_start

            for p in range(
                closing_page,
                max(
                    -1,
                    closing_page - 20
                ),
                -1
            ):

                if extract_village_name(
                    page_texts[p]
                ):

                    start_page = p

            if start_page > closing_page:
                start_page = first_start

        else:

            start_page = (
                previous_end
                +
                1
            )

        village = None

        for p in range(
            start_page,
            min(
                closing_page + 1,
                start_page + 8
            )
        ):

            found = extract_village_name(
                page_texts[p]
            )

            if found:

                village = found
                break

        if village is None:

            village = extract_village_name(
                page_texts[
                    closing_page
                ]
            )

        if village is None:

            village = (
                f"Kelurahan {i + 1}"
            )

        blocks.append(
            {
                "village": village,
                "start_page": start_page,
                "end_page": closing_page,
            }
        )

        previous_end = closing_page

    return blocks


# ============================================================
# WILAYAH
# ============================================================

def clean_region_value(
    value
):
    if value is None:
        return None

    value = str(
        value
    ).replace(
        "\xa0",
        " "
    )

    value = re.sub(
        r"[.]{3,}",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip(
        " :.-/\t"
    )

    if not value:
        return None

    blocked = {
        "DPR",
        "DPD",
        "DPRD",
        "MODEL",
        "URAIAN",
        "HALAMAN",
        "NO",
        "NOMOR",
        "TPS",
        "PARTAI",
        "JUMLAH",
        "SUARA",
    }

    if value.upper() in blocked:
        return None

    return value


def _labeled_value(
    line,
    pattern
):
    m = re.search(
        pattern
        +
        r"\s*[:.-]?\s*(.+)$",
        line,
        re.I
    )

    return (
        clean_region_value(
            m.group(1)
        )
        if m
        else None
    )


def extract_region_fields(
    text
):
    result = {
        "provinsi": None,
        "dapil": None,
        "kab_kota": None,
        "kecamatan": None,
    }

    lines = [
        x.strip()
        for x in normalize_text(
            text
        ).splitlines()
        if x.strip()
    ]

    for i, line in enumerate(
        lines
    ):

        upper = line.upper()

        next_line = (
            lines[i + 1]
            if i + 1 < len(lines)
            else ""
        )

        if "PROVINSI" in upper:

            value = _labeled_value(
                line,
                r".*?PROVINSI"
            )

            if (
                not value
                and
                next_line.startswith(":")
            ):

                value = clean_region_value(
                    next_line.lstrip(": ")
                )

            if value:

                result[
                    "provinsi"
                ] = value

        elif "DAERAH PEMILIHAN" in upper:

            value = _labeled_value(
                line,
                r".*?DAERAH PEMILIHAN"
            )

            if (
                not value
                and
                next_line.startswith(":")
            ):

                value = clean_region_value(
                    next_line.lstrip(": ")
                )

            if value:

                result[
                    "dapil"
                ] = value

        elif re.search(
            r"KABUPATEN\s*/?\s*KOTA",
            upper
        ):

            value = _labeled_value(
                line,
                r".*?KABUPATEN\s*/?\s*KOTA"
            )

            if (
                not value
                and
                next_line.startswith(":")
            ):

                value = clean_region_value(
                    next_line.lstrip(": ")
                )

            if value:

                result[
                    "kab_kota"
                ] = value

        elif "KECAMATAN" in upper:

            tail = re.sub(
                r".*?KECAMATAN",
                "",
                line,
                flags=re.I
            )

            tail = re.sub(
                r"^\s*/?\s*",
                "",
                tail
            )

            tail = re.sub(
                r"^[.\-: ]+",
                "",
                tail
            )

            value = clean_region_value(
                tail
            )

            if (
                value
                and
                value.upper()
                not in {
                    "DPR",
                    "DPD",
                    "DPRD"
                }
            ):

                result[
                    "kecamatan"
                ] = value

            elif next_line.startswith(":"):

                value = clean_region_value(
                    next_line.lstrip(": ")
                )

                if (
                    value
                    and
                    value.upper()
                    not in {
                        "DPR",
                        "DPD",
                        "DPRD"
                    }
                ):

                    result[
                        "kecamatan"
                    ] = value

    return result


# ============================================================
# DATAFRAME TPS
# ============================================================

def build_tps_dataframe(
    blocks
):
    rows = []

    for block in blocks:

        closing_data = block.get(
            "closing_data",
            {}
        )

        tps = closing_data.get(
            "tps",
            []
        )

        sah = closing_data.get(
            "suara_sah",
            []
        )

        tidak_sah = closing_data.get(
            "suara_tidak_sah",
            []
        )

        total = closing_data.get(
            "total_suara",
            []
        )

        region = block.get(
            "region",
            {}
        )

        for i, tps_number in enumerate(
            tps
        ):

            rows.append(
                {
                    "halaman": (
                        block[
                            "end_page"
                        ]
                        +
                        1
                    ),
                    "provinsi": region.get(
                        "provinsi"
                    ),
                    "dapil": region.get(
                        "dapil"
                    ),
                    "kab_kota": region.get(
                        "kab_kota"
                    ),
                    "kecamatan": region.get(
                        "kecamatan"
                    ),
                    "kelurahan": block[
                        "village"
                    ],
                    "tps": tps_number,
                    "suara_sah": (
                        sah[i]
                        if i < len(sah)
                        else None
                    ),
                    "suara_tidak_sah": (
                        tidak_sah[i]
                        if i < len(tidak_sah)
                        else None
                    ),
                    "total_suara": (
                        total[i]
                        if i < len(total)
                        else None
                    ),
                }
            )

    return pd.DataFrame(
        rows
    )


# ============================================================
# DATAFRAME PARTY
# ============================================================

def build_party_dataframe(
    blocks
):
    rows = []

    for block in blocks:

        region = block.get(
            "region",
            {}
        )

        party_results = block.get(
            "party_results",
            []
        )

        for item in party_results:

            rows.append(
                {
                    "kelurahan": block[
                        "village"
                    ],
                    "partai": item.get(
                        "partai"
                    ),
                    "nama_partai": item.get(
                        "nama_partai"
                    ),
                    "suara_akhir_partai": item.get(
                        "suara_akhir_partai"
                    ),
                    "provinsi": region.get(
                        "provinsi"
                    ),
                    "dapil": region.get(
                        "dapil"
                    ),
                    "kab_kota": region.get(
                        "kab_kota"
                    ),
                    "kecamatan": region.get(
                        "kecamatan"
                    ),
                }
            )

    return pd.DataFrame(
        rows
    )


# ============================================================
# RANGE DESA
# ============================================================

def build_ranges_dataframe(
    blocks,
    file_name
):
    rows = []

    for block in blocks:

        start = (
            block[
                "start_page"
            ]
            +
            1
        )

        end = (
            block[
                "end_page"
            ]
            +
            1
        )

        closing_data = block.get(
            "closing_data",
            {}
        )

        rows.append(
            {
                "kelurahan": block[
                    "village"
                ],
                "halaman": f"{start}-{end}",
                "total_tps": len(
                    closing_data.get(
                        "tps",
                        []
                    )
                ),
                "suara_sah": closing_data.get(
                    "jumlah_akhir_suara_sah"
                ),
                "suara_tidak_sah": closing_data.get(
                    "jumlah_akhir_suara_tidak_sah"
                ),
                "total_suara": closing_data.get(
                    "jumlah_akhir_total_suara"
                ),
            }
        )

    return pd.DataFrame(
        rows
    )


# ============================================================
# VALIDASI
# ============================================================

def build_validation_dataframe(
    blocks
):
    rows = []

    for block in blocks:

        data = block.get(
            "closing_data",
            {}
        )

        sah = data.get(
            "jumlah_akhir_suara_sah"
        )

        tidak_sah = data.get(
            "jumlah_akhir_suara_tidak_sah"
        )

        total = data.get(
            "jumlah_akhir_total_suara"
        )

        calculated = None

        if (
            sah is not None
            and
            tidak_sah is not None
        ):

            calculated = (
                sah
                +
                tidak_sah
            )

        valid = (
            calculated == total
            if (
                calculated is not None
                and
                total is not None
            )
            else False
        )

        rows.append(
            {
                "kelurahan": block[
                    "village"
                ],
                "suara_sah": sah,
                "suara_tidak_sah": tidak_sah,
                "total_suara": total,
                "hasil_perhitungan": calculated,
                "status": (
                    "OK"
                    if valid
                    else "PERLU DICEK"
                ),
            }
        )

    return pd.DataFrame(
        rows
    )


# ============================================================
# SUMMARY
# ============================================================

def build_summary_dataframe(
    file_name,
    total_pages,
    blocks
):
    total_tps = 0

    for block in blocks:

        total_tps += len(
            block.get(
                "closing_data",
                {}
            ).get(
                "tps",
                []
            )
        )

    unique_parties = set()

    for block in blocks:

        for item in block.get(
            "party_results",
            []
        ):

            party_number = item.get(
                "partai"
            )

            if party_number is not None:

                unique_parties.add(
                    party_number
                )

    total_parties = len(
        unique_parties
    )

    return pd.DataFrame(
        [
            {
                "file_name": file_name,
                "jumlah_halaman": total_pages,
                "jumlah_kelurahan": len(
                    blocks
                ),
                "jumlah_tps": total_tps,
                "jumlah_partai": total_parties,
                "perlu_review": 0,
            }
        ]
    )


# ============================================================
# RAW OCR
# ============================================================

def build_raw_dataframe(
    file_name,
    page_texts
):
    rows = []

    for index, text in enumerate(
        page_texts
    ):

        rows.append(
            {
                "file_name": file_name,
                "halaman": index + 1,
                "text": text,
            }
        )

    return pd.DataFrame(
        rows
    )


# ============================================================
# FUNGSI UTAMA
# ============================================================

def process_pdf_local_engine(
    file_bytes,
    file_name="dokumen.pdf",
    progress_callback=None,
    dpi_val=300
):
    """
    Interface utama yang dipakai oleh processor.

    progress_callback:
        callback(progress, message)

    progress berada pada 0.0 - 1.0.
    """

    if not file_bytes:
        raise ValueError(
            "File PDF kosong."
        )

    dpi_val = int(
        dpi_val or DEFAULT_DPI
    )

    if dpi_val < 180:
        dpi_val = 180

    if dpi_val > 500:
        dpi_val = 500

    doc = open_pdf(
        file_bytes
    )

    total_pages = len(
        doc
    )

    if total_pages == 0:
        doc.close()

        raise ValueError(
            "PDF tidak memiliki halaman."
        )

    try:

        # ====================================================
        # TAHAP 1
        # BACA SEMUA HALAMAN
        # ====================================================

        page_texts = []

        for page_index in range(
            total_pages
        ):

            try:

                text = ocr_page(
                    doc,
                    page_index,
                    dpi=180
                )

            except Exception:

                text = ""

            page_texts.append(
                normalize_text(
                    text
                )
            )

            if progress_callback:

                report_progress(
                    progress_callback,
                    (
                        (
                            page_index
                            +
                            1
                        )
                        /
                        max(
                            total_pages,
                            1
                        )
                        *
                        0.35
                    ),
                    (
                        f"Membaca halaman "
                        f"{page_index + 1}/"
                        f"{total_pages}"
                    )
                )

        # ====================================================
        # TAHAP 2
        # CARI HALAMAN AKHIR DESA
        # ====================================================

        closing_pages = []

        for page_index, text in enumerate(
            page_texts
        ):

            if is_closing_page(
                text
            ):

                closing_pages.append(
                    page_index
                )

        # ----------------------------------------------------
        # Hilangkan anchor yang terlalu berdekatan.
        # ----------------------------------------------------

        filtered_closing = []

        for page in closing_pages:

            if not filtered_closing:

                filtered_closing.append(
                    page
                )

                continue

            previous = filtered_closing[
                -1
            ]

            if (
                page
                -
                previous
                >= 2
            ):

                filtered_closing.append(
                    page
                )

        closing_pages = filtered_closing

        # ====================================================
        # TAHAP 3
        # BANGUN BLOK DESA
        # ====================================================

        blocks = build_village_blocks(
            page_texts,
            closing_pages
        )

        # ====================================================
        # TAHAP 4
        # BACA HALAMAN AKHIR DESA
        # ====================================================

        for block_index, block in enumerate(
            blocks
        ):

            closing_page = block[
                "end_page"
            ]

            closing_text = page_texts[
                closing_page
            ]

            # ------------------------------------------------
            # OCR ulang halaman akhir dengan DPI tinggi.
            # ------------------------------------------------

            try:

                closing_text_high = ocr_page(
                    doc,
                    closing_page,
                    dpi=dpi_val
                )

                # Hanya gunakan hasil high-DPI bila memang
                # lebih panjang DAN tidak membuat Text Layer
                # yang bagus tergantikan secara sembarangan.
                #
                # ocr_page sendiri sudah mempunyai proteksi
                # Text Layer.

                if len(
                    closing_text_high
                ) > len(
                    closing_text
                ):

                    closing_text = (
                        closing_text_high
                    )

            except Exception:
                pass

            block[
                "closing_text"
            ] = closing_text

            closing_data = extract_closing_vote_data(
                closing_text
            )

            # ------------------------------------------------
            # POSITIONAL FALLBACK
            # ------------------------------------------------

            closing_data = (
                refine_closing_data_with_positional(
                    doc,
                    closing_page,
                    closing_data,
                    dpi=dpi_val,
                )
            )

            block[
                "closing_data"
            ] = closing_data

            # ------------------------------------------------
            # WILAYAH
            # ------------------------------------------------

            region = {
                "provinsi": None,
                "dapil": None,
                "kab_kota": None,
                "kecamatan": None,
            }

            start = block[
                "start_page"
            ]

            end = min(
                block[
                    "end_page"
                ]
                +
                1,
                len(
                    page_texts
                )
            )

            for page_index in range(
                start,
                end
            ):

                found_region = (
                    extract_region_fields(
                        page_texts[
                            page_index
                        ]
                    )
                )

                for key in region:

                    if (
                        not region.get(
                            key
                        )
                        and
                        found_region.get(
                            key
                        )
                    ):

                        region[
                            key
                        ] = found_region[
                            key
                        ]

                if all(
                    region.values()
                ):
                    break

            closing_region = (
                extract_region_fields(
                    closing_text
                )
            )

            for key in region:

                if not region.get(
                    key
                ):

                    region[
                        key
                    ] = closing_region.get(
                        key
                    )

            block[
                "region"
            ] = region

            # ------------------------------------------------
            # NAMA DESA
            # ------------------------------------------------

            village = None

            for page_index in range(
                start,
                min(
                    start + 5,
                    end
                )
            ):

                village = extract_village_name(
                    page_texts[
                        page_index
                    ]
                )

                if village:
                    break

            if village:

                block[
                    "village"
                ] = village

            if progress_callback:

                report_progress(
                    progress_callback,
                    (
                        0.35
                        +
                        (
                            (
                                block_index
                                +
                                1
                            )
                            /
                            max(
                                len(
                                    blocks
                                ),
                                1
                            )
                            *
                            0.30
                        )
                    ),
                    (
                        f"Membaca penutup desa "
                        f"{block_index + 1}/"
                        f"{len(blocks)}"
                    )
                )

        # ====================================================
        # TAHAP 5
        # BACA SUARA PARTAI MULTI HALAMAN
        # ====================================================

        for block_index, block in enumerate(
            blocks
        ):

            start = block[
                "start_page"
            ]

            end = block[
                "end_page"
            ]

            closing_data = block.get(
                "closing_data",
                {}
            )

            expected_tps = len(
                closing_data.get(
                    "tps",
                    []
                )
            )

            # ------------------------------------------------
            # Ambil semua teks dalam blok desa.
            # ------------------------------------------------

            block_page_texts = []

            for page_index in range(
                start,
                end + 1
            ):

                text = page_texts[
                    page_index
                ]

                # ------------------------------------------------
                # Jika halaman terlalu pendek/kosong,
                # baca ulang dengan DPI tinggi.
                # ------------------------------------------------

                if len(
                    text.strip()
                ) < 80:

                    try:

                        better_text = ocr_page(
                            doc,
                            page_index,
                            dpi=dpi_val
                        )

                        if len(
                            better_text
                        ) > len(
                            text
                        ):

                            text = (
                                better_text
                            )

                    except Exception:
                        pass

                block_page_texts.append(
                    text
                )

            # ------------------------------------------------
            # Parser utama multi halaman.
            # ------------------------------------------------

            party_results = (
                extract_party_results_multipage(
                    block_page_texts,
                    expected_tps=expected_tps
                )
            )

            # ------------------------------------------------
            # Fallback bila kurang dari 18 partai.
            # ------------------------------------------------

            found_numbers = {
                item.get(
                    "partai"
                )
                for item in party_results
                if item.get(
                    "partai"
                ) is not None
            }

            if len(
                found_numbers
            ) < 18:

                fallback_results = []

                for page_offset, page_text in enumerate(
                    block_page_texts
                ):

                    results = (
                        extract_party_results(
                            page_text
                        )
                    )

                    if results:

                        fallback_results.extend(
                            results
                        )

                for item in fallback_results:

                    number = item.get(
                        "partai"
                    )

                    if (
                        number is not None
                        and
                        number not in found_numbers
                    ):

                        party_results.append(
                            item
                        )

                        found_numbers.add(
                            number
                        )

            # ------------------------------------------------
            # Hapus duplikat.
            # ------------------------------------------------

            party_map = {}

            for item in party_results:

                party_number = item.get(
                    "partai"
                )

                if party_number is None:
                    continue

                party_map[
                    party_number
                ] = item

            party_results = [
                party_map[key]
                for key in sorted(
                    party_map
                )
            ]

            block[
                "party_results"
            ] = party_results

            if progress_callback:

                report_progress(
                    progress_callback,
                    (
                        0.65
                        +
                        (
                            (
                                block_index
                                +
                                1
                            )
                            /
                            max(
                                len(
                                    blocks
                                ),
                                1
                            )
                            *
                            0.25
                        )
                    ),
                    (
                        f"Membaca suara partai "
                        f"{block_index + 1}/"
                        f"{len(blocks)}"
                    )
                )

        # ====================================================
        # TAHAP 6
        # HASIL AKHIR
        # ====================================================

        summary_df = (
            build_summary_dataframe(
                file_name,
                total_pages,
                blocks
            )
        )

        ranges_df = (
            build_ranges_dataframe(
                blocks,
                file_name
            )
        )

        tps_df = (
            build_tps_dataframe(
                blocks
            )
        )

        parties_df = (
            build_party_dataframe(
                blocks
            )
        )

        validation_df = (
            build_validation_dataframe(
                blocks
            )
        )

        raw_df = (
            build_raw_dataframe(
                file_name,
                page_texts
            )
        )

        # ====================================================
        # DATABASE
        # ====================================================

        db_rows = []

        for _, row in tps_df.iterrows():

            db_rows.append(
                {
                    "provinsi": row.get(
                        "provinsi"
                    ),
                    "dapil": row.get(
                        "dapil"
                    ),
                    "kab_kota": row.get(
                        "kab_kota"
                    ),
                    "kecamatan": row.get(
                        "kecamatan"
                    ),
                    "kelurahan": row.get(
                        "kelurahan"
                    ),
                    "tps": row.get(
                        "tps"
                    ),
                    "suara_sah": row.get(
                        "suara_sah"
                    ),
                    "suara_tidak_sah": row.get(
                        "suara_tidak_sah"
                    ),
                    "total_suara": row.get(
                        "total_suara"
                    ),
                    "file_name": file_name,
                }
            )

        for _, row in parties_df.iterrows():

            db_rows.append(
                {
                    "provinsi": row.get(
                        "provinsi"
                    ),
                    "dapil": row.get(
                        "dapil"
                    ),
                    "kab_kota": row.get(
                        "kab_kota"
                    ),
                    "kecamatan": row.get(
                        "kecamatan"
                    ),
                    "kelurahan": row.get(
                        "kelurahan"
                    ),
                    "tps": None,
                    "partai": row.get(
                        "partai"
                    ),
                    "nama_partai": row.get(
                        "nama_partai"
                    ),
                    "suara_akhir_partai": row.get(
                        "suara_akhir_partai"
                    ),
                    "file_name": file_name,
                }
            )

        db_df = pd.DataFrame(
            db_rows
        )

        # ====================================================
        # PROGRESS SELESAI
        # ====================================================

        if progress_callback:

            report_progress(
                progress_callback,
                1.0,
                "Selesai"
            )

        return {
            "summary": summary_df,
            "db": db_df,
            "ranges": ranges_df,
            "tps": tps_df,
            "parties": parties_df,
            "validation": validation_df,
            "raw": raw_df,
        }

    finally:

        try:
            doc.close()
        except Exception:
            pass


# ============================================================
# ALIAS KOMPATIBILITAS
# ============================================================

def proses_pdf_batch_hybrid(
    file_bytes,
    nama_file,
    api_key=None,
    progress_callback=None,
    dpi=300
):
    """
    Alias kompatibilitas.

    Tidak menggunakan Gemini atau AI.
    """

    return process_pdf_local_engine(
        file_bytes=file_bytes,
        file_name=nama_file,
        progress_callback=progress_callback,
        dpi_val=dpi
    )


# ============================================================
# EXPORT EXCEL
# ============================================================

def export_hasil_excel(
    hasil,
    output_path=None
):
    if output_path is None:

        output_path = (
            "hasil_pembacaan.xlsx"
        )

    with pd.ExcelWriter(
        output_path,
        engine="openpyxl"
    ) as writer:

        if isinstance(
            hasil,
            dict
        ):

            for sheet_name, df in hasil.items():

                if not isinstance(
                    df,
                    pd.DataFrame
                ):
                    continue

                safe_name = str(
                    sheet_name
                )[:31]

                df.to_excel(
                    writer,
                    sheet_name=safe_name,
                    index=False
                )

        elif isinstance(
            hasil,
            pd.DataFrame
        ):

            hasil.to_excel(
                writer,
                sheet_name="Hasil",
                index=False
            )

    return output_path