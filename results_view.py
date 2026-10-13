import pandas as pd
import streamlit as st


def _merge_village_votes(parties, ranges):
    df = parties.copy()
    cols = ["kelurahan", "suara_sah", "suara_tidak_sah", "total_suara"]
    if "kelurahan" in df.columns and all(c in ranges.columns for c in cols):
        votes = ranges[cols].drop_duplicates(subset=["kelurahan"])
        df = df.drop(columns=[c for c in cols[1:] if c in df.columns])
        df = df.merge(votes, on="kelurahan", how="left")

    first = ["kelurahan", "nama_partai", "no_partai", "suara_akhir_partai",
             "suara_sah", "suara_tidak_sah", "total_suara", "halaman",
             "provinsi", "dapil", "kab_kota", "kecamatan", "status_validasi"]
    ordered = [c for c in first if c in df.columns]
    rest = [c for c in df.columns if c not in ordered]
    df = df[ordered + rest]

    if "halaman" in df.columns and "status_validasi" in df.columns:
        bad = df["status_validasi"].astype(str).str.strip().ne("OK")
        df.loc[bad, "halaman"] = df.loc[bad, "halaman"].apply(
            lambda v: f"^ {v}" if pd.notna(v) and not str(v).startswith("^") else v
        )
    return df


def render_results(result, show_tps=True):
    summary = result.get("summary", pd.DataFrame())
    ranges = result.get("ranges", pd.DataFrame())
    parties = result.get("parties", pd.DataFrame())
    validation = result.get("validation", pd.DataFrame())
    tps = result.get("tps", pd.DataFrame())

    tabs = st.tabs(["Ringkasan", "Suara Partai", "Validasi", "Per TPS"])

    with tabs[0]:
        if not summary.empty:
            c1, c2, c3 = st.columns(3)
            num = lambda col: int(pd.to_numeric(summary.get(col), errors="coerce").fillna(0).sum())
            c1.metric("Total Halaman", f"{num('jumlah_halaman'):,}")
            c2.metric("Kelurahan/Desa", f"{num('jumlah_kelurahan'):,}")
            c3.metric("TPS", f"{num('jumlah_tps'):,}")
            st.dataframe(summary, use_container_width=True, hide_index=True)
        if not ranges.empty:
            st.markdown("**Rentang halaman per kelurahan/desa**")
            st.dataframe(ranges, use_container_width=True, hide_index=True)

    with tabs[1]:
        if parties.empty:
            st.info("Belum ada data suara partai.")
        else:
            view = _merge_village_votes(parties, ranges)
            st.dataframe(view, use_container_width=True, hide_index=True)
            if "status_validasi" in view.columns:
                n = int(view["status_validasi"].astype(str).str.strip().ne("OK").sum())
                if n:
                    st.warning(f"{n:,} data perlu dilihat ulang (ditandai ^ di kolom halaman).")

    with tabs[2]:
        if validation.empty or "status" not in validation.columns:
            st.info("Belum ada data validasi.")
        else:
            bad = validation[validation["status"].astype(str).str.contains("PERLU DICEK", na=False)]
            if bad.empty:
                st.success("Semua data lolos validasi dasar.")
            else:
                st.warning(f"{len(bad):,} data perlu diperiksa.")
                st.dataframe(bad, use_container_width=True, hide_index=True)

    with tabs[3]:
        if show_tps and not tps.empty:
            st.dataframe(tps, use_container_width=True, hide_index=True)
        else:
            st.info("Tidak ada data per TPS.")