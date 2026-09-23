"""
seat_calculation.py
Implementasi metode Sainte-Lague (digunakan KPU sejak Pemilu 2019) untuk
konversi suara sah menjadi kursi DPR/DPRD per dapil, dan alokasi kursi ke
caleg berdasarkan metode suara terbanyak (sesuai Putusan MK No. 22-24/2008).

Catatan:
- Ambang batas parlemen (parliamentary threshold) hanya berlaku untuk
  DPR RI (nasional), dihitung dari total suara sah nasional -- bukan per dapil.
  Untuk DPRD Provinsi/Kabupaten-Kota tidak ada ambang batas.
- Fitur ini bersifat opsional: perhitungan kursi & caleg terpilih butuh data
  suara per CALEG, bukan cuma total per partai. Kalau data yang diupload
  hanya berisi total suara partai per TPS, hasil "caleg terpilih" akan kosong
  (karena tidak ada baris caleg untuk dihitung), tapi alokasi kursi ANTAR
  PARTAI tetap bisa dihitung dari total suara partai.
"""

from typing import Dict, List, Tuple


def sainte_lague(votes_per_partai: Dict[str, int], total_kursi: int) -> Dict[str, int]:
    """
    Alokasi kursi antar partai dalam SATU dapil menggunakan metode Sainte-Lague murni
    (pembagi ganjil: 1, 3, 5, 7, ...).
    """
    kursi = {p: 0 for p in votes_per_partai}
    kandidat_partai = [p for p, v in votes_per_partai.items() if v > 0]

    for _ in range(total_kursi):
        best_partai = None
        best_quotient = -1
        for p in kandidat_partai:
            divisor = 2 * kursi[p] + 1
            quotient = votes_per_partai[p] / divisor
            if quotient > best_quotient:
                best_quotient = quotient
                best_partai = p
        if best_partai is None:
            break
        kursi[best_partai] += 1

    return kursi


def apply_parliamentary_threshold(
    votes_per_partai_nasional: Dict[str, int], threshold_pct: float
) -> List[str]:
    """Daftar nama partai yang LOLOS ambang batas parlemen nasional (khusus DPR RI)."""
    total = sum(votes_per_partai_nasional.values())
    if total == 0 or threshold_pct <= 0:
        return list(votes_per_partai_nasional.keys())
    ambang = total * (threshold_pct / 100.0)
    return [p for p, v in votes_per_partai_nasional.items() if v >= ambang]


def allocate_candidates(
    kursi_per_partai: Dict[str, int],
    suara_caleg: Dict[str, List[Tuple[str, int]]],
) -> Dict[str, List[Tuple[str, int]]]:
    """
    Menentukan caleg terpilih per partai berdasarkan metode suara terbanyak.
    suara_caleg: {partai: [(nama_caleg, jumlah_suara), ...]} -- baris "(Suara Partai)"
    otomatis dikecualikan dari daftar caleg yang bisa duduk.
    """
    hasil = {}
    for partai, n_kursi in kursi_per_partai.items():
        if n_kursi <= 0:
            hasil[partai] = []
            continue
        caleg_list = [
            (nama, suara) for nama, suara in suara_caleg.get(partai, [])
            if nama != "(Suara Partai)"
        ]
        caleg_list.sort(key=lambda x: x[1], reverse=True)
        hasil[partai] = caleg_list[:n_kursi]
    return hasil
