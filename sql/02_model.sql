-- Model: dimensions, cleaned facts, and an hourly census of every unit.
-- The data covers 1 April 2023 00:00 to 31 March 2026 23:59. Anything still open at the end
-- (a patient in a bed, a patient in the ER) is treated as ongoing at the cut-off.

CREATE OR REPLACE MACRO data_end() AS TIMESTAMP '2026-04-01 00:00:00';

CREATE OR REPLACE TABLE dim_date AS
SELECT
    d::DATE AS date,
    date_trunc('month', d)::DATE AS month,
    strftime(d, '%b %Y') AS month_label,
    (year(d) - 2023) * 12 + month(d) AS month_index,
    'FY ' || (year(d) - CASE WHEN month(d) < 4 THEN 1 ELSE 0 END)::VARCHAR || '-'
        || right((year(d) + CASE WHEN month(d) < 4 THEN 0 ELSE 1 END)::VARCHAR, 2) AS fiscal_year,
    strftime(d, '%a') AS weekday,
    isodow(d) AS weekday_num
FROM range(DATE '2023-04-01', DATE '2026-04-01', INTERVAL 1 DAY) t(d);

CREATE OR REPLACE TABLE dim_hour AS
SELECT
    h AS hour,
    lpad(h::VARCHAR, 2, '0') || ':00' AS hour_label,
    CASE WHEN h BETWEEN 7 AND 14 THEN 'Morning' WHEN h BETWEEN 15 AND 22 THEN 'Evening' ELSE 'Night' END AS shift
FROM range(0, 24) t(h);

CREATE OR REPLACE TABLE dim_unit AS
SELECT
    u.unit,
    u.patients_per_nurse_target,
    count(b.bed_id) AS beds,
    CASE u.unit WHEN 'ER' THEN 1 WHEN 'ICU' THEN 2 WHEN 'Surgery' THEN 3 ELSE 4 END AS unit_order
FROM stg_units u
LEFT JOIN stg_beds b USING (unit)
GROUP BY ALL;

CREATE OR REPLACE TABLE dim_bed AS
SELECT
    bed_id,
    unit,
    bay,
    slot,
    CASE unit WHEN 'ICU' THEN 'ICU' WHEN 'Surgery' THEN 'Surgery' ELSE 'Ward' END || ' ' || bay AS bay_label,
    in_service_from
FROM stg_beds;

CREATE OR REPLACE TABLE dim_patient AS
SELECT patient_id, sex, date_of_birth, payer FROM stg_patients;

CREATE OR REPLACE TABLE fact_er_visits AS
SELECT
    visit_id,
    patient_id,
    arrival_ts,
    arrival_ts::DATE AS arrival_date,
    hour(arrival_ts) AS arrival_hour,
    triage,
    'ESI ' || triage AS triage_label,
    complaint,
    seen_ts,
    decision_ts,
    departure_ts,
    disposition,
    date_diff('minute', arrival_ts, seen_ts) AS wait_min,
    date_diff('minute', arrival_ts, departure_ts) AS er_stay_min,
    CASE WHEN disposition = 'Admitted' THEN date_diff('minute', decision_ts, departure_ts) END AS boarding_min,
    disposition = 'Left without being seen' AS left_unseen,
    disposition = 'Admitted' AS admitted
FROM stg_er_visits;

CREATE OR REPLACE TABLE fact_bed_stays AS
SELECT s.stay_id, s.admission_id, s.bed_id, b.unit, s.start_ts, s.end_ts, s.ready_ts
FROM stg_bed_stays s
JOIN stg_beds b USING (bed_id);

CREATE OR REPLACE TABLE fact_admissions AS
WITH charlson(condition, weight) AS (
    VALUES ('Myocardial infarction', 1), ('Congestive heart failure', 1), ('Chronic pulmonary disease', 1),
           ('Diabetes', 1), ('Diabetes with complications', 2), ('Renal disease', 2), ('Cerebrovascular disease', 1),
           ('Mild liver disease', 1), ('Cancer', 2), ('Dementia', 1)
),
score AS (
    SELECT admission_id, sum(weight) AS charlson, count(*) AS conditions
    FROM stg_comorbidities JOIN charlson USING (condition)
    GROUP BY admission_id
),
units AS (
    SELECT admission_id,
           arg_min(unit, start_ts) AS first_unit,
           arg_max(unit, start_ts) AS last_unit,
           bool_or(unit = 'ICU') AS had_icu
    FROM fact_bed_stays GROUP BY admission_id
)
SELECT
    a.admission_id,
    a.patient_id,
    a.er_visit_id,
    a.admission_type,
    a.requested_ts,
    a.admit_ts,
    a.discharge_ts,
    a.diagnosis,
    a.discharge_status,
    date_diff('minute', a.admit_ts, a.discharge_ts) / 1440.0 AS los_days,
    date_diff('year', p.date_of_birth, a.admit_ts::DATE)
        - CASE WHEN strftime(a.admit_ts, '%m%d') < strftime(p.date_of_birth, '%m%d') THEN 1 ELSE 0 END AS age,
    coalesce(s.charlson, 0) AS charlson,
    coalesce(s.conditions, 0) AS conditions,
    u.first_unit,
    u.last_unit AS discharge_unit,
    u.had_icu
FROM stg_admissions a
JOIN stg_patients p USING (patient_id)
LEFT JOIN score s USING (admission_id)
LEFT JOIN units u USING (admission_id);

CREATE OR REPLACE TABLE fact_roster AS
SELECT
    r.roster_date,
    r.shift,
    r.unit,
    r.role,
    r.on_duty,
    r.roster_date + to_hours(s.start_hour) AS shift_start_ts,
    r.roster_date + to_hours(s.start_hour + 8) AS shift_end_ts
FROM stg_roster r
JOIN stg_shifts s USING (shift);

-- Census on the hour, every hour, for every unit. Occupancy rate is the average of these snapshots.
CREATE OR REPLACE TABLE fact_unit_hourly AS
WITH hours AS (
    SELECT h AS hour_ts FROM range(TIMESTAMP '2023-04-01', data_end(), INTERVAL 1 HOUR) t(h)
),
grid AS (
    SELECT hour_ts, unit FROM hours CROSS JOIN (SELECT unit FROM dim_unit)
),
in_service AS (
    SELECT g.hour_ts, g.unit, count(b.bed_id) AS beds_in_service
    FROM grid g LEFT JOIN stg_beds b ON b.unit = g.unit AND b.in_service_from <= g.hour_ts::DATE
    GROUP BY ALL
),
occupied AS (
    SELECT h.hour_ts, s.unit, count(*) AS occupied
    FROM hours h JOIN fact_bed_stays s ON s.start_ts <= h.hour_ts AND coalesce(s.end_ts, data_end()) > h.hour_ts
    GROUP BY ALL
),
cleaning AS (
    SELECT h.hour_ts, s.unit, count(*) AS cleaning
    FROM hours h JOIN fact_bed_stays s ON s.end_ts <= h.hour_ts AND coalesce(s.ready_ts, data_end()) > h.hour_ts
    GROUP BY ALL
),
er_present AS (
    SELECT h.hour_ts, 'ER' AS unit, count(*) AS present
    FROM hours h JOIN fact_er_visits v ON v.arrival_ts <= h.hour_ts AND coalesce(v.departure_ts, data_end()) > h.hour_ts
    GROUP BY ALL
),
er_waiting AS (
    SELECT h.hour_ts, 'ER' AS unit, count(*) AS waiting
    FROM hours h JOIN fact_er_visits v
      ON v.arrival_ts <= h.hour_ts AND coalesce(v.seen_ts, v.departure_ts, data_end()) > h.hour_ts
    GROUP BY ALL
),
er_boarding AS (
    SELECT h.hour_ts, 'ER' AS unit, count(*) AS boarding
    FROM hours h JOIN fact_er_visits v
      ON v.decision_ts <= h.hour_ts AND coalesce(v.departure_ts, data_end()) > h.hour_ts
    GROUP BY ALL
),
staff AS (
    SELECT h.hour_ts, r.unit,
           sum(CASE WHEN r.role = 'Nurse' THEN r.on_duty END) AS nurses,
           sum(CASE WHEN r.role = 'Doctor' THEN r.on_duty END) AS doctors
    FROM hours h JOIN fact_roster r ON r.shift_start_ts <= h.hour_ts AND r.shift_end_ts > h.hour_ts
    GROUP BY ALL
)
SELECT
    g.hour_ts,
    g.hour_ts::DATE AS date,
    hour(g.hour_ts) AS hour,
    g.unit,
    i.beds_in_service,
    coalesce(o.occupied, 0) AS occupied,
    coalesce(c.cleaning, 0) AS cleaning,
    CASE WHEN g.unit = 'ER' THEN coalesce(p.present, 0) ELSE coalesce(o.occupied, 0) END AS patients,
    CASE WHEN g.unit = 'ER' THEN coalesce(w.waiting, 0) END AS waiting,
    CASE WHEN g.unit = 'ER' THEN coalesce(bd.boarding, 0) END AS boarding,
    st.nurses,
    st.doctors
FROM grid g
JOIN in_service i USING (hour_ts, unit)
LEFT JOIN occupied o USING (hour_ts, unit)
LEFT JOIN cleaning c USING (hour_ts, unit)
LEFT JOIN er_present p USING (hour_ts, unit)
LEFT JOIN er_waiting w USING (hour_ts, unit)
LEFT JOIN er_boarding bd USING (hour_ts, unit)
LEFT JOIN staff st USING (hour_ts, unit);
