"""Break the built data on purpose, one rule at a time, and confirm the matching check fails.

Works on a copy of data/hc.duckdb, so the real build is untouched. Run after src/pipeline.py.
"""
import re
import shutil
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB, COPY = ROOT / "data" / "hc.duckdb", ROOT / "data" / "mutation.duckdb"

MUTATIONS = {
    "resent ER rows are removed and nothing else is":
        "INSERT INTO stg_er_visits SELECT * FROM stg_er_visits LIMIT 1",
    "ER timestamps run in order":
        "UPDATE fact_er_visits SET seen_ts = arrival_ts - INTERVAL 5 MINUTE WHERE visit_id = (SELECT min(visit_id) FROM fact_er_visits WHERE seen_ts IS NOT NULL)",
    "patients who left without being seen were never seen":
        "UPDATE fact_er_visits SET seen_ts = departure_ts WHERE visit_id = (SELECT min(visit_id) FROM fact_er_visits WHERE left_unseen)",
    "every admitted ER visit has exactly one admission, starting when the patient left the ER":
        "UPDATE fact_admissions SET admit_ts = admit_ts + INTERVAL 1 MINUTE WHERE admission_id = (SELECT min(admission_id) FROM fact_admissions WHERE er_visit_id IS NOT NULL)",
    "no bed holds two patients at once":
        "UPDATE fact_bed_stays SET bed_id = (SELECT bed_id FROM fact_bed_stays WHERE stay_id = 'S0000001') WHERE stay_id = 'S0000002'",
    "a bed is cleaned before the next patient gets it":
        "UPDATE fact_bed_stays SET ready_ts = ready_ts + INTERVAL 30 DAY WHERE stay_id = 'S0000001'",
    "each admission's bed stays join end to end from admission to discharge":
        "UPDATE fact_bed_stays SET end_ts = end_ts - INTERVAL 10 MINUTE WHERE stay_id = (SELECT min(stay_id) FROM fact_bed_stays WHERE end_ts IS NOT NULL)",
    "beds are only used while in service":
        "UPDATE dim_bed SET in_service_from = DATE '2026-01-01' WHERE bed_id = 'ICU-A01'",
    "hourly census never exceeds beds in service":
        "UPDATE fact_unit_hourly SET occupied = beds_in_service + 1 WHERE hour_ts = TIMESTAMP '2024-01-01 10:00' AND unit = 'ICU'",
    "hourly census matches a direct count of bed stays":
        "UPDATE fact_unit_hourly SET occupied = occupied - 1 WHERE hour_ts = TIMESTAMP '2024-01-01 11:00' AND unit = 'Surgery'",
    "ER waiting count matches a direct count of visits":
        "UPDATE fact_unit_hourly SET waiting = waiting + 1 WHERE hour_ts = TIMESTAMP '2024-01-01 21:00' AND unit = 'ER'",
    "the roster covers every hour of every unit":
        "UPDATE fact_unit_hourly SET nurses = NULL WHERE hour_ts = TIMESTAMP '2024-01-01 03:00' AND unit = 'ICU'",
    "index discharges exclude deaths, transfers and discharges against advice":
        "UPDATE fact_admissions SET discharge_status = 'Died' WHERE admission_id = (SELECT min(admission_id) FROM feat_index_discharges)",
    "readmission flag matches a direct recount":
        "UPDATE feat_index_discharges SET readmitted_30d = 1 - readmitted_30d WHERE admission_id = (SELECT min(admission_id) FROM feat_index_discharges WHERE outcome_known)",
    "discharges without 30 days of follow-up carry no outcome":
        "UPDATE feat_index_discharges SET readmitted_30d = 0 WHERE admission_id = (SELECT min(admission_id) FROM feat_index_discharges WHERE NOT outcome_known)",
    "LACE parts stay in range and add up":
        "UPDATE feat_index_discharges SET lace = lace + 1 WHERE admission_id = (SELECT min(admission_id) FROM feat_index_discharges)",
    "the model never sees sex, payer or age group":
        "INSERT INTO model_features VALUES ('sex', 'categorical')",
    "every discharge home has a risk score":
        "DELETE FROM readmission_scores WHERE admission_id = (SELECT min(admission_id) FROM readmission_scores)",
}


def main():
    text = (ROOT / "sql" / "checks.sql").read_text(encoding="utf-8")
    checks = dict(re.findall(r"-- name: ([^\n]+)\n(.*?;)", text, flags=re.S))
    assert set(checks) == set(MUTATIONS), set(checks) ^ set(MUTATIONS)
    missed = 0
    for name, mutation in MUTATIONS.items():
        shutil.copy(DB, COPY)
        con = duckdb.connect(str(COPY))
        assert not con.sql(checks[name]).fetchall(), f"check fails before mutation: {name}"
        con.execute(mutation)
        caught = bool(con.sql(checks[name]).fetchall())
        missed += not caught
        print(f"  {'caught' if caught else 'MISSED'}  {name}")
        con.close()
    COPY.unlink()
    print(f"{len(MUTATIONS) - missed} of {len(MUTATIONS)} mutations caught")
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
