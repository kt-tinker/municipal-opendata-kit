"""表データの読み書き（CSV / Excel）。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

ENCODINGS = ("utf-8-sig", "cp932", "utf-8")


def _decode(data: bytes, encoding: str) -> str:
    if encoding != "auto":
        return data.decode(encoding)
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できませんでした（utf-8 / cp932 以外）")


def read_table(path: Path, fmt: str = "csv", encoding: str = "auto", sheet: str | None = None):
    """(列名リスト, 行の辞書リスト) を返す。空行は捨てる。"""
    if fmt == "xlsx":
        try:
            import openpyxl  # 任意の依存
        except ImportError as e:  # pragma: no cover
            raise SystemExit("Excel を読むには openpyxl が必要です: pip install openpyxl") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        cols = [str(c).strip() if c is not None else "" for c in next(it)]
        rows = []
        for values in it:
            vals = ["" if v is None else str(v).strip() for v in values]
            if any(vals):
                rows.append(dict(zip(cols, vals)))
        return cols, rows

    text = _decode(Path(path).read_bytes(), encoding)
    reader = csv.reader(io.StringIO(text, newline=""))
    cols = [c.strip() for c in next(reader)]
    rows = []
    for values in reader:
        vals = [v.strip() for v in values]
        if any(vals):
            rows.append(dict(zip(cols, vals)))
    return cols, rows


def write_std_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    """自治体標準オープンデータセットの推奨どおり UTF-8（BOM付き）・CRLF で書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
