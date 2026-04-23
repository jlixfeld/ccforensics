from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


def write_json(data: Any, out: TextIO) -> None:
    json.dump(data, out, indent=2, ensure_ascii=False, sort_keys=False)
    out.write("\n")


def write_csv(rows: Iterable[Mapping[str, Any]], headers: list[str], out: TextIO) -> None:
    writer = csv.DictWriter(out, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: _csv_cell(row.get(h)) for h in headers})


_FORMULA_INJECT_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    # Guard against spreadsheet formula injection: prefix a single quote so
    # Excel/LibreOffice render the cell as literal text, not a formula.
    if s.startswith(_FORMULA_INJECT_PREFIX):
        return "'" + s
    return s
