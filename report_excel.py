

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter, column_index_from_string
from openpyxl.utils.units import pixels_to_EMU

from report_analysis import FMT, YARD_STATIONS, utc_str


log = logging.getLogger(__name__)

try:
    from PIL import Image as _PILImage  # noqa: F401
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False
    log.warning(
        "Pillow is not installed — signatures will fall back to text. "
        "Add 'Pillow' to requirements.txt and redeploy."
    )


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

BOARD_TEXT_FIELDS = (
    (("Maintenance side", "Maintenance"), "maintenance_issues"),
    (("Comments",),                       "comments"),
)

SIG_IMAGE_MAX_W_PX = 220
SIG_IMAGE_MAX_H_PX = 60

_DEFAULT_COL_WIDTH_CHARS = 8.43
_DEFAULT_ROW_HEIGHT_PT   = 15.0

# Wrapped-text sizing constants.
_WRAP_CHARS_PER_LINE = 50
_LINE_HEIGHT_PT      = 15.0
_ROW_PADDING_PT      = 4.0
_MAX_ROW_HEIGHT_PT   = 409.0


# ---------------------------------------------------------------------------
# Row lookup helpers
# ---------------------------------------------------------------------------

def _find_row(ws, *labels, max_row=200):
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(1, max_row + 1):
        value = ws.cell(row=row, column=1).value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


def _find_row_exact(ws, *labels, max_row=200):
    needles = {lbl.strip().lower() for lbl in labels if lbl and lbl.strip()}
    if not needles:
        return None
    for row in range(1, max_row + 1):
        value = ws.cell(row=row, column=1).value
        if value is None:
            continue
        if str(value).strip().lower() in needles:
            return row
    return None


def _find_row_after(ws, start_row, *labels, max_row=200):
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(start_row, max_row + 1):
        value = ws.cell(row=row, column=1).value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _containing_merge(ws, row, col=None):
    """Return the merged range containing (row, col), or None.

    When ``col`` is None we match any merge that spans ``row``.  The
    signature rows (``C60:H63``) and the free-text rows (``D44:J44``) are
    each inside a merge, and the *whole* merge has to be resized for the
    content to centre correctly.
    """
    for mr in ws.merged_cells.ranges:
        if row < mr.min_row or row > mr.max_row:
            continue
        if col is not None and not (mr.min_col <= col <= mr.max_col):
            continue
        return mr
    return None


def _effective_row_px(ws, row):
    dim = ws.row_dimensions.get(row)
    if dim is not None and dim.height:
        points = float(dim.height)
    else:
        points = _DEFAULT_ROW_HEIGHT_PT
    return int(round(points * 4 / 3))


def _expand_merged_block(ws, row, needed_pt, col=None):
    """Grow every row in the merge containing ``row`` so the total block is
    at least ``needed_pt`` tall.

    If ``row`` is not inside a merge, only that row is grown.  Returns the
    total block height in points after the expansion.
    """
    merge = _containing_merge(ws, row, col=col)

    if merge is None:
        rd = ws.row_dimensions.get(row)
        current = float(rd.height) if (rd is not None and rd.height) else 0.0
        if needed_pt > current:
            ws.row_dimensions[row].height = needed_pt
            current = needed_pt
        return current

    n_rows = merge.max_row - merge.min_row + 1
    current_total = 0.0
    for r in range(merge.min_row, merge.max_row + 1):
        rd = ws.row_dimensions.get(r)
        current_total += (
            float(rd.height) if (rd is not None and rd.height)
            else _DEFAULT_ROW_HEIGHT_PT
        )

    if current_total >= needed_pt:
        return current_total

    # Distribute the *extra* height evenly across every row in the merge.
    extra = needed_pt - current_total
    per_row_extra = extra / n_rows
    for r in range(merge.min_row, merge.max_row + 1):
        rd = ws.row_dimensions.get(r)
        base = (
            float(rd.height) if (rd is not None and rd.height)
            else _DEFAULT_ROW_HEIGHT_PT
        )
        ws.row_dimensions[r].height = base + per_row_extra

    return needed_pt


# ---------------------------------------------------------------------------
# Cell writes
# ---------------------------------------------------------------------------

def _safe_write(ws, coord_or_none, value, number_format=None):
    if coord_or_none is None:
        return None
    target = coord_or_none
    for mr in ws.merged_cells.ranges:
        if coord_or_none in mr:
            anchor = str(mr).split(":")[0]
            if anchor != coord_or_none:
                target = anchor
            break
    ws[target] = value
    if number_format:
        ws[target].number_format = number_format
    return target


def _write_at(ws, row, column, value, number_format=None):
    if row is None:
        return
    _safe_write(ws, f"{column}{row}", value, number_format)


def _write_wrapped(ws, row, column, value,
                   chars_per_line=_WRAP_CHARS_PER_LINE,
                   horizontal="center",
                   vertical="center"):
    """Write ``value`` wrapped, vertically centred, and grow the row block.

    This is the text equivalent of ``_embed_signature``:

      * ``wrap_text=True`` so Excel wraps the text.
      * ``vertical="center"`` so multi-line text is centred inside the row
        (this is the fix — ``vertical="top"`` left the text hanging at the
        top of the cell).
      * ``_expand_merged_block`` raises *every* row in the containing merge
        so the whole visible cell is tall enough for the wrapped text.
    """
    if row is None:
        return

    coord = _safe_write(ws, f"{column}{row}", value)
    if coord is None:
        return

    ws[coord].alignment = Alignment(
        wrap_text=True,
        vertical=vertical,
        horizontal=horizontal,
    )

    text = "" if value is None else str(value)
    if not text:
        return

    # Estimate the number of wrapped lines: split on explicit newlines
    # first, then wrap each segment at ``chars_per_line``.
    line_count = 0
    for segment in text.split("\n"):
        if not segment:
            line_count += 1
            continue
        line_count += max(1, math.ceil(len(segment) / chars_per_line))

    needed_pt = min(
        _MAX_ROW_HEIGHT_PT,
        line_count * _LINE_HEIGHT_PT + _ROW_PADDING_PT,
    )

    # Resolve the merge containing the *actual* coordinate we wrote to
    # (it may be the anchor of a merged range, not the requested column).
    target_col_idx = None
    try:
        target_col_idx = column_index_from_string(
            re.match(r"[A-Z]+", coord).group(0)
        )
    except Exception:
        target_col_idx = None

    block_pt = _expand_merged_block(ws, row, needed_pt, col=target_col_idx)
    log.debug(
        "write_wrapped: row=%d coord=%s lines=%d needed=%.1fpt "
        "block=%.1fpt",
        row, coord, line_count, needed_pt, block_pt,
    )


def _duration(seconds):
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

def _col_width_px(ws, col_letter):
    dim = ws.column_dimensions.get(col_letter)
    if dim is not None and dim.width:
        char_w = float(dim.width)
    else:
        char_w = _DEFAULT_COL_WIDTH_CHARS
    return int(round(char_w * 7 + 5))


def _row_height_px(ws, row, fallback_pt=None):
    dim = ws.row_dimensions.get(row)
    if dim is not None and dim.height:
        points = float(dim.height)
    elif fallback_pt:
        points = float(fallback_pt)
    else:
        points = _DEFAULT_ROW_HEIGHT_PT
    return int(round(points * 4 / 3))


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

_DATA_URL_PNG_RE = re.compile(
    r"^data:image/(png|jpeg|jpg);base64,(.+)$", re.DOTALL
)


def _is_image_signature(kind_field, sig_value):
    if (kind_field or "").strip().lower() != "image":
        return False, None
    value = (sig_value or "").strip()
    m = _DATA_URL_PNG_RE.match(value)
    if not m:
        return False, None
    try:
        raw = base64.b64decode(m.group(2))
    except (ValueError, TypeError):
        return False, None
    return True, raw


def _embed_signature(ws, row, image_bytes, col_start="D", col_end="J"):
    """Embed a signature PNG, centred inside its (possibly merged) cell."""
    if not _PIL_AVAILABLE:
        log.error("Cannot embed signature at row %d: Pillow not installed.", row)
        return

    try:
        img = XLImage(io.BytesIO(image_bytes))
    except Exception as exc:
        log.warning("could not build signature image: %s", exc)
        return

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

    padding_px = 8
    needed_pt = (h + padding_px * 2) * 0.75

    # Resolve the merge containing the "Driver's Signature" label cell.
    # The default anchor column is D (col index 4) — that's where the
    # merge in the template starts.  Passing `col=` ensures we don't
    # accidentally match a merge that only *spans* this row.
    merge = _containing_merge(ws, row, col=column_index_from_string("D"))

    if merge is None:
        block_pt = _expand_merged_block(ws, row, needed_pt)
        anchor_row = row
        cs = column_index_from_string(col_start)
        ce = column_index_from_string(col_end)
    else:
        block_pt = _expand_merged_block(
            ws, row, needed_pt, col=column_index_from_string("D"),
        )
        anchor_row = merge.min_row
        cs = merge.min_col
        ce = merge.max_col

    if ce < cs:
        cs, ce = ce, cs

    # Total merged block height in pixels, summed across every row.
    if merge is None:
        block_h_px = _row_height_px(ws, row, fallback_pt=block_pt)
    else:
        block_h_px = sum(
            _effective_row_px(ws, r)
            for r in range(merge.min_row, merge.max_row + 1)
        )

    total_width_px = sum(
        _col_width_px(ws, get_column_letter(c))
        for c in range(cs, ce + 1)
    )

    x_off_px = max(0, (total_width_px - w) // 2)
    y_off_px = max(0, (block_h_px - h) // 2)

    log.info(
        "signature anchor: row=%d anchor_row=%d merge=%s block_pt=%.1f "
        "block_h_px=%d img=%dx%d x_off_px=%d y_off_px=%d",
        row, anchor_row, merge, block_pt, block_h_px, w, h,
        x_off_px, y_off_px,
    )

    # Remove any stale signature image already anchored here (defensive
    # against templates that ship with a placeholder image, or a previous
    # export that left one behind).
    try:
        for existing in list(ws._images):
            anch = existing.anchor
            if hasattr(anch, "_from"):
                a = anch._from
                if a.row == anchor_row - 1 and a.col == cs - 1:
                    ws._images.remove(existing)
                    log.info("removed stale signature image at row %d",
                             anchor_row)
    except Exception:
        pass

    marker = AnchorMarker(
        col=cs - 1,
        colOff=pixels_to_EMU(x_off_px),
        row=anchor_row - 1,
        rowOff=pixels_to_EMU(y_off_px),
    )
    size = XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h))
    anchor = OneCellAnchor(_from=marker, ext=size)

    try:
        ws.add_image(img, anchor)
        log.info(
            "embedded signature centered in %s%d:%s%d "
            "(%d×%d px, x_off=%d, y_off=%d, block_h_px=%d)",
            get_column_letter(cs), anchor_row,
            get_column_letter(ce), merge.max_row if merge else anchor_row,
            w, h, x_off_px, y_off_px, block_h_px,
        )
    except Exception as exc:
        log.warning("could not add centered signature image: %s", exc)


# ---------------------------------------------------------------------------
# fill_sheet
# ---------------------------------------------------------------------------

def fill_sheet(ws, *, truck_id, driver_name, trip_start, trip_end,
               stops, events, yard, history, manual=None):
    manual = manual or {}

    # ---- header rows ----
    for label, value in (
        ("Report Generated", datetime.now(timezone.utc).strftime(FMT_DT)),
        ("Trip Start Date/Time", trip_start.strftime(FMT_DT) if trip_start else None),
        ("Trip End Date/Time",   trip_end.strftime(FMT_DT) if trip_end else None),
        ("Trck ID",              truck_id),
    ):
        if value is None:
            continue
        _write_at(ws, _find_row(ws, label), "D", value)

    # ---- driver name ----
    row = _find_row(ws, "Drivers Name", "Driver's Name", "Driver Name")
    effective_driver = _blank_to_none(manual.get("driver_name"))
    _write_at(ws, row, "D",
              effective_driver if effective_driver is not None else driver_name)

    # ---- yard operations (before trip) ----
    if yard is not None:
        for key, _ in YARD_STATIONS:
            row = _find_row(ws, YARD_STATION_LABELS[key])
            _write_at(ws, row, "E",
                      _duration(yard["operations"][key]["total_seconds"]),
                      DURATION_FORMAT)
        row = _find_row(ws, "Total Yard Time")
        _write_at(ws, row, "E",
                  _duration(yard["total_seconds"]), DURATION_FORMAT)

    # ---- rest and breaks (this trip) ----
    row = _find_row(ws, "Duration of Manditory rest break",
                    "Duration of Mandatory rest break")
    _write_at(ws, row, "E", _duration(stops["rest_break_seconds"]),
              DURATION_FORMAT)

    row = _find_row(ws, "Number of stops exceeding 5 mins")
    _write_at(ws, row, "E", stops["count"])

    row = _find_row(ws, "Duration of stops exceeding 5 mins")
    _write_at(ws, row, "E", _duration(stops["total_seconds"]),
              DURATION_FORMAT)

    # ---- driver behaviour (this trip) ----
    c, m = events["counts"], events["max_speed"]

    def _behaviour_row(label_start):
        return _find_row(ws, label_start)

    r = _behaviour_row("Hard breaking") or _behaviour_row("Hard braking")
    _write_at(ws, r, "E", c["hard_braking"])
    _write_at(ws, r, "F", round(m["hard_braking"], 1))

    r = _behaviour_row("Hard acceleration")
    _write_at(ws, r, "E", c["hard_acceleration"])
    _write_at(ws, r, "F", round(m["hard_acceleration"], 1))

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

    # ---- history (previous 3 months) ----
    if history and history["trip_count"] > 0:
        h = history
        if h.get("yard"):
            yh = h["yard"]
            for key, _ in YARD_STATIONS:
                row = _find_row(ws, YARD_STATION_LABELS[key])
                op = yh["operations"][key]
                _write_at(ws, row, "H", _duration(op["avg_seconds"]),
                          DURATION_FORMAT)
                _write_at(ws, row, "I", _duration(op["max_seconds"]),
                          DURATION_FORMAT)
                _write_at(ws, row, "J", _duration(op["min_seconds"]),
                          DURATION_FORMAT)

            row = _find_row(ws, "Total Yard Time")
            _write_at(ws, row, "H", _duration(yh["avg_total_seconds"]),
                      DURATION_FORMAT)
            _write_at(ws, row, "I", _duration(yh["max_total_seconds"]),
                      DURATION_FORMAT)
            _write_at(ws, row, "J", _duration(yh["min_total_seconds"]),
                      DURATION_FORMAT)

        row = _find_row(ws, "Duration of Manditory rest break",
                        "Duration of Mandatory rest break")
        _write_at(ws, row, "H", _duration(h["rests"]["avg_seconds"]),
                  DURATION_FORMAT)
        _write_at(ws, row, "I", _duration(h["rests"]["max_seconds"]),
                  DURATION_FORMAT)
        _write_at(ws, row, "J", _duration(h["rests"]["min_seconds"]),
                  DURATION_FORMAT)

        row = _find_row(ws, "Number of stops exceeding 5 mins")
        _write_at(ws, row, "H", round(h["stops"]["avg_count"], 1))
        _write_at(ws, row, "I", h["stops"]["max_count"])
        _write_at(ws, row, "J", h["stops"]["min_count"])

        row = _find_row(ws, "Duration of stops exceeding 5 mins")
        _write_at(ws, row, "H", _duration(h["stops"]["avg_seconds"]),
                  DURATION_FORMAT)
        _write_at(ws, row, "I", _duration(h["stops"]["max_seconds"]),
                  DURATION_FORMAT)
        _write_at(ws, row, "J", _duration(h["stops"]["min_seconds"]),
                  DURATION_FORMAT)

        b = h["behaviours"]
        r = _behaviour_row("Hard breaking") or _behaviour_row("Hard braking")
        _write_at(ws, r, "H", b["hard_braking"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_braking"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_braking"]["avg_max_speed"], 1))

        r = _behaviour_row("Hard acceleration")
        _write_at(ws, r, "H", b["hard_acceleration"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_acceleration"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_acceleration"]["avg_max_speed"], 1))

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

    # ---- statistics ----
    for label, key in (
        ("Out of fuel event", "out_of_fuel"),
        ("Accidents",         "accidents"),
        ("Tyres damaged",     "tyres_damaged"),
        ("Breakdown",         "breakdown"),
    ):
        val = _blank_to_none(manual.get(key))
        if val is None:
            continue
        _write_at(ws, _find_row(ws, label), "E", val)

    # ---- board additions (Comments + Maintenance side) ----
    # Use _find_row_exact so "Maintenance" cannot match "Time at Maintenance".
    # Use _write_wrapped (centred) so the text is vertically centred, wraps,
    # and the containing merge grows to fit.
    for label_variants, key in BOARD_TEXT_FIELDS:
        val = _blank_to_none(manual.get(key))
        if val is None:
            continue
        row = _find_row_exact(ws, *label_variants)
        if row is None:
            log.warning("fill_sheet: no exact-match row for %r",
                        label_variants)
            continue
        _write_wrapped(ws, row, "D", val,
                       horizontal="center", vertical="center")

    # ---- driver's signature ----
    driver_sig_row = _find_row(ws, "Drivers Signature", "Driver's Signature")
    is_img, img_bytes = _is_image_signature(
        manual.get("signature_kind"), manual.get("signature"),
    )
    if is_img and img_bytes and driver_sig_row is not None and _PIL_AVAILABLE:
        _embed_signature(ws, driver_sig_row, img_bytes)
    else:
        signature = _blank_to_none(manual.get("signature"))
        if signature is not None and driver_sig_row is not None:
            _write_wrapped(ws, driver_sig_row, "D", signature,
                           horizontal="center", vertical="center")

    # ---- officer's signature ----
    officer_sig_row = _find_row(
        ws, "Officer's Signature", "Officer Signature", "Officers Signature",
    )
    is_img_o, img_bytes_o = _is_image_signature(
        manual.get("officer_signature_kind"), manual.get("officer_signature"),
    )
    if is_img_o and img_bytes_o and officer_sig_row is not None and _PIL_AVAILABLE:
        _embed_signature(ws, officer_sig_row, img_bytes_o)
    else:
        officer_sig = _blank_to_none(manual.get("officer_signature"))
        if officer_sig is not None and officer_sig_row is not None:
            _write_wrapped(ws, officer_sig_row, "D", officer_sig,
                           horizontal="center", vertical="center")


# ---------------------------------------------------------------------------
# fill_template
# ---------------------------------------------------------------------------

def fill_template(template_path, output_path, *, truck_id, driver_name,
                  trip_start, trip_end, stops, events, yard, history,
                  manual=None):
    wb = load_workbook(template_path)
    ws = wb["Sheet1"]
    fill_sheet(
        ws, truck_id=truck_id, driver_name=driver_name,
        trip_start=trip_start, trip_end=trip_end,
        stops=stops, events=events, yard=yard, history=history,
        manual=manual,
    )
    wb.save(output_path)


# ---------------------------------------------------------------------------
# write_bulk_excel
# ---------------------------------------------------------------------------

_SHEET_NAME_BAD_CHARS = set('[]:*?/\\')


def _sanitise_sheet_name(name, existing):
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
        candidate = base[: 31 - len(suffix)] + suffix
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    fallback = "Sheet" + str(len(existing) + 1)
    existing.add(fallback)
    return fallback


def write_bulk_excel(reports, template_path, output_path,
                     per_trip_manual=None):
    if not reports:
        raise ValueError("write_bulk_excel: reports list is empty")
    per_trip_manual = per_trip_manual or {}

    per_truck_seen = {}
    sheet_plan = []
    for r in reports:
        truck = r.get("truck_id") or "Report"
        n = per_truck_seen.get(truck, 0) + 1
        per_truck_seen[truck] = n
        sheet_plan.append((r, truck if n == 1 else f"{truck} ({n})"))

    wb = load_workbook(template_path)
    if "Sheet1" not in wb.sheetnames:
        raise RuntimeError(f"no Sheet1 in template: {wb.sheetnames!r}")
    template_ws = wb["Sheet1"]

    used_names = set()
    for i, (report, desired_name) in enumerate(sheet_plan):
        target_name = _sanitise_sheet_name(desired_name, used_names)
        if target_name == "Sheet1":
            target_name = _sanitise_sheet_name("Sheet1 (trip)", used_names)

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

    if "Sheet1" in wb.sheetnames and len(wb.sheetnames) > 1:
        del wb["Sheet1"]

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