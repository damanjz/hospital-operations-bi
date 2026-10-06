"""Compare every report number with the same figure computed directly in DuckDB.

Needs the report open in its own Power BI window: powerbi/open_pbip.ps1 -Keep.
Each case runs a DAX query against Power BI's engine and an independent SQL query against
data/hc.duckdb, and prints both. Exit code 1 if any pair differs.
"""
import json
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
con = duckdb.connect(str(ROOT / "data" / "hc.duckdb"), read_only=True)


def dax(expr):
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(ROOT / "powerbi" / "query_desktop.ps1"), "-Dax", f'EVALUATE ROW("v", {expr})'],
                         capture_output=True, text=True, check=True).stdout.strip()
    row = json.loads(out)
    row = row[0] if isinstance(row, list) else row
    return list(row.values())[0]


def sql(query):
    return con.sql(query).fetchone()[0]


ER = "SELECT * FROM fact_er_visits v JOIN dim_date d ON d.date = v.arrival_date"
UH = "SELECT * FROM fact_unit_hourly h JOIN dim_date d USING (date)"
DS = "SELECT * FROM mart_discharges m JOIN dim_date d ON d.date = m.discharge_date"


def asof(month, day, hour):
    return f"""'As-of month'[Label] = "{month}", 'As-of day'[Day] = {day}, 'As-of hour'[Hour] = {hour}"""


def flow_cases(label, dax_filter, where_er, where_uh):
    f = f", {dax_filter}" if dax_filter else ""
    return [
        (f"{label}: median wait", f"CALCULATE([Median wait]{f})", f"SELECT median(wait_min) FROM ({ER}) WHERE {where_er}"),
        (f"{label}: left unseen", f"CALCULATE([Left without being seen]{f})",
         f"SELECT avg(left_unseen::INT) FROM ({ER}) WHERE {where_er}"),
        (f"{label}: average boarding h", f"CALCULATE([Average boarding]{f})",
         f"SELECT avg(boarding_min) / 60 FROM ({ER}) WHERE {where_er}"),
        (f"{label}: bed occupancy", f"CALCULATE([Bed occupancy]{f})",
         f"SELECT sum(occupied) / sum(beds_in_service) FROM ({UH}) WHERE {where_uh}"),
        (f"{label}: patients per nurse", f"CALCULATE([Patients per nurse]{f})",
         f"SELECT sum(patients) / sum(nurses) FROM ({UH}) WHERE {where_uh}"),
    ]


def readmit_cases(label, dax_filter, where):
    f = f", {dax_filter}" if dax_filter else ""
    return [
        (f"{label}: readmission rate", f"CALCULATE([Readmission rate]{f})", f"SELECT avg(readmitted_30d) FROM ({DS}) WHERE {where}"),
        (f"{label}: discharges home", f"CALCULATE([Discharges home]{f})", f"SELECT count(readmitted_30d) FROM ({DS}) WHERE {where}"),
        (f"{label}: readmissions", f"CALCULATE([Readmissions]{f})", f"SELECT sum(readmitted_30d) FROM ({DS}) WHERE {where}"),
        (f"{label}: LACE catch rate", f"CALCULATE([LACE catch rate]{f})",
         f"SELECT sum(readmitted_30d) FILTER (WHERE lace_high) / sum(readmitted_30d) FROM ({DS}) WHERE {where}"),
    ]


def now_cases(month, day, hour, ts):
    a = asof(month, day, hour)
    hu = f"FROM fact_unit_hourly WHERE hour_ts = TIMESTAMP '{ts}'"
    waiting = f"""FROM fact_er_visits WHERE arrival_ts <= TIMESTAMP '{ts}'
                  AND (coalesce(seen_ts, departure_ts) IS NULL OR coalesce(seen_ts, departure_ts) > TIMESTAMP '{ts}')"""
    cases = [
        (f"{ts}: occupied", f"CALCULATE([Occupied now], {a})", f"SELECT sum(occupied) {hu}"),
        (f"{ts}: beds", f"CALCULATE([Beds now], {a})", f"SELECT sum(beds_in_service) {hu}"),
        (f"{ts}: cleaning", f"CALCULATE([Cleaning now], {a})", f"SELECT sum(cleaning) {hu}"),
        (f"{ts}: ER waiting", f"CALCULATE([ER waiting now], {a})", f"SELECT count(*) {waiting}"),
        (f"{ts}: longest wait", f"CALCULATE([Longest wait now], {a})",
         f"SELECT date_diff('minute', min(arrival_ts), TIMESTAMP '{ts}') {waiting}"),
        (f"{ts}: boarding", f"CALCULATE([Boarding now], {a})", f"SELECT sum(boarding) {hu} AND unit = 'ER'"),
        (f"{ts}: occupied cells on the bed board",
         f"CALCULATE(COUNTROWS(FILTER(VALUES(Bed[bed_id]), [Bed status] = \"Occupied\")), {a})",
         f"""SELECT count(*) FROM fact_bed_stays WHERE start_ts <= TIMESTAMP '{ts}'
             AND (end_ts IS NULL OR end_ts > TIMESTAMP '{ts}')"""),
    ]
    for unit in ("ER", "ICU", "Surgery", "General Ward"):
        cases.append((f"{ts}: patients per nurse, {unit}", f'CALCULATE([Patients per nurse now], Unit[unit] = "{unit}", {a})',
                      f"SELECT patients / nurses {hu} AND unit = '{unit}'"))
    return cases


CASES = (
    flow_cases("all", "", "true", "true")
    + flow_cases("FY 2024-25", "'Date'[fiscal_year] = \"FY 2024-25\"", "fiscal_year = 'FY 2024-25'", "fiscal_year = 'FY 2024-25'")
    + flow_cases("FY 2025-26, General Ward", "'Date'[fiscal_year] = \"FY 2025-26\", Unit[unit] = \"General Ward\"",
                 "fiscal_year = 'FY 2025-26'", "fiscal_year = 'FY 2025-26' AND unit = 'General Ward'")
    + [("evening shift median wait, FY 2023-24", "CALCULATE([Median wait], Hour[shift] = \"Evening\", 'Date'[fiscal_year] = \"FY 2023-24\")",
        f"SELECT median(wait_min) FROM ({ER}) JOIN dim_hour h ON h.hour = arrival_hour WHERE shift = 'Evening' AND fiscal_year = 'FY 2023-24'"),
       ("Monday 19:00 arrivals per hour", "CALCULATE([Arrivals per hour], 'Date'[weekday] = \"Mon\", Hour[hour] = 19)",
        f"SELECT count(*) / (SELECT count(*) FROM dim_date WHERE weekday = 'Mon') FROM ({ER}) WHERE weekday = 'Mon' AND arrival_hour = 19")]
    + readmit_cases("all", "", "true")
    + readmit_cases("FY 2023-24, 75+", "'Date'[fiscal_year] = \"FY 2023-24\", Discharges[age_group] = \"75+\"",
                    "fiscal_year = 'FY 2023-24' AND age_group = '75+'")
    + readmit_cases("Surgery", "Unit[unit] = \"Surgery\"", "discharge_unit = 'Surgery'")
    + [("full units readmission rate", "CALCULATE([Readmission rate], Discharges[occupancy_band] = \"95% or more\")",
        "SELECT avg(readmitted_30d) FROM mart_discharges WHERE occupancy_band = '95% or more'")]
    + now_cases("Sep 2024", 16, 20, "2024-09-16 20:00:00")
    + now_cases("Mar 2026", 31, 8, "2026-03-31 08:00:00")
    + now_cases("Aug 2023", 14, 21, "2023-08-14 21:00:00")
)


def main():
    bad = 0
    for name, d, q in CASES:
        a, b = dax(d), sql(q)
        same = (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-9)
        bad += not same
        print(f"  {'ok  ' if same else 'DIFF'}  {name:55s} Power BI {a!s:>22}   DuckDB {b!s:>22}")
    print(f"{len(CASES) - bad} of {len(CASES)} match")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
