import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# ============================================================
# MEMBUAT HASIL EXCEL SEDERHANA
# ============================================================

def buat_hasil_sederhana(result):
    """
    Mengubah hasil OCR menjadi tabel sederhana:

    kelurahan
    halaman berapa sampai berapa
    total tps
    partai
    suara akhir partai
    suara sah
    suara tidak sah
    total suara

    1 baris = 1 partai dalam 1 kelurahan.
    """

    ranges = result.get("ranges", pd.DataFrame())
    tps = result.get("tps", pd.DataFrame())
    parties = result.get("parties", pd.DataFrame())

    # --------------------------------------------------------
    # Jika tidak ada data
    # --------------------------------------------------------

    if ranges is None:
        ranges = pd.DataFrame()

    if tps is None:
        tps = pd.DataFrame()

    if parties is None:
        parties = pd.DataFrame()

    # --------------------------------------------------------
    # Normalisasi nama kolom
    # --------------------------------------------------------

    if not ranges.empty:
        ranges = ranges.copy()

    if not tps.empty:
        tps = tps.copy()

    if not parties.empty:
        parties = parties.copy()

    # --------------------------------------------------------
    # Tentukan kolom kelurahan
    # --------------------------------------------------------

    def cari_kolom(df, pilihan):
        for nama in pilihan:
            if nama in df.columns:
                return nama
        return None

    col_kel_ranges = cari_kolom(
        ranges,
        ["kelurahan", "desa"]
    )

    col_kel_tps = cari_kolom(
        tps,
        ["kelurahan", "desa"]
    )

    col_kel_party = cari_kolom(
        parties,
        ["kelurahan", "desa"]
    )

    # --------------------------------------------------------
    # Jika tidak ada data sama sekali
    # --------------------------------------------------------

    if (
        ranges.empty
        and tps.empty
        and parties.empty
    ):
        return pd.DataFrame(
            columns=[
                "kelurahan",
                "halaman",
                "total tps",
                "partai",
                "suara akhir partai",
                "suara sah",
                "suara tidak sah",
                "total suara",
            ]
        )

    # ========================================================
    # 1. DATA RANGE HALAMAN KELURAHAN
    # ========================================================

    range_data = {}

    if not ranges.empty and col_kel_ranges:

        for _, row in ranges.iterrows():

            kel = str(row.get(col_kel_ranges, "")).strip()

            if not kel:
                continue

            halaman_mulai = row.get(
                "halaman_mulai",
                ""
            )

            halaman_selesai = row.get(
                "halaman_selesai",
                ""
            )

            try:
                halaman_mulai = int(
                    float(halaman_mulai)
                )
            except Exception:
                halaman_mulai = ""

            try:
                halaman_selesai = int(
                    float(halaman_selesai)
                )
            except Exception:
                halaman_selesai = ""

            if halaman_mulai != "" and halaman_selesai != "":
                halaman = f"{halaman_mulai}-{halaman_selesai}"
            else:
                halaman = ""

            range_data[kel] = {
                "halaman": halaman
            }

    # ========================================================
    # 2. HITUNG TOTAL TPS PER KELURAHAN
    # ========================================================

    tps_data = {}

    if not tps.empty and col_kel_tps:

        temp = tps.copy()

        temp[col_kel_tps] = (
            temp[col_kel_tps]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        temp = temp[temp[col_kel_tps] != ""]

        # ----------------------------------------------------
        # Gunakan nomor TPS jika tersedia
        # ----------------------------------------------------

        if "tps" in temp.columns:

            for kel, group in temp.groupby(
                col_kel_tps
            ):

                nomor_tps = (
                    group["tps"]
                    .dropna()
                    .astype(str)
                    .str.strip()
                )

                nomor_tps = nomor_tps[
                    (nomor_tps != "")
                    & (nomor_tps != "0")
                ]

                tps_data[kel] = len(
                    set(nomor_tps.tolist())
                )

        else:

            for kel, group in temp.groupby(
                col_kel_tps
            ):
                tps_data[kel] = len(group)

    # ========================================================
    # 3. HITUNG SUARA SAH / TIDAK SAH / TOTAL
    # ========================================================

    suara_data = {}

    if not tps.empty and col_kel_tps:

        temp = tps.copy()

        temp[col_kel_tps] = (
            temp[col_kel_tps]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        temp = temp[temp[col_kel_tps] != ""]

        # ----------------------------------------------------
        # Konversi angka
        # ----------------------------------------------------

        for col in [
            "suara_sah",
            "suara_tidak_sah",
            "total_suara",
        ]:

            if col in temp.columns:

                temp[col] = pd.to_numeric(
                    temp[col],
                    errors="coerce"
                )

        # ----------------------------------------------------
        # Hilangkan duplikat TPS
        # ----------------------------------------------------

        group_columns = [
            col_kel_tps
        ]

        if "tps" in temp.columns:
            group_columns.append("tps")

        # Jangan langsung sum data halaman yang sama
        # jika OCR menghasilkan duplikasi.
        temp_unique = temp.drop_duplicates(
            subset=group_columns,
            keep="first"
        )

        for kel, group in temp_unique.groupby(
            col_kel_tps
        ):

            sah = (
                group["suara_sah"].sum()
                if "suara_sah" in group.columns
                else 0
            )

            tidak_sah = (
                group["suara_tidak_sah"].sum()
                if "suara_tidak_sah" in group.columns
                else 0
            )

            total = (
                group["total_suara"].sum()
                if "total_suara" in group.columns
                else 0
            )

            # ------------------------------------------------
            # Jika total tidak terbaca,
            # hitung dari sah + tidak sah
            # ------------------------------------------------

            if total == 0 and (
                sah != 0 or tidak_sah != 0
            ):
                total = sah + tidak_sah

            suara_data[kel] = {
                "suara_sah": int(sah),
                "suara_tidak_sah": int(tidak_sah),
                "total_suara": int(total),
            }

    # ========================================================
    # 4. SUARA AKHIR PARTAI
    # ========================================================

    party_data = pd.DataFrame()

    if not parties.empty:

        party_data = parties.copy()

        # ----------------------------------------------------
        # Cari kolom total suara partai
        # ----------------------------------------------------

        if "total_suara_partai_kelurahan" in party_data.columns:

            col_suara_partai = (
                "total_suara_partai_kelurahan"
            )

        elif "jumlah_suara" in party_data.columns:

            col_suara_partai = "jumlah_suara"

        else:

            col_suara_partai = None

        # ----------------------------------------------------
        # Cari kolom nama partai
        # ----------------------------------------------------

        col_partai = cari_kolom(
            party_data,
            ["partai", "nama_partai"]
        )

        # ----------------------------------------------------
        # Cari kolom kelurahan
        # ----------------------------------------------------

        col_kel = cari_kolom(
            party_data,
            ["kelurahan", "desa"]
        )

        if (
            col_suara_partai
            and col_partai
            and col_kel
        ):

            party_data[col_suara_partai] = pd.to_numeric(
                party_data[col_suara_partai],
                errors="coerce"
            ).fillna(0)

            # ------------------------------------------------
            # Gabungkan jika ada partai yang muncul
            # beberapa kali
            # ------------------------------------------------

            group_cols = [
                col_kel,
                col_partai
            ]

            # Pertahankan nomor partai jika tersedia
            if "nomor_urut_partai" in party_data.columns:
                group_cols.insert(
                    1,
                    "nomor_urut_partai"
                )

            party_data = (
                party_data
                .groupby(
                    group_cols,
                    dropna=False
                )[col_suara_partai]
                .sum()
                .reset_index()
            )

    # ========================================================
    # 5. BUAT TABEL FINAL
    # ========================================================

    hasil = []

    if not party_data.empty:

        for _, row in party_data.iterrows():

            kel = str(
                row.get(
                    col_kel,
                    ""
                )
            ).strip()

            if not kel:
                continue

            partai = str(
                row.get(
                    col_partai,
                    ""
                )
            ).strip()

            suara_partai = row.get(
                col_suara_partai,
                0
            )

            try:
                suara_partai = int(
                    float(suara_partai)
                )
            except Exception:
                suara_partai = 0

            # ------------------------------------------------
            # Range halaman
            # ------------------------------------------------

            halaman = range_data.get(
                kel,
                {}
            ).get(
                "halaman",
                ""
            )

            # ------------------------------------------------
            # Total TPS
            # ------------------------------------------------

            total_tps = tps_data.get(
                kel,
                0
            )

            # ------------------------------------------------
            # Suara kelurahan
            # ------------------------------------------------

            suara = suara_data.get(
                kel,
                {}
            )

            suara_sah = suara.get(
                "suara_sah",
                0
            )

            suara_tidak_sah = suara.get(
                "suara_tidak_sah",
                0
            )

            total_suara = suara.get(
                "total_suara",
                0
            )

            # ------------------------------------------------
            # Jika total belum ada
            # ------------------------------------------------

            if total_suara == 0:

                total_suara = (
                    suara_sah
                    + suara_tidak_sah
                )

            hasil.append({
                "kelurahan": kel,
                "halaman": halaman,
                "total tps": total_tps,
                "partai": partai,
                "suara akhir partai": suara_partai,
                "suara sah": suara_sah,
                "suara tidak sah": suara_tidak_sah,
                "total suara": total_suara,
            })

    # ========================================================
    # 6. FALLBACK
    # ========================================================

    # Jika data partai belum terbaca,
    # tetap buat informasi kelurahan.

    if not hasil:

        semua_kelurahan = set()

        semua_kelurahan.update(
            range_data.keys()
        )

        semua_kelurahan.update(
            tps_data.keys()
        )

        semua_kelurahan.update(
            suara_data.keys()
        )

        for kel in sorted(semua_kelurahan):

            halaman = range_data.get(
                kel,
                {}
            ).get(
                "halaman",
                ""
            )

            total_tps = tps_data.get(
                kel,
                0
            )

            suara = suara_data.get(
                kel,
                {}
            )

            sah = suara.get(
                "suara_sah",
                0
            )

            tidak = suara.get(
                "suara_tidak_sah",
                0
            )

            total = suara.get(
                "total_suara",
                sah + tidak
            )

            hasil.append({
                "kelurahan": kel,
                "halaman": halaman,
                "total tps": total_tps,
                "partai": "",
                "suara akhir partai": 0,
                "suara sah": sah,
                "suara tidak sah": tidak,
                "total suara": total,
            })

    # ========================================================
    # DATAFRAME FINAL
    # ========================================================

    df = pd.DataFrame(hasil)

    columns = [
        "kelurahan",
        "halaman",
        "total tps",
        "partai",
        "suara akhir partai",
        "suara sah",
        "suara tidak sah",
        "total suara",
    ]

    for col in columns:

        if col not in df.columns:
            df[col] = ""

    df = df[columns]

    # Urutkan
    if not df.empty:

        df = df.sort_values(
            by=[
                "kelurahan",
                "partai"
            ],
            kind="stable"
        )

    return df.reset_index(drop=True)


# ============================================================
# EXPORT KE EXCEL
# ============================================================

def export_excel_sederhana(result):

    df = buat_hasil_sederhana(result)

    output = io.BytesIO()

    wb = Workbook()

    ws = wb.active
    ws.title = "HASIL PEMINDAIAN"

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    headers = [
        "kelurahan",
        "halaman",
        "total tps",
        "partai",
        "suara akhir partai",
        "suara sah",
        "suara tidak sah",
        "total suara",
    ]

    ws.append(headers)

    # --------------------------------------------------------
    # STYLE HEADER
    # --------------------------------------------------------

    thin = Side(
        style="thin"
    )

    border = Border(
        left=thin,
        right=thin,
        top=thin,
        bottom=thin
    )

    for cell in ws[1]:

        cell.font = Font(
            bold=True
        )

        cell.alignment = Alignment(
            horizontal="center",
            vertical="center"
        )

        cell.border = border

    # --------------------------------------------------------
    # DATA
    # --------------------------------------------------------

    for _, row in df.iterrows():

        ws.append([
            row["kelurahan"],
            row["halaman"],
            row["total tps"],
            row["partai"],
            row["suara akhir partai"],
            row["suara sah"],
            row["suara tidak sah"],
            row["total suara"],
        ])

    # --------------------------------------------------------
    # FORMAT DATA
    # --------------------------------------------------------

    for row in ws.iter_rows(
        min_row=2,
        max_row=ws.max_row
    ):

        for cell in row:

            cell.border = border

            cell.alignment = Alignment(
                vertical="center"
            )

    # --------------------------------------------------------
    # FORMAT ANGKA
    # --------------------------------------------------------

    for row in range(
        2,
        ws.max_row + 1
    ):

        for col in [
            3,
            5,
            6,
            7,
            8
        ]:

            ws.cell(
                row=row,
                column=col
            ).number_format = '#,##0'

    # --------------------------------------------------------
    # LEBAR KOLOM
    # --------------------------------------------------------

    widths = {
        1: 20,
        2: 18,
        3: 12,
        4: 25,
        5: 22,
        6: 18,
        7: 22,
        8: 18,
    }

    for col, width in widths.items():

        ws.column_dimensions[
            get_column_letter(col)
        ].width = width

    # --------------------------------------------------------
    # FREEZE HEADER
    # --------------------------------------------------------

    ws.freeze_panes = "A2"

    # --------------------------------------------------------
    # FILTER
    # --------------------------------------------------------

    if ws.max_row >= 2:

        ws.auto_filter.ref = (
            f"A1:H{ws.max_row}"
        )

    # --------------------------------------------------------
    # SIMPAN
    # --------------------------------------------------------

    wb.save(output)

    output.seek(0)

    return output.getvalue()