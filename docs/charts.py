"""Draw the case-study chart from the model's own output, so no figure is typed by hand.

Writes docs/img/calibration.svg from model_calibration in data/hc.duckdb.
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
INK, OCHRE, RULE, MUTED = "#2f55b0", "#c27a1a", "#e3ded2", "#5b6170"


def calibration():
    rows = duckdb.connect(str(ROOT / "data" / "hc.duckdb"), read_only=True) \
        .sql("SELECT decile, predicted, actual FROM model_calibration ORDER BY decile").fetchall()
    w, h, left, top, bottom = 820, 200, 44, 14, 170
    top_value = 0.5
    y = lambda v: bottom - (bottom - top) * v / top_value
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h + 6}' role='img' "
           "aria-label='Calibration by risk decile: predicted and actual 30-day readmission rates, test period' "
           "font-family='Segoe UI, sans-serif'>"]
    for tick in range(0, 6):
        v = tick / 10
        out.append(f"<line x1='{left}' x2='{w - 10}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='{RULE}' stroke-width='1'/>")
        out.append(f"<text x='{left - 6}' y='{y(v) + 3.5:.1f}' text-anchor='end' font-size='10' fill='{MUTED}'>{tick * 10}%</text>")
    slot = (w - 10 - left) / len(rows)
    for decile, predicted, actual in rows:
        x0 = left + (decile - 1) * slot + slot / 2 - 24
        for i, (v, col) in enumerate(((predicted, INK), (actual, OCHRE))):
            out.append(f"<rect x='{x0 + i * 26:.1f}' y='{y(v):.1f}' width='23' height='{bottom - y(v):.1f}' fill='{col}'/>")
        out.append(f"<text x='{x0 + 24.5:.1f}' y='188' text-anchor='middle' font-size='10' fill='{MUTED}'>{decile}</text>")
    out.append(f"<text x='{(left + w - 10) / 2:.1f}' y='{h + 2}' text-anchor='middle' font-size='10' fill='{MUTED}'>"
               "risk decile (1 = lowest scores)</text></svg>")
    path = ROOT / "docs" / "img" / "calibration.svg"
    path.write_text("".join(out), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    calibration()
