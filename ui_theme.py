"""
ui_theme.py
Identitas visual "Buku Suara" -- terinspirasi dari rupa ledger/rekapitulasi
kertas resmi (garis tabel tipis, angka tabular, tinta arsip), bukan gaya
dashboard SaaS generik.
"""

import streamlit as st
import textwrap

# --- Token warna: tinta arsip di atas kertas ledger ---
INK = "#1C2B33"          # tinta utama (teks, judul)
INK_SOFT = "#4A5A60"     # tinta sekunder (subjudul, caption)
PAPER = "#ECEEE7"        # kertas dasar (sedikit sage, bukan krem hangat)
PAPER_LINE = "#CCD0C4"   # garis tabel / pembatas tipis
STAMP = "#A63A2E"        # tinta stempel (aksen utama, dipakai hemat)
BRASS = "#9C7A32"        # aksen sekunder -- penanda "tertinggi/terpilih"
PANEL = "#F5F6F1"        # panel/kartu (sedikit lebih terang dari PAPER)

FONT_IMPORT = "https://fonts.googleapis.com/css2?family=Spectral:wght@500;600;700&family=Public+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap"


def apply_theme():
    # <link> dipisah dari <style> -- kalau digabung dan ada baris kosong di
    # tengah CSS, parser Markdown berhenti memperlakukannya sebagai HTML mentah
    # begitu ketemu baris kosong, sehingga sisa CSS-nya malah tampil sebagai teks.
    st.markdown(f'<link href="{FONT_IMPORT}" rel="stylesheet">', unsafe_allow_html=True)
    st.markdown(textwrap.dedent(f"""\
    <style>
    html, body, [class*="css"] {{
        font-family: 'Public Sans', sans-serif;
        color: {INK};
    }}
    .stApp {{ background-color: {PAPER}; }}

    h1, h2, h3, .bs-serif {{
        font-family: 'Spectral', serif;
        color: {INK};
        font-weight: 600;
        letter-spacing: 0;
    }}

    /* angka tally -- tabular, monospace */
    .bs-num, div[data-testid="stMetricValue"] {{
        font-family: 'IBM Plex Mono', monospace !important;
        font-variant-numeric: tabular-nums;
        color: {INK} !important;
    }}

    /* Letterhead / kop halaman -- pengganti banner gradien */
    .bs-letterhead {{
        border-top: 3px solid {INK};
        border-bottom: 1px solid {INK};
        padding: 0.65rem 0 0.7rem 0;
        margin-bottom: 1.6rem;
    }}
    .bs-letterhead .bs-title {{
        font-family: 'Spectral', serif;
        font-weight: 700;
        font-size: 1.65rem;
        color: {INK};
        margin: 0;
    }}
    .bs-letterhead .bs-subtitle {{
        font-family: 'Public Sans', sans-serif;
        color: {INK_SOFT};
        font-size: 0.92rem;
        margin-top: 0.15rem;
    }}

    /* Breadcrumb jalur wilayah */
    .bs-breadcrumb {{
        font-family: 'Public Sans', sans-serif;
        font-size: 0.95rem;
        color: {INK_SOFT};
        padding: 0.4rem 0 1rem 0;
        border-bottom: 1px dashed {PAPER_LINE};
        margin-bottom: 1.2rem;
    }}
    .bs-breadcrumb b {{ color: {INK}; }}

    /* Catatan/peringatan -- garis kiri, tanpa kotak kuning generik */
    .bs-note {{
        border-left: 3px solid {STAMP};
        background-color: {PANEL};
        padding: 0.7rem 1rem;
        margin-bottom: 1.1rem;
        font-size: 0.92rem;
        color: {INK};
    }}

    /* Tombol -- persegi tipis, bukan pil membulat */
    div.stButton > button:first-child {{
        background-color: {INK};
        color: {PAPER};
        border-radius: 3px;
        border: 1px solid {INK};
        font-weight: 500;
        padding: 0.45rem 1.1rem;
    }}
    div.stButton > button:first-child:hover {{
        background-color: {STAMP};
        border-color: {STAMP};
        color: white;
    }}
    div.stDownloadButton > button:first-child {{
        background-color: transparent;
        color: {INK};
        border: 1px solid {INK};
        border-radius: 3px;
        font-weight: 500;
    }}
    div.stDownloadButton > button:first-child:hover {{
        border-color: {STAMP};
        color: {STAMP};
    }}

    /* Metrik -- sel ledger, bukan kartu melayang */
    div[data-testid="stMetric"] {{
        background-color: {PANEL};
        border: none;
        border-bottom: 2px solid {INK};
        padding: 0.7rem 0.9rem;
        border-radius: 0;
    }}
    div[data-testid="stMetricLabel"] {{
        font-family: 'Public Sans', sans-serif;
        color: {INK_SOFT};
        font-size: 0.82rem;
    }}

    /* Sidebar -- kertas, garis tunggal (bukan border tebal berwarna) */
    section[data-testid="stSidebar"] {{
        background-color: {PANEL};
        border-right: 1px solid {PAPER_LINE};
    }}

    hr {{ border-color: {PAPER_LINE}; }}
    </style>
    """), unsafe_allow_html=True)


def letterhead(title: str, subtitle: str = ""):
    """Kop halaman ala kepala surat resmi -- pengganti banner gradien."""
    st.markdown(textwrap.dedent(f"""\
    <div class="bs-letterhead">
        <p class="bs-title">{title}</p>
        <p class="bs-subtitle">{subtitle}</p>
    </div>
    """), unsafe_allow_html=True)


# alias supaya kompatibel dengan pemanggilan sebelumnya
def header(title: str, subtitle: str = ""):
    letterhead(title, subtitle)


def breadcrumb(parts):
    """parts: list of (label, value) -- bagian yang value=None/'Semua' dilewati."""
    shown = [v for _, v in parts if v and v != "Semua"]
    trail = " ▸ ".join(shown) if shown else "Seluruh wilayah"
    st.markdown(f'<div class="bs-breadcrumb">Wilayah: <b>{trail}</b></div>', unsafe_allow_html=True)


def note(text: str):
    st.markdown(f'<div class="bs-note">{text}</div>', unsafe_allow_html=True)


# alias lama
def warning_box(text: str):
    note(text)
