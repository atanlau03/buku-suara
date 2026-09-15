import streamlit as st
from database import get_full_dataframe
from seat_calculation import sainte_lague, apply_parliamentary_threshold, allocate_candidates
from ui_theme import apply_theme, header, note

st.set_page_config(page_title="Buku Suara — Hitung Kursi", page_icon="🗳️", layout="wide")
apply_theme()
header("Hitung Kursi — Lanjutan", "Alokasi kursi metode Sainte-Lague & caleg terpilih (opsional)")

note(
    "Fitur ini opsional dan terpisah dari alur utama. Alokasi kursi antar partai "
    "cukup pakai total suara partai per dapil. Tapi untuk menentukan <b>caleg "
    "terpilih</b>, data yang diupload harus berisi suara per caleg (bukan cuma "
    "total partai) — kalau tidak ada, bagian caleg akan kosong."
)

df = get_full_dataframe()
if df.empty:
    st.info("Belum ada data. Masukkan data dulu lewat menu Upload Data atau Upload PDF (OCR).")
    st.stop()

daftar_dapil = sorted(df["dapil"].unique())
dapil_pilihan = st.selectbox("Pilih dapil", daftar_dapil)
df_dapil = df[df["dapil"] == dapil_pilihan]

col1, col2 = st.columns(2)
total_kursi = col1.number_input("Jumlah kursi diperebutkan di dapil ini", min_value=1, value=6, step=1)
pakai_threshold = col2.checkbox("Terapkan ambang batas parlemen nasional (khusus DPR RI)")

threshold_pct = 0.0
if pakai_threshold:
    threshold_pct = st.number_input("Ambang batas (%)", min_value=0.0, max_value=100.0, value=4.0, step=0.5)
    st.caption(
        "Ambang batas dihitung dari total suara sah NASIONAL (seluruh dapil dalam data), "
        "bukan hanya dapil ini."
    )

votes_per_partai = df_dapil.groupby("partai")["jumlah_suara"].sum().to_dict()

partai_lolos = list(votes_per_partai.keys())
if pakai_threshold:
    votes_nasional = df.groupby("partai")["jumlah_suara"].sum().to_dict()
    partai_lolos = apply_parliamentary_threshold(votes_nasional, threshold_pct)
    tidak_lolos = [p for p in votes_per_partai if p not in partai_lolos]
    if tidak_lolos:
        st.caption(f"Partai tidak lolos ambang batas nasional: {', '.join(tidak_lolos)}")

votes_final = {p: v for p, v in votes_per_partai.items() if p in partai_lolos}

if st.button("Hitung kursi", type="primary"):
    kursi_partai = sainte_lague(votes_final, int(total_kursi))

    st.markdown(f"**Hasil alokasi kursi — {dapil_pilihan}**")
    hasil_tbl = sorted(kursi_partai.items(), key=lambda x: x[1], reverse=True)
    st.dataframe(
        {"Partai": [p for p, _ in hasil_tbl], "Suara": [votes_final[p] for p, _ in hasil_tbl],
         "Kursi": [k for _, k in hasil_tbl]},
        use_container_width=True, hide_index=True,
    )

    suara_caleg = {}
    for partai in votes_final:
        sub = df_dapil[df_dapil["partai"] == partai]
        agg = sub.groupby("caleg")["jumlah_suara"].sum().reset_index()
        suara_caleg[partai] = list(zip(agg["caleg"], agg["jumlah_suara"]))

    caleg_terpilih = allocate_candidates(kursi_partai, suara_caleg)

    st.markdown("**Caleg terpilih (metode suara terbanyak)**")
    ada_caleg = any(daftar for daftar in caleg_terpilih.values())
    if not ada_caleg:
        st.caption(
            "Tidak ada rincian caleg pada data dapil ini — kemungkinan data yang "
            "diupload hanya berisi total suara partai, bukan per caleg."
        )
    for partai, daftar in caleg_terpilih.items():
        if not daftar:
            continue
        st.markdown(f"_{partai}_ — {len(daftar)} kursi")
        st.dataframe(
            {"Nama Caleg": [n for n, _ in daftar], "Jumlah Suara": [v for _, v in daftar]},
            use_container_width=True, hide_index=True,
        )

    st.session_state["hasil_kursi_terakhir"] = {
        "dapil": dapil_pilihan,
        "kursi_partai": kursi_partai,
        "caleg_terpilih": caleg_terpilih,
    }
    st.success("Selesai dihitung. Hasil ini juga tersedia untuk diunduh di menu Laporan.")
