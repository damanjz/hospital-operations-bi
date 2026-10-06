-- Staging: read the raw exports as text, fix types and formats, remove resent rows.

CREATE OR REPLACE TABLE raw_er_visits AS
SELECT * FROM read_csv('data/raw/er_visits.csv', all_varchar = true);

-- The ER system resends some rows; identical rows are one visit.
CREATE OR REPLACE TABLE stg_er_visits AS
SELECT DISTINCT
    visit_id,
    patient_id,
    strptime(arrival_time, '%d-%m-%Y %H:%M') AS arrival_ts,
    CAST(replace(triage, 'ESI-', '') AS INTEGER) AS triage,
    complaint,
    strptime(nullif(seen_time, ''), '%d-%m-%Y %H:%M') AS seen_ts,
    strptime(nullif(admit_decision_time, ''), '%d-%m-%Y %H:%M') AS decision_ts,
    strptime(nullif(departure_time, ''), '%d-%m-%Y %H:%M') AS departure_ts,
    nullif(disposition, '') AS disposition
FROM raw_er_visits;

CREATE OR REPLACE TABLE stg_patients AS
SELECT patient_id, sex, CAST(date_of_birth AS DATE) AS date_of_birth, payer, CAST(registered_on AS DATE) AS registered_on
FROM read_csv('data/raw/patients.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_admissions AS
SELECT
    admission_id,
    patient_id,
    nullif(er_visit_id, '') AS er_visit_id,
    admission_type,
    CAST(bed_requested_time AS TIMESTAMP) AS requested_ts,
    CAST(admit_time AS TIMESTAMP) AS admit_ts,
    CAST(nullif(discharge_time, '') AS TIMESTAMP) AS discharge_ts,
    primary_diagnosis AS diagnosis,
    nullif(discharge_status, '') AS discharge_status
FROM read_csv('data/raw/admissions.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_comorbidities AS
SELECT admission_id, condition FROM read_csv('data/raw/comorbidities.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_bed_stays AS
SELECT
    stay_id,
    admission_id,
    bed_id,
    CAST(start_time AS TIMESTAMP) AS start_ts,
    CAST(nullif(end_time, '') AS TIMESTAMP) AS end_ts,
    CAST(nullif(bed_ready_time, '') AS TIMESTAMP) AS ready_ts
FROM read_csv('data/raw/bed_stays.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_beds AS
SELECT bed_id, unit, bay, CAST(slot AS INTEGER) AS slot, CAST(in_service_from AS DATE) AS in_service_from
FROM read_csv('data/raw/beds.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_roster AS
SELECT CAST(roster_date AS DATE) AS roster_date, shift, unit, role, CAST(on_duty AS INTEGER) AS on_duty
FROM read_csv('data/raw/staff_roster.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_shifts AS
SELECT shift, CAST(left(starts, 2) AS INTEGER) AS start_hour
FROM read_csv('data/raw/shifts.csv', all_varchar = true);

CREATE OR REPLACE TABLE stg_units AS
SELECT unit, CAST(beds AS INTEGER) AS beds, CAST(patients_per_nurse_target AS DOUBLE) AS patients_per_nurse_target
FROM read_csv('data/raw/units.csv', all_varchar = true);
