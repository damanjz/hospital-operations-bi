"""Generate the Power BI project (PBIP): TMDL semantic model + PBIR report.

The model imports the CSVs in data/model. Column types come from the DuckDB schema, so re-running
the pipeline and this script keeps the two in sync.

Three pages in two modes. Now: the hospital at one chosen hour (bed board, ER queue, staffing).
Review: patient flow and readmissions over a chosen period.
"""
import argparse
import hashlib
from datetime import datetime
import json
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi"
NAME = "PatientFlow"
SM = OUT / f"{NAME}.SemanticModel"
RP = OUT / f"{NAME}.Report"
DATA_FOLDER = str(ROOT / "data" / "model") + "\\"
PORTABLE_DATA_FOLDER = "C:\\hospital-operations-bi\\data\\model\\"

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

# ---------------------------------------------------------------- palette (same editorial system as Finance BI)
INK, OCHRE, TEAL, RUST, PLUM = "#2f55b0", "#c27a1a", "#1f8f7a", "#c0533a", "#8456b0"
PAPER, RAIL, RULE = "#fbfaf7", "#f2efe7", "#e3ded2"
TEXT, MUTED = "#1d2330", "#5b6170"
SERIF, SANS = "Georgia", "Segoe UI"
FREE, CLEANING, OCCUPIED, CLOSED = "#e3ded2", OCHRE, INK, "#fbfaf7"
UNIT_COLORS = {"ICU": RUST, "Surgery": TEAL, "General Ward": INK}
SHIFT_COLORS = {"Morning": "#9aa6c8", "Evening": RUST, "Night": TEXT}
# sequential ramp for heatmaps, paper to ink
RAMP = ["#f2efe7", "#dfe2ea", "#c6cee2", "#a6b5d8", "#8299cb", "#5d7cbe", "#3f62b4", "#2a4a96"]

# default moment for the Now page: a Monday evening in dengue season
ASOF_MONTH, ASOF_DAY, ASOF_HOUR = "2024-09-01", 16, 20
PERIOD = None             # default for the Period slicers (None = all); --period sets one for screenshots

# ---------------------------------------------------------------- model
TABLES = {  # model table -> (csv / duckdb table, visible columns)
    "Date": ("dim_date", {"date", "month", "fiscal_year", "weekday"}),
    "Hour": ("dim_hour", {"hour", "hour_label", "shift"}),
    "Unit": ("dim_unit", {"unit"}),
    "Bed": ("dim_bed", {"bed_id", "unit", "bay_label", "slot"}),
    "Bed Stays": ("fact_bed_stays", set()),
    "Unit Hours": ("fact_unit_hourly", set()),
    "ER Visits": ("mart_er_visits", {"triage_label", "complaint", "disposition"}),
    "Discharges": ("mart_discharges", {"diagnosis", "age_group", "los_band", "occupancy_band", "discharge_date",
                                       "age", "lace", "risk_percentile", "drivers", "discharge_unit", "outcome_known"}),
    "Model Metrics": ("model_metrics", set()),
}
SORT_BY = {("Date", "weekday"): "weekday_num", ("Date", "month_label"): "month_index",
           ("Unit", "unit"): "unit_order", ("Discharges", "los_band"): "los_band_order",
           ("Discharges", "occupancy_band"): "occupancy_band_order"}
RELATIONSHIPS = [("ER Visits", "arrival_date", "Date", "date"), ("Unit Hours", "date", "Date", "date"),
                 ("Discharges", "discharge_date", "Date", "date"), ("ER Visits", "arrival_hour", "Hour", "hour"),
                 ("Unit Hours", "hour", "Hour", "hour"), ("Unit Hours", "unit", "Unit", "unit"),
                 ("Discharges", "discharge_unit", "Unit", "unit"), ("Bed", "unit", "Unit", "unit"),
                 ("Bed Stays", "bed_id", "Bed", "bed_id")]

PCT = "0.0%"
MIN = '0" min"'
ASOF = "VAR t = [As-of time]"


def ramp(value, lo, hi):
    """DAX: pick a colour from RAMP by where value sits between lo and hi."""
    cases = ", ".join(f'i = {i}, "{c}"' for i, c in enumerate(RAMP))
    return f"""
        VAR i = IF ( ISBLANK ( {value} ), BLANK (), INT ( MIN ( {len(RAMP) - 1}, MAX ( 0, DIVIDE ( {value} - {lo}, {hi} - {lo} ) * {len(RAMP)} ) ) ) )
        RETURN SWITCH ( TRUE (), ISBLANK ( i ), "{PAPER}", {cases} )"""


MEASURES = [
    # ---------------- Now
    ("As-of time", f"""
        VAR m = SELECTEDVALUE ( 'As-of month'[Month], DATE ( {ASOF_MONTH[:4]}, {int(ASOF_MONTH[5:7])}, 1 ) )
        VAR d = MIN ( SELECTEDVALUE ( 'As-of day'[Day], {ASOF_DAY} ), DAY ( EOMONTH ( m, 0 ) ) )
        VAR h = SELECTEDVALUE ( 'As-of hour'[Hour], {ASOF_HOUR} )
        RETURN m + d - 1 + TIME ( h, 0, 0 )""", "ddd d mmm yyyy hh:nn", "The moment shown on the Now page."),
    ("Bed status", f"""
        {ASOF}
        VAR opened = SELECTEDVALUE ( Bed[in_service_from] )
        VAR occ =
            CALCULATE (
                COUNTROWS ( 'Bed Stays' ),
                'Bed Stays'[start_ts] <= t,
                ISBLANK ( 'Bed Stays'[end_ts] ) || 'Bed Stays'[end_ts] > t
            )
        VAR cleaning =
            CALCULATE (
                COUNTROWS ( 'Bed Stays' ),
                NOT ISBLANK ( 'Bed Stays'[end_ts] ),
                'Bed Stays'[end_ts] <= t,
                ISBLANK ( 'Bed Stays'[ready_ts] ) || 'Bed Stays'[ready_ts] > t
            )
        RETURN
            IF (
                HASONEVALUE ( Bed[bed_id] ),
                SWITCH ( TRUE (), opened > t, "Not yet open", occ > 0, "Occupied", cleaning > 0, "Being cleaned", "Free" )
            )""", None, "State of one bed at the as-of time."),
    ("Bed cell", 'IF ( NOT ISBLANK ( [Bed status] ), "o" )', None, "A character drawn in the cell colour, so every bed is a full-height cell."),
    ("Bed colour", f"""
        SWITCH ( [Bed status], "Occupied", "{OCCUPIED}", "Being cleaned", "{CLEANING}", "Free", "{FREE}", "{CLOSED}" )""",
     None, "Colour of a bed cell."),
    ("Occupied now", f"""
        {ASOF}
        RETURN CALCULATE ( SUM ( 'Unit Hours'[occupied] ), 'Unit Hours'[hour_ts] = t )""", "0",
     "Beds with a patient in them at the as-of hour."),
    ("Beds now", f"""
        {ASOF}
        RETURN CALCULATE ( SUM ( 'Unit Hours'[beds_in_service] ), 'Unit Hours'[hour_ts] = t )""", "0",
     "Beds in service at the as-of hour."),
    ("Cleaning now", f"""
        {ASOF}
        RETURN CALCULATE ( SUM ( 'Unit Hours'[cleaning] ), 'Unit Hours'[hour_ts] = t )""", "0",
     "Beds emptied and waiting for housekeeping."),
    ("Free now", "[Beds now] - [Occupied now] - [Cleaning now]", "0", "Beds clean and ready for a patient."),
    ("Occupancy now", "DIVIDE ( [Occupied now], [Beds now] )", "0%", "Occupied beds as a share of beds in service."),
    ("Beds label", '[Occupied now] & " of " & [Beds now]', None, "Occupied of total beds."),
    ("ER waiting now", f"""
        {ASOF}
        RETURN
            CALCULATE (
                COUNTROWS ( 'ER Visits' ),
                'ER Visits'[arrival_ts] <= t,
                'ER Visits'[arrival_ts] > t - 2,
                FILTER (
                    'ER Visits',
                    VAR seenOrLeft = IF ( ISBLANK ( 'ER Visits'[seen_ts] ), 'ER Visits'[departure_ts], 'ER Visits'[seen_ts] )
                    RETURN ISBLANK ( seenOrLeft ) || seenOrLeft > t
                )
            ) + 0""", "0", "Patients in the ER not yet seen by a doctor."),
    ("Boarding now", f"""
        {ASOF}
        RETURN CALCULATE ( SUM ( 'Unit Hours'[boarding] ), 'Unit Hours'[hour_ts] = t, 'Unit Hours'[unit] = "ER" )""", "0",
     "Admitted patients still in the ER, waiting for a bed."),
    ("Longest wait now", f"""
        {ASOF}
        VAR waiting =
            CALCULATETABLE (
                'ER Visits',
                'ER Visits'[arrival_ts] <= t,
                'ER Visits'[arrival_ts] > t - 2,
                FILTER (
                    'ER Visits',
                    VAR seenOrLeft = IF ( ISBLANK ( 'ER Visits'[seen_ts] ), 'ER Visits'[departure_ts], 'ER Visits'[seen_ts] )
                    RETURN ISBLANK ( seenOrLeft ) || seenOrLeft > t
                )
            )
        RETURN IF ( COUNTROWS ( waiting ) > 0, ROUND ( ( t - MINX ( waiting, 'ER Visits'[arrival_ts] ) ) * 1440, 0 ) )""", MIN,
     "Minutes the longest-waiting ER patient has been waiting."),
    ("Patients per nurse now", f"""
        {ASOF}
        RETURN
            DIVIDE (
                CALCULATE ( SUM ( 'Unit Hours'[patients] ), 'Unit Hours'[hour_ts] = t ),
                CALCULATE ( SUM ( 'Unit Hours'[nurses] ), 'Unit Hours'[hour_ts] = t )
            )""", "0.0", "Patients per nurse on duty at the as-of hour."),
    ("Nurse ratio target", "MAX ( Unit[patients_per_nurse_target] )", "0.0", "Planned patients per nurse for the unit."),
    ("As-of heading", f"""
        {ASOF}
        RETURN FORMAT ( t, "dddd d mmmm yyyy" ) & ", " & FORMAT ( t, "hh:nn" )""", None, "The as-of moment as a heading."),
    ("Insight: now", f"""
        {ASOF}
        RETURN
            FORMAT ( t, "hh:nn" ) & " on " & FORMAT ( t, "dddd d mmmm yyyy" ) & ": " & [Occupied now] & " of " & [Beds now]
                & " beds are taken, " & [Free now] & " are ready and " & [ER waiting now]
                & " people are waiting to see an ER doctor." """, None, "One-line reading of the Now page."),

    # ---------------- Flow
    ("Median wait", "MEDIAN ( 'ER Visits'[wait_min] )", MIN, "Median minutes from ER arrival to first doctor."),
    ("Left without being seen", """
        DIVIDE (
            CALCULATE ( COUNTROWS ( 'ER Visits' ), 'ER Visits'[left_unseen] = TRUE () ),
            CALCULATE ( COUNTROWS ( 'ER Visits' ), NOT ISBLANK ( 'ER Visits'[left_unseen] ) )
        )""", PCT, "Share of finished ER visits where the patient left before a doctor saw them."),
    ("ER arrivals", "COUNTROWS ( 'ER Visits' )", "#,0", "ER arrivals."),
    ("Arrivals per hour", "DIVIDE ( COUNTROWS ( 'ER Visits' ), COUNTROWS ( 'Date' ) )", "0.0",
     "Average ER arrivals in an hour: arrivals divided by the days in context."),
    ("Bed occupancy", "DIVIDE ( SUM ( 'Unit Hours'[occupied] ), SUM ( 'Unit Hours'[beds_in_service] ) )", PCT,
     "Average of hourly census divided by beds in service."),
    ("Patients per nurse", "DIVIDE ( SUM ( 'Unit Hours'[patients] ), SUM ( 'Unit Hours'[nurses] ) )", "0.0",
     "Patients per nurse on duty, averaged over every hour."),
    ("Average boarding", "DIVIDE ( AVERAGE ( 'ER Visits'[boarding_min] ), 60 )", '0.0" h"',
     "Average hours an admitted patient waited in the ER for a bed."),
    ("Arrivals colour", ramp("[Arrivals per hour]", 0, 13), None, "Heatmap colour for arrivals per hour."),
    ("Wait colour", ramp("[Median wait]", 0, 150), None, "Heatmap colour for the median wait."),
    ("Heat cell", 'FORMAT ( [Arrivals per hour], "0" )', None, "Rounded arrivals per hour for the heatmap."),
    ("Wait cell", 'FORMAT ( [Median wait], "0" )', None, "Rounded median wait for the heatmap."),
    ("Insight: flow", """
        VAR eve = CALCULATE ( [Median wait], Hour[shift] = "Evening" )
        VAR morn = CALCULATE ( [Median wait], Hour[shift] = "Morning" )
        RETURN
            "Evening arrivals wait a median " & FORMAT ( eve, "0" ) & " min for a doctor, morning arrivals "
                & FORMAT ( morn, "0" ) & " min. Beds peak in the Aug to Oct dengue season." """, None,
     "One-line reading of the flow page."),

    # ---------------- Readmissions
    ("Readmission rate", "DIVIDE ( SUM ( Discharges[readmitted_30d] ), COUNT ( Discharges[readmitted_30d] ) )", PCT,
     "Share of patients sent home who had an emergency readmission within 30 days."),
    ("Discharges home", "COUNT ( Discharges[readmitted_30d] )", "#,0", "Discharges home with 30 days of follow-up."),
    ("Readmissions", "SUM ( Discharges[readmitted_30d] )", "#,0", "Emergency readmissions within 30 days."),
    ("LACE catch rate", """
        DIVIDE ( CALCULATE ( SUM ( Discharges[readmitted_30d] ), Discharges[lace_high] = TRUE () ), SUM ( Discharges[readmitted_30d] ) )""",
     "0%", "Share of readmissions the LACE high-risk flag (score 10 or more) had caught."),
    ("Model vs LACE", """
        VAR m = CALCULATE ( MAX ( 'Model Metrics'[roc_auc] ), 'Model Metrics'[chosen] = TRUE () )
        VAR l = CALCULATE ( MAX ( 'Model Metrics'[roc_auc] ), 'Model Metrics'[model] = "LACE index" )
        RETURN FORMAT ( m, "0.00" ) & " vs " & FORMAT ( l, "0.00" )""", None,
     "Out-of-time ROC AUC of the chosen model against the LACE index."),
    ("Risk percentile", "MAX ( Discharges[risk_percentile] )", "0%", "Where the patient's risk score ranks among all discharges."),
    ("Insight: readmissions", """
        VAR full = CALCULATE ( [Readmission rate], Discharges[occupancy_band] = "95% or more" )
        VAR calm = CALCULATE ( [Readmission rate], Discharges[occupancy_band] = "Under 85%" )
        RETURN
            "Sent home from a unit 95% full or more: " & FORMAT ( full, "0.0%" ) & " come back. Under 85% full: "
                & FORMAT ( calm, "0.0%" ) & "." """, None, "One-line reading of the readmissions page."),
]

CALC_TABLES = {
    "Metrics": ("{ 1 }", [("Value", "int64", "[Value]", True, None)]),
    "As-of month": ("""
            SELECTCOLUMNS (
                FILTER ( VALUES ( 'Date'[month] ), 'Date'[month] < DATE ( 2026, 4, 1 ) ),
                "Month", 'Date'[month],
                "Label", FORMAT ( 'Date'[month], "mmm yyyy" )
            )""", [("Month", "dateTime", "[Month]", True, "Short Date"), ("Label", "string", "[Label]", False, None)]),
    "As-of day": ('SELECTCOLUMNS ( GENERATESERIES ( 1, 31, 1 ), "Day", [Value] )',
                  [("Day", "int64", "[Day]", False, "0")]),
    "As-of hour": ('SELECTCOLUMNS ( GENERATESERIES ( 0, 23, 1 ), "Hour", [Value] )',
                   [("Hour", "int64", "[Hour]", False, "0")]),
}

TYPE_MAP = {"DATE": ("dateTime", "type date", "Short Date"), "TIMESTAMP": ("dateTime", "type datetime", "General Date"),
            "VARCHAR": ("string", "type text", None), "BIGINT": ("int64", "Int64.Type", "0"),
            "INTEGER": ("int64", "Int64.Type", "0"), "HUGEINT": ("int64", "Int64.Type", "0"),
            "DOUBLE": ("double", "type number", "#,0.00"), "FLOAT": ("double", "type number", "#,0.00"),
            "BOOLEAN": ("boolean", "type logical", None)}


def fmt_value(fmt):
    """TMDL needs values containing quotes wrapped in quotes, with inner quotes doubled."""
    return '"' + fmt.replace('"', '""') + '"' if '"' in fmt else fmt


def q(name):
    return f"'{name}'" if any(c in name for c in " .=:'-()") else name


def tmdl_type(duck_type):
    if duck_type.startswith("DECIMAL"):
        return TYPE_MAP["DOUBLE"]
    return TYPE_MAP[duck_type]


def indent(text, tabs):
    lines = [l for l in text.strip("\n").splitlines()]
    pad = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    return "\n".join("\t" * tabs + l[pad:] for l in lines)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path, obj):
    write(path, json.dumps(obj, indent=2, ensure_ascii=False))


def build_model(con):
    d = SM / "definition"
    write_json(SM / "definition.pbism", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}})
    write_json(SM / ".platform", platform("SemanticModel"))
    write(d / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n")
    refs = "\n".join(f"ref table {q(t)}" for t in list(TABLES) + list(CALC_TABLES))
    write(d / "model.tmdl", f"""model Model
\tculture: en-US
\tdefaultPowerBIDataSourceVersion: powerBI_V3
\tdiscourageImplicitMeasures
\tsourceQueryCulture: en-US
\tdataAccessOptions
\t\tlegacyRedirects
\t\treturnErrorValuesAsNull

annotation __PBI_TimeIntelligenceEnabled = 0

annotation PBI_ProTooling = ["TMDL-Extension"]

{refs}
""")
    write(d / "expressions.tmdl",
          f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')

    for table, (source, visible) in TABLES.items():
        cols = con.sql(f"DESCRIBE {source}").fetchall()
        out = [f"table {q(table)}", ""]
        for col, ctype, *_ in cols:
            dtype, _, fmt = tmdl_type(ctype)
            if col.endswith("_ts"):
                fmt = "yyyy-mm-dd hh:nn"
            elif col == "discharge_date":
                fmt = "d mmm"
            elif col.endswith("_date"):
                fmt = "d mmm yyyy"
            elif col == "risk_percentile":
                fmt = "0.0%"
            out.append(f"\tcolumn {q(col)}")
            out.append(f"\t\tdataType: {dtype}")
            if fmt:
                out.append(f"\t\tformatString: {fmt_value(fmt)}")
            if col not in visible:
                out.append("\t\tisHidden")
            if (table, col) in SORT_BY:
                out.append(f"\t\tsortByColumn: {SORT_BY[(table, col)]}")
            out.append("\t\tsummarizeBy: none")
            out.append(f"\t\tsourceColumn: {col}")
            out.append("")
        types = ",\n".join(f'\t\t\t\t\t\t{{"{c}", {tmdl_type(t)[1]}}}' for c, t, *_ in cols)
        out.append(f"\tpartition {q(table)} = m")
        out.append("\t\tmode: import")
        out.append("\t\tsource =")
        out.append(f"""\t\t\t\tlet
\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & "{source}.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
\t\t\t\t    #"Promoted headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
\t\t\t\t    #"Typed columns" = Table.TransformColumnTypes(
\t\t\t\t        #"Promoted headers",
\t\t\t\t        {{
{types}
\t\t\t\t        }},
\t\t\t\t        "en-US"
\t\t\t\t    )
\t\t\t\tin
\t\t\t\t    #"Typed columns"
""")
        write(d / "tables" / f"{table}.tmdl", "\n".join(out))

    for table, (dax, cols) in CALC_TABLES.items():
        out = [f"table {q(table)}", ""]
        if table == "Metrics":
            for name, expr, fmt, desc in MEASURES:
                out.append(f"\t/// {desc}")
                if "\n" in expr.strip():
                    out.append(f"\tmeasure {q(name)} = ```")
                    out.append(indent(expr, 3))
                    out.append("\t\t\t```")
                else:
                    out.append(f"\tmeasure {q(name)} = {expr.strip()}")
                if fmt:
                    out.append(f"\t\tformatString: {fmt_value(fmt)}")
                out.append("")
        for col, dtype, src, hidden, fmt in cols:
            out.append(f"\tcolumn {q(col)}")
            out.append(f"\t\tdataType: {dtype}")
            if fmt:
                out.append(f"\t\tformatString: {fmt_value(fmt)}")
            if hidden:
                out.append("\t\tisHidden")
            if table == "As-of month" and col == "Label":
                out.append("\t\tsortByColumn: Month")
            out.append("\t\tsummarizeBy: none")
            out.append(f"\t\tsourceColumn: {src}")
            out.append("")
        out.append(f"\tpartition {q(table)} = calculated")
        out.append("\t\tmode: import")
        if "\n" in dax.strip():
            out.append("\t\tsource = ```")
            out.append(indent(dax, 4))
            out.append("\t\t\t\t```")
        else:
            out.append(f"\t\tsource = {dax}")
        out.append("")
        write(d / "tables" / f"{table}.tmdl", "\n".join(out))

    rels = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rid = uuid.uuid5(uuid.NAMESPACE_URL, f"{ft}.{fc}->{tt}.{tc}")
        rels.append(f"relationship {rid}\n\tfromColumn: {q(ft)}.{q(fc)}\n\ttoColumn: {q(tt)}.{q(tc)}\n")
    write(d / "relationships.tmdl", "\n".join(rels))


# ---------------------------------------------------------------- report helpers
def uid(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def platform(kind):
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": kind, "displayName": NAME},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NAME}-{kind}"))}}


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def s(text):
    return lit("'" + text.replace("'", "''") + "'")


def num(v, suffix="D"):
    return lit(f"{v}{suffix}")


def color(hex_):
    return {"solid": {"color": s(hex_)}}


def measure(name, entity="Metrics"):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def column(entity, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def measure_color(name):
    return {"solid": {"color": {"expr": measure(name)}}}


def proj(field, display=None):
    kind = "Measure" if "Measure" in field else "Column"
    ent = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    p = {"field": field, "queryRef": f"{ent}.{prop}", "nativeQueryRef": prop}
    if kind == "Column":
        p["active"] = True
    if display:
        p["displayName"] = display
    return p


def container(title=None, bg=None, pad=None):
    c = {"title": [{"properties": {"show": lit("true" if title else "false")}}],
         "background": [{"properties": {"show": lit("true" if bg else "false")}}],
         "border": [{"properties": {"show": lit("false")}}],
         "dropShadow": [{"properties": {"show": lit("false")}}],
         "visualHeader": [{"properties": {"show": lit("false")}}]}
    if title:
        c["title"][0]["properties"].update({"text": s(title), "fontFamily": s(SANS), "fontSize": num(11),
                                            "fontColor": color(MUTED), "bold": lit("false")})
    if bg:
        c["background"][0]["properties"].update({"color": color(bg), "transparency": num(0)})
    if pad is not None:
        c["padding"] = [{"properties": {k: num(pad) for k in ("top", "bottom", "left", "right")}}]
    return c


def categorical_filter(entity, col, values, name):
    return {"name": name, "field": column(entity, col), "type": "Categorical",
            "filter": {"Version": 2, "From": [{"Name": "f", "Entity": entity, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": col}}],
                           "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]},
            "howCreated": "User"}


class Page:
    def __init__(self, key, display):
        self.key, self.display, self.visuals, self.interactions = key, display, [], []

    def add(self, vid, x, y, w, h, visual, z=None, filters=None):
        v = {"$schema": f"{SCHEMA}/visualContainer/2.1.0/schema.json",
             "name": uid(self.key, vid),
             "position": {"x": x, "y": y, "z": z if z is not None else 1000 * (len(self.visuals) + 1),
                          "height": h, "width": w, "tabOrder": 1000 * (len(self.visuals) + 1)},
             "visual": visual}
        if filters:
            v["filterConfig"] = {"filters": filters}
        v["_folder"] = vid
        self.visuals.append(v)

    def no_filter(self, source, target):
        self.interactions.append({"source": uid(self.key, source), "target": uid(self.key, target), "type": "NoFilter"})


def textbox(paragraphs):
    return {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def run(text, font=SANS, size=10, col=TEXT, bold=False):
    style = {"fontFamily": font, "fontSize": f"{size}pt", "color": col}
    if bold:
        style["fontWeight"] = "bold"
    return {"value": text, "textStyle": style}


def kpi_cards(fields):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(m), d) for m, d in fields]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(len(fields), "L"),
                                           "style": s("Cards"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(TEXT), "fontSize": num(22), "fontFamily": s(SERIF),
                                          "labelDisplayUnits": num(1)},
                           "selector": {"id": "default"}}],
                "label": [{"properties": {"fontColor": color(MUTED), "fontSize": num(10), "fontFamily": s(SANS),
                                          "position": s("aboveValue")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("true"), "position": s("Left"), "color": color(RULE),
                                              "width": num(2)}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def insight(measure_name):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(measure_name))]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(1, "L"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(MUTED), "fontSize": num(11), "fontFamily": s(SANS),
                                          "horizontalAlignment": s("left")}, "selector": {"id": "default"}}],
                "label": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def axis_objects():
    return {"categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineShow": lit("false")}}],
            "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                          "gridlineColor": color(RULE), "gridlineThickness": num(1, "L")}}],
            "legend": [{"properties": {"show": lit("true"), "position": s("Top"), "labelColor": color(TEXT),
                                       "fontSize": num(9), "showTitle": lit("false")}}]}


def eq(entity, col, value):
    return {"Comparison": {"ComparisonKind": 0, "Left": column(entity, col), "Right": {"Literal": {"Value": f"'{value}'"}}}}


def series_colors(entity, col, mapping):
    return [{"properties": {"fill": color(hex_)}, "selector": {"data": [{"scopeId": eq(entity, col, value)}]}}
            for value, hex_ in mapping.items()]


def measure_colors(mapping):
    return [{"properties": {"fill": color(hex_)}, "selector": {"metadata": f"Metrics.{m}"}} for m, hex_ in mapping.items()]


def chart(vtype, category, values, title, series=None, extra=None, sort=None):
    qs = {"Category": {"projections": [proj(category)]},
          "Y": {"projections": [proj(measure(v)) for v in values]}}
    if series:
        qs["Series"] = {"projections": [proj(series)]}
    v = {"visualType": vtype, "query": {"queryState": qs}, "objects": axis_objects(),
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    if extra:
        v["objects"].update(extra)
    return v


def bars(category, value, title, fill=INK, sort=None, pct=True):
    return chart("barChart", category, [value], title, sort=sort, extra={
        "dataPoint": [{"properties": {"fill": color(fill)}}],
        "categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                         "maxMarginFactor": num(45, "L")}}],
        "valueAxis": [{"properties": {"show": lit("false"), "showAxisTitle": lit("false"), "start": num(0)}}],
        "labels": [{"properties": {"show": lit("true"), "color": color(MUTED), "fontSize": num(9)}}],
        "legend": [{"properties": {"show": lit("false")}}]})


def slicer(entity, col, title, mode, sync=None, default=None, default_text=None, suffix="D"):
    v = {"visualType": "slicer",
         "query": {"queryState": {"Values": {"projections": [proj(column(entity, col))]}}},
         "objects": {"data": [{"properties": {"mode": s(mode)}}],
                     "header": [{"properties": {"show": lit("true"), "text": s(title), "fontColor": color(MUTED),
                                                "fontFamily": s(SANS), "textSize": num(10)}}],
                     "items": [{"properties": {"fontColor": color(TEXT), "fontFamily": s(SANS), "textSize": num(11)}}]},
         "visualContainerObjects": container(), "drillFilterOtherVisuals": True}
    if default is not None or default_text is not None:
        right = {"Literal": {"Value": f"{default}{suffix}" if default is not None else f"'{default_text}'"}}
        if mode == "Dropdown":
            v["objects"]["selection"] = [{"properties": {"strictSingleSelect": lit("true")}}]
        elif default is not None:
            v["objects"]["data"][0]["properties"]["numericStart"] = num(default)
        v["objects"]["general"] = [{"properties": {"filter": {"filter": {
            "Version": 2, "From": [{"Name": "p", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": col}},
                "Right": right}}}]}}}}]
    if sync:
        v["syncGroup"] = {"groupName": sync, "fieldChanges": True, "filterChanges": True}
    return v


def matrix(rows, cols, value, back, title, fore=None, cell_px=None, font=11, hide_text=False, padding=3, header_font=8):
    """A grid of coloured cells: rows x columns, cell colour from a measure that returns a hex code."""
    v = {"visualType": "pivotTable",
         "query": {"queryState": {"Rows": {"projections": [proj(rows, " ")]},
                                  "Columns": {"projections": [proj(cols)]},
                                  "Values": {"projections": [proj(measure(value))]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(fore or TEXT), "fontSize": num(font), "fontFamily": s(SANS),
                                        "wordWrap": lit("false"),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER)}},
                        {"properties": {"backColor": measure_color(back),
                                        **({"fontColor": measure_color(back)} if hide_text else {})},
                         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"Metrics.{value}"}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(header_font), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("None"), "wordWrap": lit("false"),
                                               "alignment": s("Center"), "autoSizeColumnWidth": lit("false")}}],
             "rowHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                            "backColor": color(PAPER), "outline": s("None"), "showExpandCollapseButtons": lit("false")}}],
             "grid": [{"properties": {"gridVertical": lit("true"), "gridVerticalColor": color(PAPER), "gridVerticalWeight": num(2, "L"),
                                      "gridHorizontal": lit("true"), "gridHorizontalColor": color(PAPER), "gridHorizontalWeight": num(2, "L"),
                                      "outlineColor": color(PAPER), "rowPadding": num(padding, "L"), "textSize": num(font)}}],
             "subTotals": [{"properties": {"rowSubtotals": lit("false"), "columnSubtotals": lit("false")}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if cell_px:
        v["objects"]["columnWidth"] = [{"properties": {"value": num(cell_px)}, "selector": {"metadata": f"Metrics.{value}"}}]
    return v


def table(columns, title, sort=None, widths=None):
    v = {"visualType": "tableEx",
         "query": {"queryState": {"Values": {"projections": [proj(f, d) for f, d in columns]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(TEXT), "fontSize": num(9), "fontFamily": s(SANS),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER)}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("BottomOnly")}}],
             "grid": [{"properties": {"gridVertical": lit("false"), "gridHorizontal": lit("true"),
                                      "gridHorizontalColor": color(RULE), "outlineColor": color(RULE), "rowPadding": num(3, "L")}}],
             "total": [{"properties": {"totals": lit("false")}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    v["objects"]["values"][0]["properties"]["wordWrap"] = lit("true")
    v["objects"]["columnHeaders"][0]["properties"]["autoSizeColumnWidth"] = lit("false")
    if widths:
        v["objects"]["columnWidth"] = [
            {"properties": {"value": num(w)}, "selector": {"metadata": proj(f)["queryRef"]}}
            for (f, _), w in zip(columns, widths)]
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    return v


# ---------------------------------------------------------------- report pages
PAGES = [("now", "Now"), ("flow", "Flow"), ("readmissions", "Readmissions")]
X0, W = 24, 1232          # content column


def header(page):
    page.add("title", X0, 10, 420, 64, textbox([
        {"textRuns": [run("Patient flow", SERIF, 18, TEXT)]},
        {"textRuns": [run("Synthetic 256-bed hospital, Hyderabad. Apr 2023 to Mar 2026", SANS, 8, MUTED)]}]))

    def state(sel, col, bar):
        return {"text": {"properties": {"fontFamily": s(SERIF), "fontSize": num(11), "fontColor": color(col)},
                         "selector": {"id": sel}},
                "fill": {"properties": {"show": lit("true"), "fillColor": color(PAPER), "transparency": num(0)},
                         "selector": {"id": sel}},
                "accentBar": {"properties": {"show": lit(bar), "position": s("Bottom"), "color": color(INK),
                                             "width": num(2, "L")}, "selector": {"id": sel}}}
    states = [state("default", MUTED, "false"), state("hover", TEXT, "false"), state("press", TEXT, "false"),
              state("selected", INK, "true")]
    nav = {k: [st[k] for st in states] for k in ("text", "fill", "accentBar")}
    nav["outline"] = [{"properties": {"show": lit("false")}}]
    nav["layout"] = [{"properties": {"orientation": num(0)}}]
    page.add("nav", 716, 14, 540, 48, {"visualType": "pageNavigator", "objects": nav,
             "visualContainerObjects": container(), "drillFilterOtherVisuals": True})


def build_pages():
    pages = []

    # ------------------------------------------------ Now
    p = Page("now", "Now")
    header(p)
    month_label = datetime.strptime(ASOF_MONTH, "%Y-%m-%d").strftime("%b %Y")
    p.add("asofMonth", X0, 80, 150, 56, slicer("As-of month", "Label", "Month", "Dropdown", default_text=month_label))
    p.add("asofDay", X0 + 166, 80, 100, 56, slicer("As-of day", "Day", "Day", "Dropdown", default=ASOF_DAY, suffix="L"))
    p.add("asofHour", X0 + 282, 80, 100, 56, slicer("As-of hour", "Hour", "Hour", "Dropdown", default=ASOF_HOUR, suffix="L"))
    heading = insight("As-of heading")
    heading["objects"]["value"][0]["properties"].update({"fontColor": color(TEXT), "fontSize": num(18), "fontFamily": s(SERIF)})
    p.add("heading", X0 + 420, 84, W - 420, 48, heading)
    p.add("kpis", X0, 148, W, 84, kpi_cards([("Occupancy now", "Beds occupied"), ("Free now", "Beds ready"),
                                             ("Cleaning now", "Being cleaned"), ("ER waiting now", "Waiting for an ER doctor"),
                                             ("Longest wait now", "Longest wait"), ("Boarding now", "Admitted, waiting for a bed")]))
    p.add("board", X0, 248, 640, 458,
          matrix(column("Bed", "bay_label"), column("Bed", "slot"), "Bed cell", "Bed colour",
                 "Every bed, by bay (Ward I opened October 2024)", cell_px=30, hide_text=True, font=9, padding=2))
    p.add("legend", X0 + 330, 244, 310, 30, textbox([{"textRuns": [
        run("■ ", SANS, 9, OCCUPIED), run("Occupied   ", SANS, 8, MUTED), run("■ ", SANS, 9, CLEANING),
        run("Being cleaned   ", SANS, 8, MUTED), run("■ ", SANS, 9, "#cfc8b6"), run("Free", SANS, 8, MUTED)]}]))
    p.add("erQueue", 700, 248, 556, 220, bars(column("ER Visits", "triage_label"), "ER waiting now",
                                              "Waiting for an ER doctor, by triage level (ESI 1 most urgent)",
                                              fill=RUST, sort=(column("ER Visits", "triage_label"), "Ascending")))
    p.add("ratio", 700, 484, 556, 220, chart(
        "clusteredBarChart", column("Unit", "unit"), ["Patients per nurse now", "Nurse ratio target"],
        "Patients per nurse on duty now, against the unit's target",
        sort=(column("Unit", "unit"), "Ascending"),
        extra={"dataPoint": measure_colors({"Patients per nurse now": INK, "Nurse ratio target": "#cfc8b6"}),
               "labels": [{"properties": {"show": lit("true"), "color": color(MUTED), "fontSize": num(9)}}],
               "valueAxis": [{"properties": {"show": lit("false"), "showAxisTitle": lit("false"), "start": num(0)}}],
               "categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false")}}]}))
    pages.append(p)

    # ------------------------------------------------ Flow
    p = Page("flow", "Flow")
    header(p)
    p.add("period", X0, 80, 180, 56, slicer("Date", "fiscal_year", "Period", "Dropdown", sync="period", default_text=PERIOD))
    p.add("unit", X0 + 196, 80, 180, 56, slicer("Unit", "unit", "Unit (beds and staffing)", "Dropdown", sync="unit"))
    p.add("insight", X0 + 400, 84, W - 400, 48, insight("Insight: flow"))
    p.add("kpis", X0, 148, W, 84, kpi_cards([("Median wait", "Median wait for an ER doctor"),
                                             ("Left without being seen", "Left without being seen"),
                                             ("Bed occupancy", "Bed occupancy"), ("Patients per nurse", "Patients per nurse"),
                                             ("Average boarding", "Average wait for a bed")]))
    p.add("heatArrivals", X0, 248, 604, 230, matrix(column("Date", "weekday"), column("Hour", "hour"), "Heat cell",
                                                    "Arrivals colour", "ER arrivals per hour by weekday and hour (light 0, dark 13 or more)",
                                                    cell_px=22, font=9, hide_text=True, header_font=6))
    p.add("heatWait", X0 + 628, 248, 604, 230, matrix(column("Date", "weekday"), column("Hour", "hour"), "Wait cell",
                                                      "Wait colour", "Median minutes to see a doctor (light 0, dark 150 or more)",
                                                      cell_px=22, font=9, hide_text=True, header_font=6))
    p.add("occupancy", X0, 490, 604, 216, chart(
        "lineChart", column("Date", "month"), ["Bed occupancy"], "Bed occupancy by month",
        series=column("Unit", "unit"),
        extra={"dataPoint": series_colors("Unit", "unit", UNIT_COLORS),
               "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineColor": color(RULE), "start": num(0.5), "end": num(1)}}]}),
          filters=[categorical_filter("Unit", "unit", ["'ICU'", "'Surgery'", "'General Ward'"], "inpatient")])
    p.add("waitTrend", X0 + 628, 490, 604, 216, chart(
        "lineChart", column("Date", "month"), ["Median wait"], "Median wait by month and shift (extra evening doctor from Apr 2025)",
        series=column("Hour", "shift"),
        extra={"dataPoint": series_colors("Hour", "shift", SHIFT_COLORS),
               "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineColor": color(RULE), "start": num(0)}}]}))
    for v in ("heatArrivals", "heatWait", "waitTrend"):
        p.no_filter("unit", v)
    pages.append(p)

    # ------------------------------------------------ Readmissions
    p = Page("readmissions", "Readmissions")
    header(p)
    p.add("period", X0, 80, 180, 56, slicer("Date", "fiscal_year", "Period", "Dropdown", sync="period", default_text=PERIOD))
    p.add("unit", X0 + 196, 80, 180, 56, slicer("Unit", "unit", "Discharging unit", "Dropdown", sync="unit"))
    p.add("age", X0 + 392, 80, 140, 56, slicer("Discharges", "age_group", "Age group", "Dropdown"))
    p.add("insight", X0 + 556, 84, W - 556, 48, insight("Insight: readmissions"))
    p.add("kpis", X0, 148, W, 84, kpi_cards([("Readmission rate", "30-day readmission rate"),
                                             ("Discharges home", "Discharges home"), ("Readmissions", "Readmissions"),
                                             ("LACE catch rate", "Caught by the LACE flag"),
                                             ("Model vs LACE", "Model vs LACE, ROC AUC")]))
    rate_sort = (measure("Readmission rate"), "Descending")
    p.add("byDiagnosis", X0, 248, 400, 458, bars(column("Discharges", "diagnosis"), "Readmission rate",
                                                 "30-day readmission rate by diagnosis", sort=rate_sort))
    p.add("byAge", X0 + 416, 248, 300, 136, bars(column("Discharges", "age_group"), "Readmission rate", "By age group",
                                                 sort=(column("Discharges", "age_group"), "Ascending")))
    p.add("byStay", X0 + 416, 392, 300, 172, bars(column("Discharges", "los_band"), "Readmission rate", "By length of stay",
                                                  sort=(column("Discharges", "los_band"), "Ascending")))
    p.add("byPressure", X0 + 416, 572, 300, 134, bars(column("Discharges", "occupancy_band"), "Readmission rate",
                                                      "By how full the unit was at discharge", fill=RUST,
                                                      sort=(column("Discharges", "occupancy_band"), "Ascending")))
    p.add("followUp", X0 + 732, 248, 500, 458, table(
        [(column("Discharges", "discharge_date"), "Date"), (column("Discharges", "diagnosis"), "Diagnosis"),
         (column("Discharges", "age"), "Age"), (column("Discharges", "lace"), "LACE"),
         (column("Discharges", "risk_percentile"), "Risk"), (column("Discharges", "drivers"), "Why")],
        "Follow-up list: sent home in the last 30 days, highest risk first",
        sort=(column("Discharges", "risk_percentile"), "Descending"), widths=[54, 132, 34, 40, 50, 160]),
        filters=[categorical_filter("Discharges", "outcome_known", ["false"], "pending")])
    for src in ("period", "age"):
        p.no_filter(src, "followUp")
    pages.append(p)
    return pages


def theme():
    return {
        "name": "Editorial ledger",
        "dataColors": [INK, OCHRE, TEAL, RUST, PLUM, "#5e6676", "#9aa6c8", "#c9b99a"],
        "background": PAPER, "foreground": TEXT, "tableAccent": INK,
        "good": TEAL, "neutral": OCHRE, "bad": RUST,
        "textClasses": {
            "callout": {"fontFace": SERIF, "fontSize": 22, "color": TEXT},
            "title": {"fontFace": SANS, "fontSize": 11, "color": MUTED},
            "header": {"fontFace": SANS, "fontSize": 11, "color": TEXT},
            "label": {"fontFace": SANS, "fontSize": 10, "color": MUTED}},
        "visualStyles": {
            "page": {"*": {"background": [{"color": {"solid": {"color": PAPER}}, "transparency": 0}],
                           "outspace": [{"color": {"solid": {"color": PAPER}}}]}},
            "*": {"*": {"background": [{"show": False}], "border": [{"show": False}],
                        "dropShadow": [{"show": False}]}}},
    }


def build_report():
    d = RP / "definition"
    write_json(RP / ".platform", platform("Report"))
    write_json(RP / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    write_json(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write_json(RP / "StaticResources" / "RegisteredResources" / "theme.json", theme())
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.0.0/schema.json",
        "themeCollection": {
            "baseTheme": {"name": "CY24SU10", "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"},
                          "type": "SharedResources"},
            "customTheme": {"name": "theme.json", "reportVersionAtImport": {"visual": "1.8.100", "report": "2.0.100", "page": "1.3.100"},
                            "type": "RegisteredResources"}},
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "theme.json", "path": "theme.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "useEnhancedTooltips": True, "hideVisualContainerHeader": True}})
    pages = build_pages()
    write_json(d / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.key for p in pages], "activePageName": pages[0].key})
    for p in pages:
        page = {"$schema": f"{SCHEMA}/page/1.4.0/schema.json", "name": p.key, "displayName": p.display,
                "displayOption": "FitToPage", "height": 720, "width": 1280}
        if p.interactions:
            page["visualInteractions"] = p.interactions
        write_json(d / "pages" / p.key / "page.json", page)
        for v in p.visuals:
            folder = v.pop("_folder")
            write_json(d / "pages" / p.key / "visuals" / folder / "visual.json", v)


def main():
    global DATA_FOLDER, ASOF_MONTH, ASOF_DAY, ASOF_HOUR, PERIOD, OUT, SM, RP
    parser = argparse.ArgumentParser(description="Generate the Power BI project.")
    parser.add_argument("--portable", action="store_true",
                        help="use the neutral path C:\\hospital-operations-bi\\data\\model instead of this checkout's path")
    parser.add_argument("--asof", help='open the Now page at another moment, e.g. "2025-09-18 12"')
    parser.add_argument("--period", help='open the review pages filtered to one year, e.g. "FY 2025-26"')
    parser.add_argument("--out", help="write the project to another folder (screenshot copies)")
    args = parser.parse_args()
    if args.out:
        OUT = Path(args.out)
        SM, RP = OUT / f"{NAME}.SemanticModel", OUT / f"{NAME}.Report"
    if args.portable:
        DATA_FOLDER = PORTABLE_DATA_FOLDER
    if args.asof:
        t = datetime.strptime(args.asof, "%Y-%m-%d %H")
        ASOF_MONTH, ASOF_DAY, ASOF_HOUR = t.strftime("%Y-%m-01"), t.day, t.hour
    PERIOD = args.period
    for path in (SM, RP):
        if path.exists():
            shutil.rmtree(path)
    con = duckdb.connect(str(ROOT / "data" / "hc.duckdb"), read_only=True)
    build_model(con)
    build_report()
    write_json(OUT / f"{NAME}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})
    write(OUT / ".gitignore", "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n")
    print(f"wrote {OUT / (NAME + '.pbip')}")


if __name__ == "__main__":
    main()
