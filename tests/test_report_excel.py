import pytest
from pathlib import Path
from openpyxl import load_workbook
from report_excel import fill_sheet, _sanitise_sheet_name, _containing_merge
from report import write_bulk_excel
from config import TEMPLATE_PATH  # <-- add this

def _anchor_of(ws, row, col):
    """Return (row, col) of the top-left anchor of the merge containing
    (row, col), or (row, col) if not merged."""
    from openpyxl.utils import get_column_letter
    coord = f"{get_column_letter(col)}{row}"
    for mr in ws.merged_cells.ranges:
        if coord in mr:
            return mr.min_row, mr.min_col
    return row, col

def test_fill_sheet_output(tmp_path):
    out = tmp_path / "out.xlsx"
    from shutil import copyfile
    copyfile(TEMPLATE_PATH, out)

    wb = load_workbook(out)
    ws = wb["Sheet1"]

    fill_sheet(
        ws,
        truck_id="GT TEST",
        driver_name="TEST DRIVER",
        trip_start=None, trip_end=None,
        stops={"count": 5, "total_seconds": 3600, "rest_break_seconds": 7200},
        events={
            "counts": {"hard_braking": 1, "hard_acceleration": 2, "hard_cornering": 3,
                       "speeding_10": 0, "speeding_20": 0, "speeding_over_20": 0},
            "max_speed": {"hard_braking": 60, "hard_acceleration": 70, "hard_cornering": 50,
                          "speeding_10": 0, "speeding_20": 0, "speeding_over_20": 0},
        },
        yard=None,
        history=None,
        manual={"comments": "Test comment", "maintenance_issues": "Oil change"},
    )

    # Hard acceleration row: count in E, max speed in F
    acc_row = None
    for row in ws.iter_rows(min_col=1, max_col=1):
        if row[0].value and "Hard acceleration" in str(row[0].value):
            acc_row = row[0].row
            break
    assert acc_row is not None
    assert ws.cell(row=acc_row, column=5).value == 2
    assert ws.cell(row=acc_row, column=6).value == 70

    # Comments row: read the merge anchor, not the MergedCell
    comments_row = None
    for row in ws.iter_rows(min_col=1, max_col=1):
        if row[0].value == "Comments":
            comments_row = row[0].row
            break
    assert comments_row is not None

    anchor_row, anchor_col = _anchor_of(ws, comments_row, 4)
    cell = ws.cell(row=anchor_row, column=anchor_col)
    assert cell.value == "Test comment"
    assert cell.alignment.wrap_text is True