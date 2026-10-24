"""
report_excel.py — filling the Trip Review Excel template.

Consumes the plain-dict output of report_analysis.py (stops / events / yard /
history) and writes it into the xlsx template. Handles single-trip fills and
bulk (one workbook, one sheet per trip) fills. Also embeds signature images
(drawn in the browser and passed in as data URLs) at column D of the
"Drivers Signature:" row.

The public API is:

  fill_sheet(ws, *, truck_id, driver_name, trip_start, trip_end,
             stops, events, yard, history, manual) -> None
  fill_template(template_path, output_path, *, ...) -> None
  write_bulk_excel(reports, template_path, output_path,
                   per_trip_manual=None) -> None
  _sanitise_sheet_name(name, existing) -> str

Anything else here is private to this module.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.utils import get_column_letter, column_index_from_string
from openpyxl.utils.units import pixels_to_EMU

from report_analysis import (
    FMT, YARD_STATIONS, utc_str,
)


log = logging.getLogger(__name__)


# Pillow is required to build openpyxl images. If it's missing, signature
# embedding will silently fail — so make that failure loud at import time.
try:
    from PIL import Image as _PILImage  # noqa: F401
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False
    log.warning(
        "Pillow is not installed — signatures will NOT be embedded in "
        "Excel files. Add 'Pillow' to requirements.txt and redeploy."
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DURATION_FORMAT = "[h]:mm:ss"
FMT_DT = "%Y-%m-%d %H:%M:%S"

YARD_STATION_LABELS = {
    "maintenance": "Time at Maintenance",
    "waybill":     "Time for Waybill",
    "fueling":     "Time for Fueling",
    "loading":     "Time for Loading",
    "weighing":    "Time for weighing",
    "waiting":     "Time at Waiting (Dispatch)",
}

SIG_IMAGE_MAX_W_PX = 220
SIG_IMAGE_MAX_H_PX = 60

# openpyxl's defaults when a column/row dimension hasn't been set explicitly.
_DEFAULT_COL_WIDTH_CHARS = 8.43   # Excel default (in "characters")
_DEFAULT_ROW_HEIGHT_PT   = 15.0   # Excel default (in points)


# ---------------------------------------------------------------------------
# Row lookup helpers
# ---------------------------------------------------------------------------

def _find_row(ws, *labels: str, max_row: int = 120) -> int | None:
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(1, max_row + 1):
        cell = ws.cell(row=row, column=1)
        value = cell.value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


def _find_row_after(ws, start_row: int, *labels: str,
                    max_row: int = 120) -> int | None:
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(start_row, max_row + 1):
        cell = ws.cell(row=row, column=1)
        value = cell.value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


def _safe_write(ws, coord_or_none, value, number_format: str | None = None) -> None:
    if coord_or_none is None:
        return
    target = coord_or_none
    for mr in ws.merged_cells.ranges:
        if coord_or_none in mr:
            anchor = str(mr).split(":")[0]
            if anchor != coord_or_none:
                log.debug("%s inside merge %s; → %s", coord_or_none, mr, anchor)
                target = anchor
            break
    ws[target] = value
    if number_format:
        ws[target].number_format = number_format


def _write_at(ws, row: int | None, column: str, value,
              number_format: str | None = None) -> None:
    if row is None:
        return
    _safe_write(ws, f"{column}{row}", value, number_format)


def _duration(seconds) -> float:
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    return s / 86400.0


def _blank_to_none(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


# ---------------------------------------------------------------------------
# Cell-dimension helpers (for centering the signature image)
# ---------------------------------------------------------------------------

def _col_width_px(ws, col_letter: str) -> int:
    """Approximate pixel width of a column (Calibri 11, 96 DPI)."""
    dim = ws.column_dimensions.get(col_letter)
    if dim is not None and dim.width:
        char_w = float(dim.width)
    else:
        char_w = _DEFAULT_COL_WIDTH_CHARS
    # Excel's rule-of-thumb: pixels = chars * 7 + 5 (padding).
    return int(round(char_w * 7 + 5))


def _row_height_px(ws, row: int) -> int:
    """Pixel height of a row (points → pixels at 96 DPI)."""
    dim = ws.row_dimensions.get(row)
    if dim is not None and dim.height:
        points = float(dim.height)
    else:
        points = _DEFAULT_ROW_HEIGHT_PT
    return int(round(points * 4 / 3))


# ---------------------------------------------------------------------------
# Signature handling
# ---------------------------------------------------------------------------

_DATA_URL_PNG_RE = re.compile(r"^data:image/(png|jpeg|jpg);base64,(.+)$", re.DOTALL)


def _is_image_signature(manual: dict | None) -> tuple[bool, bytes | None]:
    if not manual:
        return False, None
    if (manual.get("signature_kind") or "").strip().lower() != "image":
        return False, None
    value = (manual.get("signature") or "").strip()
    m = _DATA_URL_PNG_RE.match(value)
    if not m:
        return False, None
    try:
        raw = base64.b64decode(m.group(2))
    except (ValueError, TypeError):
        return False, None
    return True, raw


def _embed_signature(ws, row: int, image_bytes: bytes,
                     col_start: str = "D", col_end: str = "J") -> None:
    """Embed a signature PNG, centered both horizontally and vertically
    across the ``col_start`` → ``col_end`` range on ``row``."""
    if not _PIL_AVAILABLE:
        log.error(
            "Cannot embed signature at row %d: Pillow is not installed. "
            "Add 'Pillow' to requirements.txt and redeploy.",
            row,
        )
        return

    try:
        img = XLImage(io.BytesIO(image_bytes))
    except Exception as exc:
        log.warning("could not build signature image: %s", exc)
        return

    # --- scale the image down to the max size ---
    w, h = img.width, img.height
    if not w or not h:
        log.warning("signature image has zero dimension: %r×%r", w, h)
        return
    if w > SIG_IMAGE_MAX_W_PX:
        scale = SIG_IMAGE_MAX_W_PX / w
        w = int(w * scale)
        h = int(h * scale)
    if h > SIG_IMAGE_MAX_H_PX:
        scale = SIG_IMAGE_MAX_H_PX / h
        h = int(h * scale)
        w = int(w * scale)
    img.width = w
    img.height = h

    # --- make sure the row is tall enough to hold the image ---
    padding_px = 6
    needed_pt = (h + padding_px * 2) * 0.75   # pixels → points
    current = ws.row_dimensions[row].height
    if current is None or current < needed_pt:
        ws.row_dimensions[row].height = needed_pt

    # --- compute the total pixel width of the target range ---
    cs = column_index_from_string(col_start)
    ce = column_index_from_string(col_end)
    if ce < cs:
        cs, ce = ce, cs

    total_width_px = 0
    for c in range(cs, ce + 1):
        total_width_px += _col_width_px(ws, get_column_letter(c))

    row_h_px = _row_height_px(ws, row)

    # --- centering offsets (clamped to ≥ 0 in case the image is wider
    #     than the range; that shouldn't happen with the default range) ---
    x_off_px = max(0, (total_width_px - w) // 2)
    y_off_px = max(0, (row_h_px - h) // 2)

    # --- build the anchor: top-left corner at (col_start, row) + offsets,
    #     extent = scaled image size ---
    marker = AnchorMarker(
        col=cs - 1,                         # 0-indexed column
        colOff=pixels_to_EMU(x_off_px),
        row=row - 1,                        # 0-indexed row
        rowOff=pixels_to_EMU(y_off_px),
    )
    size = XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h))
    anchor = OneCellAnchor(_from=marker, ext=size)

    try:
        ws.add_image(img, anchor)
        log.info(
            "embedded signature image centered in %s%d:%s%d "
            "(%d×%d px, x_off=%d, y_off=%d)",
            col_start, row, col_end, row, w, h, x_off_px, y_off_px,
        )
    except Exception as exc:
        log.warning("could not add centered signature image: %s", exc)


# ---------------------------------------------------------------------------
# fill_sheet
# ---------------------------------------------------------------------------

def fill_sheet(ws, *,
               truck_id: str, driver_name: str,
               trip_start: datetime | None,
               trip_end: datetime | None,
               stops: dict, events: dict,
               yard: dict | None,
               history: dict | None,
               manual: dict | None = None) -> None:
    manual = manual or {}

    for label, value in (
        ("Report Generated", datetime.now(timezone.utc).strftime(FMT_DT)),
        ("Trip Start Date/Time", trip_start.strftime(FMT_DT) if trip_start else None),
        ("Trip End Date/Time",   trip_end.strftime(FMT_DT) if trip_end else None),
        ("Trck ID",              truck_id),
    ):
        if value is None:
            continue
        row = _find_row(ws, label)
        _write_at(ws, row, "D", value)

    row = _find_row(ws, "Drivers Name", "Driver's Name", "Driver Name")
    effective_driver = _blank_to_none(manual.get("driver_name"))
    _write_at(ws, row, "D",
              effective_driver if effective_driver is not None else driver_name)

    if yard is not None:
        for key, _ in YARD_STATIONS:
            label = YARD_STATION_LABELS[key]
            row = _find_row(ws, label)
            _write_at(ws, row, "E",
                      _duration(yard["operations"][key]["total_seconds"]),
                      DURATION_FORMAT)
        row = _find_row(ws, "Total Yard Time")
        _write_at(ws, row, "E",
                  _duration(yard["total_seconds"]), DURATION_FORMAT)

    row = _find_row(ws, "Duration of Manditory rest break",
                    "Duration of Mandatory rest break")
    _write_at(ws, row, "E",
              _duration(stops["rest_break_seconds"]), DURATION_FORMAT)

    row = _find_row(ws, "Number of stops exceeding 5 mins")
    _write_at(ws, row, "E", stops["count"])

    row = _find_row(ws, "Duration of stops exceeding 5 mins")
    _write_at(ws, row, "E",
              _duration(stops["total_seconds"]), DURATION_FORMAT)

    c, m = events["counts"], events["max_speed"]

    def _behaviour_row(label_start: str) -> int | None:
        return _find_row(ws, label_start)

    r = _behaviour_row("Hard breaking") or _behaviour_row("Hard braking")
    _write_at(ws, r, "E", c["hard_braking"])
    _write_at(ws, r, "F", round(m["hard_braking"], 1))

    r = _behaviour_row("Hard cornering")
    _write_at(ws, r, "E", c["hard_cornering"])
    _write_at(ws, r, "F", round(m["hard_cornering"], 1))

    r = _behaviour_row("Speeding event <10kph")
    _write_at(ws, r, "E", c["speeding_10"])
    _write_at(ws, r, "F", round(m["speeding_10"], 1))

    r = _behaviour_row("Speeding event <20kph")
    _write_at(ws, r, "E", c["speeding_20"])
    _write_at(ws, r, "F", round(m["speeding_20"], 1))

    r = _behaviour_row("Speeding events >20kph")
    _write_at(ws, r, "E", c["speeding_over_20"])
    _write_at(ws, r, "F", round(m["speeding_over_20"], 1))

    if history and history["trip_count"] > 0:
        h = history
        if h.get("yard"):
            yh = h["yard"]
            for key, _ in YARD_STATIONS:
                label = YARD_STATION_LABELS[key]
                row = _find_row(ws, label)
                op = yh["operations"][key]
                _write_at(ws, row, "H", _duration(op["avg_seconds"]), DURATION_FORMAT)
                _write_at(ws, row, "I", _duration(op["max_seconds"]), DURATION_FORMAT)
                _write_at(ws, row, "J", _duration(op["min_seconds"]), DURATION_FORMAT)

            row = _find_row(ws, "Total Yard Time")
            _write_at(ws, row, "H",
                      _duration(yh["avg_total_seconds"]), DURATION_FORMAT)
            _write_at(ws, row, "I",
                      _duration(yh["max_total_seconds"]), DURATION_FORMAT)
            _write_at(ws, row, "J",
                      _duration(yh["min_total_seconds"]), DURATION_FORMAT)

        row = _find_row(ws, "Duration of Manditory rest break",
                        "Duration of Mandatory rest break")
        _write_at(ws, row, "H",
                  _duration(h["rests"]["avg_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "I",
                  _duration(h["rests"]["max_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "J",
                  _duration(h["rests"]["min_seconds"]), DURATION_FORMAT)

        row = _find_row(ws, "Number of stops exceeding 5 mins")
        _write_at(ws, row, "H", round(h["stops"]["avg_count"], 1))
        _write_at(ws, row, "I", h["stops"]["max_count"])
        _write_at(ws, row, "J", h["stops"]["min_count"])

        row = _find_row(ws, "Duration of stops exceeding 5 mins")
        _write_at(ws, row, "H",
                  _duration(h["stops"]["avg_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "I",
                  _duration(h["stops"]["max_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "J",
                  _duration(h["stops"]["min_seconds"]), DURATION_FORMAT)

        b = h["behaviours"]
        r = _behaviour_row("Hard breaking") or _behaviour_row("Hard braking")
        _write_at(ws, r, "H", b["hard_braking"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_braking"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_braking"]["avg_max_speed"], 1))

        r = _behaviour_row("Hard cornering")
        _write_at(ws, r, "H", b["hard_cornering"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_cornering"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_cornering"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding event <10kph")
        _write_at(ws, r, "H", b["speeding_10"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_10"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_10"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding event <20kph")
        _write_at(ws, r, "H", b["speeding_20"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_20"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_20"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding events >20kph")
        _write_at(ws, r, "H", b["speeding_over_20"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_over_20"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_over_20"]["avg_max_speed"], 1))

    for label, key in (
        ("Out of fuel event", "out_of_fuel"),
        ("Accidents",         "accidents"),
        ("Tyres damaged",     "tyres_damaged"),
        ("Breakdown",         "breakdown"),
    ):
        val = _blank_to_none(manual.get(key))
        if val is None:
            continue
        row = _find_row(ws, label)
        _write_at(ws, row, "E", val)

    # Signature — image branch (new) or legacy text branch.
    sig_row = _find_row(ws, "Drivers Signature", "Driver's Signature")
    is_img, img_bytes = _is_image_signature(manual)
    if is_img and img_bytes and sig_row is not None:
        _embed_signature(ws, sig_row, img_bytes)
    else:
        signature = _blank_to_none(manual.get("signature"))
        if signature is not None and sig_row is not None:
            _write_at(ws, sig_row, "D", signature)


# ---------------------------------------------------------------------------
# fill_template
# ---------------------------------------------------------------------------

def fill_template(template_path: Path, output_path: Path, *,
                  truck_id: str, driver_name: str,
                  trip_start: datetime | None,
                  trip_end: datetime | None,
                  stops: dict, events: dict,
                  yard: dict | None,
                  history: dict | None,
                  manual: dict | None = None) -> None:
    wb = load_workbook(template_path)
    ws = wb["Sheet1"]
    fill_sheet(
        ws,
        truck_id=truck_id, driver_name=driver_name,
        trip_start=trip_start, trip_end=trip_end,
        stops=stops, events=events,
        yard=yard, history=history, manual=manual,
    )
    wb.save(output_path)


# ---------------------------------------------------------------------------
# write_bulk_excel
# ---------------------------------------------------------------------------

_SHEET_NAME_BAD_CHARS = set('[]:*?/\\')


def _sanitise_sheet_name(name: str, existing: set[str]) -> str:
    base = "".join(
        "_" if (ch in _SHEET_NAME_BAD_CHARS or ord(ch) < 32) else ch
        for ch in (name or "Report")
    ).strip() or "Report"
    base = base[:31]

    if base not in existing:
        existing.add(base)
        return base

    for n in range(2, 1000):
        suffix = f" ({n})"
        candidate = (base[: 31 - len(suffix)] + suffix)
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    fallback = "Sheet" + str(len(existing) + 1)
    existing.add(fallback)
    return fallback


def write_bulk_excel(reports: list[dict],
                     template_path: Path,
                     output_path: Path,
                     per_trip_manual: dict[str, dict] | None = None) -> None:
    if not reports:
        raise ValueError("write_bulk_excel: reports list is empty")

    per_trip_manual = per_trip_manual or {}

    per_truck_seen: dict[str, int] = {}
    sheet_plan: list[tuple[dict, str]] = []
    for r in reports:
        truck = r.get("truck_id") or "Report"
        n = per_truck_seen.get(truck, 0) + 1
        per_truck_seen[truck] = n
        sheet_plan.append((r, truck if n == 1 else f"{truck} ({n})"))

    wb = load_workbook(template_path)
    original_sheet_name = "Sheet1"
    if original_sheet_name not in wb.sheetnames:
        raise RuntimeError(
            f"write_bulk_excel: template has no sheet {original_sheet_name!r}; "
            f"found {wb.sheetnames!r}"
        )
    template_ws = wb[original_sheet_name]

    used_names: set[str] = set()
    for i, (report, desired_name) in enumerate(sheet_plan):
        target_name = _sanitise_sheet_name(desired_name, used_names)
        if target_name == original_sheet_name:
            target_name = _sanitise_sheet_name(target_name + " (trip)", used_names)

        new_ws = wb.copy_worksheet(template_ws)
        new_ws.title = target_name

        fill_sheet(
            new_ws,
            truck_id=report["truck_id"],
            driver_name=report.get("driver_name") or "",
            trip_start=report.get("trip_start"),
            trip_end=report.get("trip_end"),
            stops=report["stops"],
            events=report["events"],
            yard=report.get("yard"),
            history=report.get("history"),
            manual=per_trip_manual.get(str(i), {}),
        )

    if original_sheet_name in wb.sheetnames and len(wb.sheetnames) > 1:
        del wb[original_sheet_name]

    wb.save(output_path)

    meta = {
        "kind": "bulk",
        "sheet_count": len(sheet_plan),
        "sheets": [
            {
                "sheet_name": target_name,
                "truck_id":   r["truck_id"],
                "imei":       r.get("imei"),
                "trip_start": utc_str(r["trip_start"]) if r.get("trip_start") else None,
                "trip_end":   utc_str(r["trip_end"]) if r.get("trip_end") else None,
            }
            for r, target_name in sheet_plan
        ],
        "generated_at": utc_str(datetime.now(timezone.utc)),
    }
    try:
        output_path.with_suffix(".meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write bulk metadata sidecar: %s", exc)