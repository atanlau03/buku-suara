"""Export hasil OCR KPU ke Excel sesuai struktur rekap yang diminta."""
import io
import re
from collections import OrderedDict

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

EXPORT_COLUMNS = [
    "kelurahan",
    "nama_partai",
    "suara_akhir_partai",
    "suara_sah",
    "suara_tidak_sah",
    "total_suara",
    "provinsi",
    "dapil",
    "kab_kota",
    "kecamatan",
    "partai",
]


def _text(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _num(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)) or _text(v) == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _safe_sheet_name(name, used):
    name = _text(name) or "DAPIL_TIDAK_TERBACA"
    name = re.sub(r"[\\/*?:\[\]]", "-", name)
    name = name[:31] or "DAPIL"
    base = name
    i = 2
    while name in used:
        suffix = f" ({i})"
        name = (base[:31-len(suffix)] + suffix)
        i += 1
    used.add(name)
    return name


def _first_value(df, col):
    if df is None or df.empty or col not in df.columns:
        return ""
    for v in df[col].tolist():
        if _text(v):
            return _text(v)
    return ""


def _build_file_rows(file_name, result):
    parties = result.get("parties", pd.DataFrame())
    ranges = result.get("ranges", pd.DataFrame())
    tps = result.get("tps", pd.DataFrame())

    if not isinstance(parties, pd.DataFrame):
        parties = pd.DataFrame(parties)
    if not isinstance(ranges, pd.DataFrame):
        ranges = pd.DataFrame(ranges)
    if not isinstance(tps, pd.DataFrame):
        tps = pd.DataFrame(tps)

    if parties.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS), pd.DataFrame(), "DAPIL_TIDAK_TERBACA"

    # Rekap desa adalah sumber resmi untuk suara_sah/tidak_sah/total_suara.
    vote_cols = ["kelurahan", "suara_sah", "suara_tidak_sah", "total_suara"]
    if not ranges.empty:
        rv = ranges.copy()
        for c in vote_cols[1:]:
            if c in rv.columns:
                rv[c] = rv[c].apply(_num)
        rv = rv[vote_cols].drop_duplicates(subset=["kelurahan"], keep="first")
    else:
        rv = pd.DataFrame(columns=vote_cols)

    p = parties.copy()
    for c in ["partai", "suara_akhir_partai"]:
        if c in p.columns:
            p[c] = p[c].apply(_num)
    for c in ["kelurahan", "nama_partai", "provinsi", "dapil", "kab_kota", "kecamatan"]:
        if c not in p.columns:
            p[c] = ""
        p[c] = p[c].map(_text)

    p = p.merge(rv, on="kelurahan", how="left", suffixes=("", "_rekap"))
    dapil = _first_value(p, "dapil") or _first_value(tps, "dapil")

    rows = []
    validations = []

    # Urutan: file -> kelurahan sesuai kemunculan -> nomor partai.
    village_order = list(dict.fromkeys(p["kelurahan"].tolist()))
    for village in village_order:
        vp = p[p["kelurahan"] == village].copy()
        vp = vp.sort_values(by=["partai"], kind="stable", na_position="last")

        sah = _num(vp["suara_sah"].iloc[0]) if not vp.empty else None
        tidak = _num(vp["suara_tidak_sah"].iloc[0]) if not vp.empty else None
        total = _num(vp["total_suara"].iloc[0]) if not vp.empty else None

        # Deteksi duplikat partai TANPA menjumlahkannya.
        dup_count = 0
        conflicts = []
        usable_party_values = []
        for party_no, grp in vp.groupby("partai", dropna=False, sort=False):
            vals = [x for x in grp["suara_akhir_partai"].tolist() if x is not None]
            if len(grp) > 1:
                dup_count += len(grp) - 1
                unique_vals = sorted(set(vals))
                if len(unique_vals) > 1:
                    conflicts.append(f"Partai {party_no}: {unique_vals}")
            if vals:
                # Nilai identik berulang dianggap duplikat, hanya dihitung sekali.
                usable_party_values.append(vals[0])

        sum_partai = sum(usable_party_values) if usable_party_values else None
        calc_total = sah + tidak if sah is not None and tidak is not None else None
        status_total = "OK" if calc_total is not None and total is not None and calc_total == total else "PERLU DICEK"
        status_partai = "OK" if sum_partai is not None and sah is not None and sum_partai == sah else "PERLU DICEK"
        status_dup = "OK" if not conflicts and dup_count == 0 else ("DUPLIKAT IDENTIK" if not conflicts else "KONFLIK DUPLIKAT")

        status = "OK" if status_total == "OK" and status_partai == "OK" and not conflicts else "PERLU DICEK"
        catatan = []
        if dup_count:
            catatan.append(f"{dup_count} baris partai duplikat; tidak dijumlahkan")
        if conflicts:
            catatan.append("; ".join(conflicts))
        if status_total != "OK":
            catatan.append("suara_sah + suara_tidak_sah != total_suara")
        if status_partai != "OK":
            catatan.append("jumlah suara akhir partai != suara_sah")

        validations.append({
            "file_pdf": file_name,
            "kelurahan": village,
            "dapil": dapil,
            "suara_sah": sah,
            "suara_tidak_sah": tidak,
            "total_suara": total,
            "hasil_sah_plus_tidak_sah": calc_total,
            "jumlah_suara_akhir_partai_unik": sum_partai,
            "selisih_dengan_sah": (sum_partai - sah) if sum_partai is not None and sah is not None else None,
            "duplikat_partai": dup_count,
            "status_total": status_total,
            "status_partai": status_partai,
            "status": status,
            "catatan": " | ".join(catatan) if catatan else "Tidak ada konflik logika",
        })

        for _, r in vp.iterrows():
            rows.append({
                "kelurahan": village,
                "nama_partai": _text(r.get("nama_partai")),
                "suara_akhir_partai": _num(r.get("suara_akhir_partai")),
                "suara_sah": sah,
                "suara_tidak_sah": tidak,
                "total_suara": total,
                "provinsi": _text(r.get("provinsi")),
                "dapil": _text(r.get("dapil")) or dapil,
                "kab_kota": _text(r.get("kab_kota")),
                "kecamatan": _text(r.get("kecamatan")),
                "partai": _num(r.get("partai")),
            })

    return pd.DataFrame(rows, columns=EXPORT_COLUMNS), pd.DataFrame(validations), dapil


def build_excel(file_results):
    """file_results = [(nama_file_pdf, result), ...]."""
    grouped = OrderedDict()
    validation_frames = []
    file_info = []

    for file_name, result in file_results:
        df, vdf, dapil = _build_file_rows(file_name, result)
        key = _text(dapil) or "DAPIL_TIDAK_TERBACA"
        grouped.setdefault(key, []).append((file_name, df))
        if not vdf.empty:
            validation_frames.append(vdf)
        file_info.append({
            "file_pdf": file_name,
            "dapil": key,
            "jumlah_baris_export": len(df),
            "status": "OK" if not df.empty else "TIDAK ADA DATA PARTAI",
        })

    output = io.BytesIO()
    wb = Workbook()
    default = wb.active
    wb.remove(default)
    used = set()

    header_fill = PatternFill("solid", fgColor="1F4E78")
    separator_fill = PatternFill("solid", fgColor="D9EAF7")
    warning_fill = PatternFill("solid", fgColor="FFF2CC")
    ok_fill = PatternFill("solid", fgColor="E2F0D9")
    thin = Side(style="thin", color="B7B7B7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for dapil, file_items in grouped.items():
        sheet = _safe_sheet_name(dapil, used)
        ws = wb.create_sheet(sheet)
        ws.freeze_panes = "A2"
        ws.append(EXPORT_COLUMNS)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border

        for idx, (file_name, df) in enumerate(file_items):
            if idx > 0:
                row = ws.max_row + 1
                ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(EXPORT_COLUMNS))
                cell = ws.cell(row=row, column=1, value=f"PEMBATAS FILE PDF — {file_name}")
                cell.fill = separator_fill
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center")
                cell.border = border
            elif len(file_items) == 1:
                # Tetap beri metadata sumber sebagai komentar di header, bukan kolom tambahan.
                ws["A1"].comment = __import__('openpyxl').comments.Comment(f"Sumber PDF: {file_name}", "KPU OCR")

            for _, r in df.iterrows():
                ws.append([r.get(c, "") for c in EXPORT_COLUMNS])

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = border
                cell.alignment = Alignment(vertical="center")

        # Format angka: kolom C-F dan K.
        for row in range(2, ws.max_row + 1):
            for col in [3, 4, 5, 6, 11]:
                ws.cell(row, col).number_format = '#,##0'

        widths = [24, 34, 20, 15, 20, 16, 16, 18, 24, 24, 10]
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.auto_filter.ref = f"A1:K{ws.max_row}"

    # Sheet validasi selalu ada agar pengguna bisa melihat konflik angka.
    wv = wb.create_sheet(_safe_sheet_name("VALIDASI", used))
    if validation_frames:
        v_all = pd.concat(validation_frames, ignore_index=True)
    else:
        v_all = pd.DataFrame(columns=["file_pdf", "kelurahan", "dapil", "suara_sah", "suara_tidak_sah", "total_suara", "hasil_sah_plus_tidak_sah", "jumlah_suara_akhir_partai_unik", "selisih_dengan_sah", "duplikat_partai", "status_total", "status_partai", "status", "catatan"])
    for c in v_all.columns:
        pass
    wv.append(list(v_all.columns))
    for cell in wv[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for _, r in v_all.iterrows():
        wv.append([r.get(c, "") for c in v_all.columns])
    for row in wv.iter_rows(min_row=2):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        status_cell = row[12] if len(row) > 12 else None
        if status_cell:
            status_cell.fill = ok_fill if status_cell.value == "OK" else warning_fill
    for i, c in enumerate(v_all.columns, 1):
        wv.column_dimensions[get_column_letter(i)].width = 24 if c not in {"catatan"} else 55
    wv.freeze_panes = "A2"
    if wv.max_row > 1:
        wv.auto_filter.ref = f"A1:{get_column_letter(wv.max_column)}{wv.max_row}"

    wi = wb.create_sheet(_safe_sheet_name("INFO", used))
    wi.append(["keterangan", "nilai"])
    for c in wi[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
        c.border = border
    wi.append(["Aturan pengulangan suara_sah/suara_tidak_sah/total_suara", "Nilai boleh berulang pada setiap partai karena merupakan rekap kelurahan; tidak dijumlahkan per baris."])
    wi.append(["Validasi total", "suara_sah + suara_tidak_sah harus sama dengan total_suara."])
    wi.append(["Validasi partai", "Jumlah suara akhir partai unik harus sama dengan suara_sah."])
    wi.append(["Duplikat partai", "Baris identik tidak dijumlahkan. Jika nomor partai memiliki nilai berbeda, ditandai KONFLIK DUPLIKAT."])
    wi.append(["Pemisahan PDF", "PDF dengan dapil sama berada pada sheet yang sama dan diberi baris pembatas berwarna. Dapil berbeda dibuat sheet baru."])
    wi.column_dimensions["A"].width = 48
    wi.column_dimensions["B"].width = 100
    for row in wi.iter_rows():
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(output)
    output.seek(0)
    return output.getvalue()
