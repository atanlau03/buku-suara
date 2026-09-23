import pickle
import sys
from pathlib import Path

from local_ocr_engine import process_pdf_local_engine


def main():
    if len(sys.argv) != 4:
        raise SystemExit("Usage: pdf_worker.py INPUT.pdf OUTPUT.pkl DPI")
    input_path, output_path, dpi = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    data = input_path.read_bytes()
    result = process_pdf_local_engine(data, input_path.name, dpi_val=dpi)
    with output_path.open("wb") as fh:
        pickle.dump(result, fh, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main()
