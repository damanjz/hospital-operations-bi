"""Build the DuckDB model, train the readmission model, run the checks, export for Power BI."""
import os
import re
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "hc.duckdb"
OUT = ROOT / "data" / "model"
EXPORT = ["dim_date", "dim_hour", "dim_unit", "dim_bed", "fact_bed_stays", "fact_unit_hourly", "mart_er_visits",
          "mart_discharges", "model_metrics", "model_calibration", "model_fairness", "model_importance"]


def run_sql(con, name):
    con.execute((ROOT / "sql" / name).read_text(encoding="utf-8"))
    print(f"built {name}")


def run_checks(con):
    text = (ROOT / "sql" / "checks.sql").read_text(encoding="utf-8")
    failed = 0
    for name, sql in re.findall(r"-- name: ([^\n]+)\n(.*?;)", text, flags=re.S):
        bad = con.sql(sql).fetchall()
        failed += bool(bad)
        print(f"  {'FAIL' if bad else 'pass'}  {name}" + (f"  ({len(bad)} rows, e.g. {bad[0]})" if bad else ""))
    return failed


def main():
    os.chdir(ROOT)
    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    for name in ("01_staging.sql", "02_model.sql", "03_features.sql"):
        run_sql(con, name)
    metrics = model.run(con)
    chosen = metrics[metrics.chosen].iloc[0]
    lace = metrics[metrics.model == "LACE index"].iloc[0]
    print(f"trained model: {chosen.model}, test ROC AUC {chosen.roc_auc:.3f} (LACE {lace.roc_auc:.3f}), "
          f"top-decile capture {chosen.top_decile_capture:.1%}")
    run_sql(con, "04_marts.sql")
    print("checks:")
    failed = run_checks(con)
    if failed:
        print(f"{failed} checks failed; nothing exported")
        con.close()
        sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    for t in EXPORT:
        con.execute(f"COPY (SELECT * FROM {t} ORDER BY ALL) TO '{(OUT / (t + '.csv')).as_posix()}' (HEADER)")
    print(f"exported {len(EXPORT)} tables to data/model")
    con.close()


if __name__ == "__main__":
    main()
