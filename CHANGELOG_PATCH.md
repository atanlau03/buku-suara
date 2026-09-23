# Changelog Patch — Perbaikan Deteksi Desa & Keamanan

Patch ini menggabungkan kekuatan `sistem_pileg` (bacaan per-partai per-TPS
lewat Gemini) dengan kekuatan sistem `OCR_Rekap_Kelurahan` milik teman
(deteksi nama desa yang tahan typo OCR), plus beberapa perbaikan baru yang
tidak ada di kedua sistem asal.

## File baru

- **`village_resolver.py`** — modul inti resolusi nama desa/kelurahan:
  - Normalisasi tahan typo OCR (1↔I, 0↔O, dst) — diadaptasi dari sistem teman.
  - `_strip_ocr_dot_leader_noise()` — **bug fix baru**: membuang sampah
    karakter dari garis titik-titik formulir KPU (mis. teks
    `"KELURAHAN/DESA ....: GEDUNG KARYA JITU"` yang di-OCR jadi
    `"0.00.-000000EE0000 GEDUNG KARYA JITU"`) sebelum nama desa dipakai.
  - `build_auto_roster()` — daftar nama desa kanonis dibangun **otomatis
    per file PDF** dari bacaan-bacaan langsung berconfidence tinggi.
    Ini menggantikan pendekatan sistem teman yang daftarnya di-hardcode
    per kecamatan (dan salah kota untuk PDF non-Bogor).
  - `match_to_roster()` — cocokkan bacaan lemah/typo ke roster via fuzzy
    matching (SequenceMatcher + token overlap + tail-matching).
  - `VillageCarryState` — carry-forward nama desa antar halaman dengan
    batas jumlah halaman, plus label sumber `LANGSUNG` / `DIWARISI` /
    `DICOCOKKAN` / `TIDAK_DITEMUKAN` untuk audit.

## File yang diubah

### `pdf_hybrid_engine.py` (engine Gemini/Hybrid AI)
- Import `village_resolver`.
- `PROMPT` → `PROMPT_TEMPLATE` + `_build_prompt()`: prompt yang dikirim
  ke Gemini sekarang disisipi **konteks wilayah dari halaman-halaman
  sebelumnya**, dan diberi instruksi eksplisit untuk memakai konteks itu
  bila halaman ini tidak mencantumkan kelurahan/desa secara eksplisit
  (persis pola dokumen KPU: hanya halaman pertama tiap desa yang
  mencantumkannya).
- `_call()` sekarang menerima parameter `known_context`.
- Loop `proses_pdf_batch_hybrid()`: pewarisan wilayah yang lama (naif,
  copy dari halaman sebelumnya tanpa batas) **dihapus dari dalam loop**.
  Semua halaman disimpan mentah dulu.
- Fungsi baru `_resolve_wilayah_dua_tahap()`: dipanggil SETELAH semua
  halaman selesai dibaca. Pass 1 bangun roster otomatis, Pass 2 jalankan
  carry-forward + fuzzy match per halaman secara berurutan.
- Validasi (`05_VALIDASI` di Excel) ditambah kolom `kelurahan_sumber`,
  dan flag baru `LANGSUNG_TIDAK_DIKENALI` (nama desa terbaca tapi tidak
  cocok dengan desa lain di file ini — kemungkinan typo OCR parah,
  perlu dicek manual).

### `local_ocr_engine.py` (engine lokal Tesseract, fallback)
- `DESA_KANONIS` hardcode (isinya nama desa **Kota Bogor** — tidak
  relevan untuk dokumen wilayah lain) **dihapus**.
- `extract_wilayah()` sekarang hanya mengembalikan kandidat mentah
  (`kelurahan_mentah`), tidak langsung mencocokkan ke daftar tetap.
- `process_pdf_local_engine()` diubah pola dua-tahap yang sama dengan
  `pdf_hybrid_engine.py`: kumpulkan semua halaman dulu → bangun roster
  otomatis → carry-forward + fuzzy match.
- Kolom `kelurahan_sumber` ditambahkan ke tabel validasi.

### `ocr_utils.py` (perbaikan keamanan)
- **API key Gemini yang di-hardcode di source code dihapus**, diganti
  `os.getenv("GEMINI_API_KEY", "")`.
- ⚠️ **Tindakan wajib**: API key lama (di file ini maupun di
  `.streamlit/secrets.toml`) harus dianggap **sudah bocor** karena
  pernah ter-zip dan terkirim keluar. Segera revoke/rotate di
  https://aistudio.google.com/ lalu isi key yang baru lewat env var
  atau `.streamlit/secrets.toml` (yang sudah ada di `.gitignore`).

### `.streamlit/secrets.toml`
- File asli berisi API key nyata **tidak disertakan** di paket ini.
- Disediakan `.streamlit/secrets.toml.example` sebagai template —
  salin jadi `secrets.toml` lalu isi API key kamu sendiri.

## Yang TIDAK diubah

`app.py`, `database.py`, `seat_calculation.py`, `ui_theme.py`, dan semua
file di `pages/` disertakan apa adanya (tidak ada perubahan logika),
supaya struktur project tetap lengkap dan langsung bisa dijalankan.

## Cara pakai

1. Salin `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml`,
   isi `GEMINI_API_KEY` dengan key BARU (bukan yang lama, karena sudah
   bocor).
2. `pip install -r requirements.txt`
3. `streamlit run app.py`
4. Di halaman "Pemindai Data", jalankan seperti biasa — kolom
   `kelurahan_sumber` di tabel validasi akan menunjukkan halaman mana
   yang desanya terbaca langsung vs diwarisi vs dicocokkan otomatis,
   supaya bisa diperiksa manual bila perlu.
