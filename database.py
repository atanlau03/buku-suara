"""
database.py
Model & helper akses database untuk Buku Suara.

Struktur data disederhanakan sesuai kebutuhan dasar: satu baris = satu partai
di satu TPS. Kolom "dapil" & rincian caleg bersifat OPSIONAL, hanya dipakai
kalau memakai fitur lanjutan "Hitung Kursi".

Hierarki wilayah: Provinsi -> Kabupaten/Kota -> Kecamatan -> Kelurahan/Desa -> TPS
Setiap baris hasil suara dicatat asalnya (sumber & nama file) supaya bisa
ditelusuri/dihapus per file kalau ada yang salah upload.
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, UniqueConstraint, DateTime, text
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
import pandas as pd

DB_PATH = "sqlite:///pileg.db"
engine = create_engine(DB_PATH, echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

SUARA_PARTAI_LABEL = "(Suara Partai)"


class Wilayah(Base):
    __tablename__ = "wilayah"
    id = Column(Integer, primary_key=True)
    provinsi = Column(String, nullable=False)
    dapil = Column(String, nullable=False, default="-")
    kab_kota = Column(String, nullable=False)
    kecamatan = Column(String, nullable=False)
    kelurahan = Column(String, nullable=False)
    tps = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provinsi", "dapil", "kab_kota", "kecamatan", "kelurahan", "tps",
            name="uq_wilayah"
        ),
    )
    hasil = relationship("HasilSuara", back_populates="wilayah")


class Partai(Base):
    __tablename__ = "partai"
    id = Column(Integer, primary_key=True)
    nomor_urut = Column(Integer, nullable=False, default=0)
    nama_partai = Column(String, nullable=False, unique=True)

    caleg_list = relationship("Caleg", back_populates="partai")


class Caleg(Base):
    """Opsional -- hanya terisi kalau upload menyertakan rincian caleg (untuk Hitung Kursi)."""
    __tablename__ = "caleg"
    id = Column(Integer, primary_key=True)
    partai_id = Column(Integer, ForeignKey("partai.id"), nullable=False)
    dapil = Column(String, nullable=False, default="-")
    nomor_urut = Column(Integer, nullable=False, default=0)
    nama_caleg = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("partai_id", "dapil", "nomor_urut", "nama_caleg", name="uq_caleg"),
    )
    partai = relationship("Partai", back_populates="caleg_list")
    hasil = relationship("HasilSuara", back_populates="caleg")


class HasilSuara(Base):
    __tablename__ = "hasil_suara"
    id = Column(Integer, primary_key=True)
    wilayah_id = Column(Integer, ForeignKey("wilayah.id"), nullable=False)
    caleg_id = Column(Integer, ForeignKey("caleg.id"), nullable=False)
    jumlah_suara = Column(Integer, nullable=False, default=0)
    sumber = Column(String, nullable=False, default="Upload Excel")
    nama_file_asal = Column(String, nullable=True)
    waktu_upload = Column(DateTime, nullable=True)
    aktif = Column(Integer, nullable=False, default=1)  # 1=tampil di Dashboard, 0=disembunyikan

    __table_args__ = (
        UniqueConstraint("wilayah_id", "caleg_id", name="uq_hasil"),
    )
    wilayah = relationship("Wilayah", back_populates="hasil")
    caleg = relationship("Caleg", back_populates="hasil")


def init_db():
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        existing_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(hasil_suara)"))]
        if "sumber" not in existing_cols:
            conn.execute(text("ALTER TABLE hasil_suara ADD COLUMN sumber TEXT DEFAULT 'Upload Excel'"))
        if "nama_file_asal" not in existing_cols:
            conn.execute(text("ALTER TABLE hasil_suara ADD COLUMN nama_file_asal TEXT"))
        if "waktu_upload" not in existing_cols:
            conn.execute(text("ALTER TABLE hasil_suara ADD COLUMN waktu_upload TEXT"))
        if "aktif" not in existing_cols:
            conn.execute(text("ALTER TABLE hasil_suara ADD COLUMN aktif INTEGER DEFAULT 1"))
        conn.commit()


# Kolom WAJIB untuk alur dasar (dashboard suara per wilayah).
REQUIRED_COLUMNS = ["provinsi", "kab_kota", "kecamatan", "kelurahan", "tps", "partai", "jumlah_suara"]

# Kolom OPSIONAL -- kalau ada, dipakai untuk fitur lanjutan Hitung Kursi.
OPTIONAL_COLUMNS = ["dapil", "nomor_urut_partai", "caleg", "nomor_urut_caleg"]


def validate_columns(df: pd.DataFrame):
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    return missing


def import_dataframe(df: pd.DataFrame, sumber: str = "Upload Excel", nama_file_asal: str = None):
    """
    Import dataframe ke database. Kolom wajib: lihat REQUIRED_COLUMNS.
    Kolom opsional (dapil, nomor_urut_partai, caleg, nomor_urut_caleg) dipakai
    kalau ada -- kalau tidak ada, otomatis diberi nilai default (dapil='-',
    setiap baris dianggap "suara partai" langsung, bukan suara per caleg).

    sumber & nama_file_asal dicatat per baris untuk keperluan audit/telusur di Dashboard.
    Mengembalikan (jumlah_baris_masuk, jumlah_baris_dilewati, list_error).
    """
    init_db()
    session = SessionLocal()
    inserted, skipped, errors = 0, 0, []
    waktu = datetime.now()

    partai_cache, caleg_cache, wilayah_cache = {}, {}, {}
    ada_no_partai = "nomor_urut_partai" in df.columns
    ada_dapil = "dapil" in df.columns
    ada_caleg = "caleg" in df.columns

    try:
        for idx, row in df.iterrows():
            try:
                nama_partai = str(row["partai"]).strip()
                no_partai = int(row["nomor_urut_partai"]) if ada_no_partai and pd.notna(row.get("nomor_urut_partai")) else 0
                dapil = str(row["dapil"]).strip() if ada_dapil and pd.notna(row.get("dapil")) else "-"

                caleg_name_raw = str(row.get("caleg", "")).strip() if ada_caleg else ""
                is_suara_partai = (
                    caleg_name_raw == "" or caleg_name_raw.lower() in ("nan", "suara partai")
                )
                caleg_name = SUARA_PARTAI_LABEL if is_suara_partai else caleg_name_raw
                no_caleg = 0
                if not is_suara_partai and "nomor_urut_caleg" in df.columns and pd.notna(row.get("nomor_urut_caleg")):
                    no_caleg = int(row["nomor_urut_caleg"])

                # --- Partai ---
                if nama_partai not in partai_cache:
                    partai = session.query(Partai).filter_by(nama_partai=nama_partai).first()
                    if not partai:
                        partai = Partai(nomor_urut=no_partai, nama_partai=nama_partai)
                        session.add(partai)
                        session.flush()
                    partai_cache[nama_partai] = partai
                partai = partai_cache[nama_partai]

                # --- Caleg (placeholder "(Suara Partai)" kalau tidak ada rincian caleg) ---
                key_c = (partai.id, dapil, no_caleg, caleg_name)
                if key_c not in caleg_cache:
                    caleg = session.query(Caleg).filter_by(
                        partai_id=partai.id, dapil=dapil, nomor_urut=no_caleg, nama_caleg=caleg_name
                    ).first()
                    if not caleg:
                        caleg = Caleg(partai_id=partai.id, dapil=dapil, nomor_urut=no_caleg, nama_caleg=caleg_name)
                        session.add(caleg)
                        session.flush()
                    caleg_cache[key_c] = caleg
                caleg = caleg_cache[key_c]

                # --- Wilayah ---
                key_w = (row["provinsi"], dapil, row["kab_kota"], row["kecamatan"], row["kelurahan"], row["tps"])
                if key_w not in wilayah_cache:
                    wilayah = session.query(Wilayah).filter_by(
                        provinsi=row["provinsi"], dapil=dapil, kab_kota=row["kab_kota"],
                        kecamatan=row["kecamatan"], kelurahan=row["kelurahan"], tps=str(row["tps"])
                    ).first()
                    if not wilayah:
                        wilayah = Wilayah(
                            provinsi=row["provinsi"], dapil=dapil, kab_kota=row["kab_kota"],
                            kecamatan=row["kecamatan"], kelurahan=row["kelurahan"], tps=str(row["tps"])
                        )
                        session.add(wilayah)
                        session.flush()
                    wilayah_cache[key_w] = wilayah
                wilayah = wilayah_cache[key_w]

                # --- Hasil Suara (upsert) ---
                hasil = session.query(HasilSuara).filter_by(wilayah_id=wilayah.id, caleg_id=caleg.id).first()
                jumlah = int(row["jumlah_suara"])
                if hasil:
                    hasil.jumlah_suara = jumlah
                    hasil.sumber = sumber
                    hasil.nama_file_asal = nama_file_asal
                    hasil.waktu_upload = waktu
                else:
                    hasil = HasilSuara(
                        wilayah_id=wilayah.id, caleg_id=caleg.id, jumlah_suara=jumlah,
                        sumber=sumber, nama_file_asal=nama_file_asal, waktu_upload=waktu,
                    )
                    session.add(hasil)

                inserted += 1
            except Exception as e:
                skipped += 1
                errors.append(f"Baris {idx + 2}: {e}")

        session.commit()
    finally:
        session.close()

    return inserted, skipped, errors


def get_full_dataframe() -> pd.DataFrame:
    init_db()
    session = SessionLocal()
    try:
        q = (
            session.query(
                Wilayah.provinsi, Wilayah.dapil, Wilayah.kab_kota, Wilayah.kecamatan,
                Wilayah.kelurahan, Wilayah.tps,
                Partai.nama_partai, Partai.nomor_urut.label("no_partai"),
                Caleg.nama_caleg, Caleg.nomor_urut.label("no_caleg"),
                HasilSuara.jumlah_suara, HasilSuara.sumber,
                HasilSuara.nama_file_asal, HasilSuara.waktu_upload, HasilSuara.aktif,
            )
            .join(HasilSuara, HasilSuara.wilayah_id == Wilayah.id)
            .join(Caleg, HasilSuara.caleg_id == Caleg.id)
            .join(Partai, Caleg.partai_id == Partai.id)
        )
        rows = q.all()
        cols = [
            "provinsi", "dapil", "kab_kota", "kecamatan", "kelurahan", "tps",
            "partai", "no_partai", "caleg", "no_caleg", "jumlah_suara", "sumber",
            "nama_file_asal", "waktu_upload", "aktif",
        ]
        return pd.DataFrame(rows, columns=cols)
    finally:
        session.close()


def list_uploaded_files() -> pd.DataFrame:
    """Ringkasan tiap file yang pernah diupload -- untuk fitur kelola/sembunyikan/hapus per file."""
    df = get_full_dataframe()
    df = df[df["nama_file_asal"].notna() & (df["nama_file_asal"] != "")]
    if df.empty:
        return pd.DataFrame(columns=["nama_file_asal", "waktu_upload_terakhir", "jumlah_baris", "total_suara", "aktif"])
    return (
        df.groupby("nama_file_asal")
        .agg(
            waktu_upload_terakhir=("waktu_upload", "max"),
            jumlah_baris=("jumlah_suara", "count"),
            total_suara=("jumlah_suara", "sum"),
            aktif=("aktif", "max"),  # kalau ada satu saja yang aktif, anggap file itu aktif
        )
        .reset_index()
        .sort_values("waktu_upload_terakhir", ascending=False)
    )


def set_file_active(nama_file_asal: str, aktif: bool) -> int:
    """
    Sembunyikan (aktif=False) atau tampilkan lagi (aktif=True) semua baris dari satu file
    di Dashboard, TANPA menghapus datanya dari database. Bisa dibalik kapan saja tanpa
    perlu upload ulang. Mengembalikan jumlah baris yang terpengaruh.
    """
    init_db()
    session = SessionLocal()
    try:
        rows = session.query(HasilSuara).filter(HasilSuara.nama_file_asal == nama_file_asal).all()
        for r in rows:
            r.aktif = 1 if aktif else 0
        session.commit()
        return len(rows)
    finally:
        session.close()


def delete_by_file(nama_file_asal: str) -> int:
    """Hapus semua baris hasil_suara yang berasal dari satu nama file tertentu."""
    init_db()
    session = SessionLocal()
    try:
        rows = session.query(HasilSuara).filter(HasilSuara.nama_file_asal == nama_file_asal).all()
        jumlah = len(rows)
        for r in rows:
            session.delete(r)
        session.commit()
        return jumlah
    finally:
        session.close()


def reset_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)