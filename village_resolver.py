"""
village_resolver.py
====================

Modul ini menggabungkan dua kekuatan yang sebelumnya terpisah di dua sistem:

1. Dari sistem "OCR_Rekap_Kelurahan" (teman):
   - Normalisasi nama desa yang tahan typo OCR (mengganti 1<->I, 0<->O, dst).
   - Fuzzy matching (SequenceMatcher + token overlap) ke daftar kanonis.
   - Carry-forward nama desa antar halaman dengan batas (MAX_VILLAGE_CARRY_PAGES).

2. Perbaikan baru (tidak ada di kedua sistem asal):
   - Daftar desa TIDAK di-hardcode per kecamatan (kelemahan sistem teman:
     DESA_KANONIS-nya berisi nama desa Kota Bogor, tidak relevan untuk
     PDF Lampung/Tulang Bawang yang sedang diproses -> fuzzy matching jadi
     percuma). Di sini daftar kanonis dibangun OTOMATIS per file PDF
     (auto-roster), lewat proses dua tahap:
       Pass 1 -> kumpulkan semua bacaan desa yang confidence-nya tinggi
                 (dibaca langsung dari halaman, bukan hasil warisan).
       Pass 2 -> pakai roster itu untuk membetulkan/menyamakan bacaan yang
                 lemah, dan sebagai target carry-forward.
   - Setiap hasil akhir diberi label sumber: "LANGSUNG", "DIWARISI",
     atau "DICOCOKKAN" -> supaya tim bisa audit halaman mana yang perlu
     dicek manual, alih-alih hanya percaya begitu saja.

Modul ini sengaja dibuat independen dari engine OCR/AI mana pun (Gemini
maupun Tesseract) supaya bisa dipakai bersama oleh pdf_hybrid_engine.py
ATAU local_ocr_engine.py tanpa duplikasi logika.
"""

import re
from difflib import SequenceMatcher
from collections import Counter


# ============================================================
# KONFIGURASI DEFAULT (bisa di-override per pemanggilan)
# ============================================================

DESA_SIMILARITY_MIN_DEFAULT = 0.62
MAX_VILLAGE_CARRY_PAGES_DEFAULT = 30
MIN_DIRECT_CONFIDENCE_FOR_ROSTER = 0.70


# ============================================================
# NORMALISASI TEKS
# ============================================================

def norm(text):
    text = str(text or "").upper()
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"[^A-Z0-9\s./:+-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_desa(text):
    """
    Normalisasi khusus nama desa: buang prefix DESA/KELURAHAN,
    dan tukar karakter yang sering tertukar OCR (1<->I, 0<->O, dst),
    supaya "GEDUNG KARYA J1TU" == "GEDUNG KARYA JITU".
    """
    text = norm(text)
    text = re.sub(r"^(KELURAHAN\s*/?\s*DESA|KELURAHAN|DESA|KEL|DES)\s*[:.\-]*\s*", "", text)
    text = re.sub(r"\.+", " ", text)
    text = text.translate(str.maketrans({"1": "I", "0": "O", "4": "A", "5": "S"}))
    return re.sub(r"\s+", " ", text).strip()


def similarity(a, b):
    a, b = norm_desa(a), norm_desa(b)
    if not a or not b:
        return 0.0
    seq_ratio = SequenceMatcher(None, a, b).ratio()
    token_a, token_b = set(a.split()), set(b.split())
    token_ratio = len(token_a & token_b) / max(1, len(token_a | token_b))
    return 0.70 * seq_ratio + 0.30 * token_ratio


def _tail_similarity(a, b, tail_words=3):
    """
    Bandingkan hanya N kata terakhir dari dua nama. Berguna untuk kasus
    sisa noise OCR berupa huruf (bukan digit) yang menempel di DEPAN nama
    desa, mis. "CCCCNEREEEET GEDUNG KARYA JITU" vs "GEDUNG KARYA JITU"
    -- dengan tail matching, keduanya tetap dikenali sebagai desa yang
    sama karena tiga kata terakhirnya identik.
    """
    tail_a = " ".join(norm_desa(a).split()[-tail_words:])
    tail_b = " ".join(norm_desa(b).split()[-tail_words:])
    if not tail_a or not tail_b:
        return 0.0
    return SequenceMatcher(None, tail_a, tail_b).ratio()


# ============================================================
# EKSTRAKSI KANDIDAT NAMA DESA DARI TEKS MENTAH
# ============================================================

_DESA_LINE_PATTERNS = [
    r"KELURAHAN\s*/?\s*DESA\s*[:.\-]+\s*(.+)$",
    r"\bKELURAHAN\b\s*[:.\-]+\s*(.+)$",
    r"\bDESA\b\s*[:.\-]+\s*(.+)$",
]

_STOP_WORDS = r"\b(?:NO|URAIAN|RINCIAN|TPS|DATA|PROVINSI|KABUPATEN|KECAMATAN|KODE|LAMPIRAN)\b"


def _strip_ocr_dot_leader_noise(candidate):
    """
    Formulir KPU biasanya mencetak label dengan garis titik-titik sebagai
    pengisi, mis. "KELURAHAN/DESA ....................: GEDUNG KARYA JITU".
    Saat di-OCR, garis titik itu sering terbaca sebagai gerombolan
    karakter acak yang menempel SEBELUM nama desa yang sebenarnya, mis.
    "0.00.-000000EE0000 GEDUNG KARYA JITU" atau "RSER0ET GEDUNG KARYA JITU".

    Heuristik: nama desa asli hampir tidak pernah mengandung digit,
    sedangkan sisa "noise" dari garis titik OCR hampir selalu mengandung
    setidaknya satu digit. Maka: buang token di awal yang mengandung
    digit, sisakan token-token murni huruf di baliknya.
    """
    tokens = candidate.split()
    start = 0
    for idx, token in enumerate(tokens):
        if not any(ch.isdigit() for ch in token):
            start = idx
            break
    else:
        start = len(tokens)

    return " ".join(tokens[start:]).strip()


def extract_desa_candidate_from_text(raw_text):
    """
    Cari baris yang eksplisit menyebut 'KELURAHAN/DESA: ...' pada teks
    hasil OCR/AI satu halaman. Mengembalikan string kandidat mentah
    (belum dicocokkan ke roster) atau "" jika tidak ada.
    """
    lines = [norm(x) for x in str(raw_text or "").splitlines() if x.strip()]

    for line in lines:
        if "KELURAHAN" not in line and "DESA" not in line:
            continue
        for pattern in _DESA_LINE_PATTERNS:
            m = re.search(pattern, line)
            if m:
                candidate = re.split(_STOP_WORDS, m.group(1))[0].strip(" .:-")
                candidate = _strip_ocr_dot_leader_noise(candidate)
                if len(candidate) >= 3:
                    return candidate

    return ""


# ============================================================
# ROSTER OTOMATIS (PASS 1)
# ============================================================

def build_auto_roster(direct_readings, min_confidence=MIN_DIRECT_CONFIDENCE_FOR_ROSTER):
    """
    Bangun daftar kanonis nama desa secara OTOMATIS dari seluruh halaman
    dalam SATU file PDF, tanpa perlu hardcode per kecamatan.

    Parameters
    ----------
    direct_readings : list[tuple[str, float]]
        Daftar (nama_desa_mentah, confidence) hasil bacaan LANGSUNG per
        halaman (bukan hasil warisan/carry-forward).
    min_confidence : float
        Ambang confidence minimum agar sebuah bacaan dianggap layak masuk
        roster.

    Returns
    -------
    list[str]
        Daftar nama desa kanonis (representatif, sudah dinormalisasi
        tampilannya), diurutkan sesuai kemunculan pertama.
    """
    # Kelompokkan varian tulisan yang mirip agar "GEDUNG KARYA JITU" dan
    # "GEDUNG KARYA J1TU" (typo OCR) tidak dianggap dua desa berbeda.
    clusters = []  # list of {"label": str, "variants": Counter}

    for raw_name, confidence in direct_readings:
        if not raw_name or confidence < min_confidence:
            continue

        matched_cluster = None
        for cluster in clusters:
            score = max(
                similarity(raw_name, cluster["label"]),
                _tail_similarity(raw_name, cluster["label"]),
            )
            if score >= 0.80:
                matched_cluster = cluster
                break

        if matched_cluster is None:
            clusters.append({
                "label": raw_name.strip(),
                "variants": Counter([raw_name.strip()]),
            })
        else:
            matched_cluster["variants"][raw_name.strip()] += 1
            # Pakai varian yang paling sering muncul sebagai label resmi
            matched_cluster["label"] = matched_cluster["variants"].most_common(1)[0][0]

    return [cluster["label"] for cluster in clusters]


# ============================================================
# PENCOCOKAN KE ROSTER (PASS 2)
# ============================================================

def match_to_roster(candidate, roster, similarity_min=DESA_SIMILARITY_MIN_DEFAULT):
    """
    Cocokkan satu kandidat nama desa ke roster kanonis.

    Returns
    -------
    (nama_final, confidence, cocok) : tuple[str, float, bool]
        `cocok=True` berarti berhasil dipetakan ke salah satu entri
        roster. Jika False, `nama_final` adalah kandidat asli (dibersihkan)
        dan sistem sebaiknya menandainya untuk dicek manual.
    """
    if not candidate:
        return "", 0.0, False

    if not roster:
        # Tidak ada roster (mis. file baru / desa pertama) -> percaya
        # kandidat mentah apa adanya.
        return candidate.strip(), 1.0, True

    best_name, best_score = None, 0.0
    for name in roster:
        score = similarity(candidate, name)
        if score > best_score:
            best_name, best_score = name, score

    if best_score >= similarity_min:
        return best_name, round(best_score, 3), True

    return candidate.strip(), round(best_score, 3), False


# ============================================================
# STATE MACHINE CARRY-FORWARD (dipakai per halaman, berurutan)
# ============================================================

class VillageCarryState:
    """
    Pembungkus state kecil untuk carry-forward nama desa antar halaman,
    dengan batas jumlah halaman (MAX_VILLAGE_CARRY_PAGES) supaya desa lama
    tidak "menular" tanpa batas ke bagian dokumen yang sudah pindah topik
    (mis. lampiran tanda tangan / daftar hadir di akhir dokumen).
    """

    def __init__(self, roster=None, similarity_min=DESA_SIMILARITY_MIN_DEFAULT,
                 max_carry_pages=MAX_VILLAGE_CARRY_PAGES_DEFAULT):
        self.roster = roster or []
        self.similarity_min = similarity_min
        self.max_carry_pages = max_carry_pages
        self.current_desa = ""
        self.current_confidence = 0.0
        self.carry_count = 0

    def resolve(self, raw_candidate_from_page):
        """
        Panggil untuk setiap halaman secara berurutan.

        Returns
        -------
        dict dengan keys: kelurahan, confidence, sumber
            sumber in {"LANGSUNG", "DICOCOKKAN", "DIWARISI", "TIDAK_DITEMUKAN"}
        """
        if raw_candidate_from_page:
            nama, skor, cocok = match_to_roster(
                raw_candidate_from_page, self.roster, self.similarity_min
            )
            self.current_desa = nama
            self.current_confidence = skor
            self.carry_count = 0
            return {
                "kelurahan": nama,
                "confidence": skor,
                "sumber": "LANGSUNG" if cocok and skor >= 0.999 else (
                    "DICOCOKKAN" if cocok else "LANGSUNG_TIDAK_DIKENALI"
                ),
            }

        # Tidak ada bacaan desa eksplisit di halaman ini -> warisi.
        if self.current_desa:
            self.carry_count += 1
            if self.carry_count > self.max_carry_pages:
                self.current_desa = ""
                self.current_confidence = 0.0
                return {"kelurahan": "", "confidence": 0.0, "sumber": "TIDAK_DITEMUKAN"}

            # Confidence menurun perlahan makin jauh dari halaman sumber asli,
            # supaya tim tahu kalau ini sudah "warisan lama".
            decayed = max(0.5, self.current_confidence - 0.01 * self.carry_count)
            return {
                "kelurahan": self.current_desa,
                "confidence": round(decayed, 3),
                "sumber": "DIWARISI",
            }

        return {"kelurahan": "", "confidence": 0.0, "sumber": "TIDAK_DITEMUKAN"}
