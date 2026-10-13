import json
import re
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
SCAN_RESULTS_DIR = BASE_DIR / "scan_results"
SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def archive_name(file_name):
    stem = Path(file_name).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return (safe or "hasil_scan") + ".json"


def save_scan_result(file_name, result, dpi_val):
    archive = {
        "nama_file_pdf": file_name,
        "waktu_scan": pd.Timestamp.now().isoformat(),
        "dpi": int(dpi_val),
        "hasil": {},
    }
    for key, value in result.items():
        if isinstance(value, pd.DataFrame):
            archive["hasil"][key] = json.loads(
                value.to_json(orient="records", date_format="iso")
            )
        else:
            archive["hasil"][key] = value

    out = SCAN_RESULTS_DIR / archive_name(file_name)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)


def load_saved_result(file_name):
    path = SCAN_RESULTS_DIR / archive_name(file_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hasil = {
            k: (pd.DataFrame(v) if isinstance(v, list) else v)
            for k, v in data.get("hasil", {}).items()
        }
        return {
            "nama_file_pdf": data.get("nama_file_pdf", file_name),
            "waktu_scan": data.get("waktu_scan", ""),
            "dpi": data.get("dpi", ""),
            "hasil": hasil,
        }
    except Exception:
        return None