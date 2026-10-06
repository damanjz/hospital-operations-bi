-- Features for the 30-day readmission model, and the LACE index it is compared against.
-- An index discharge is a stay that ended with the patient going home. Deaths, transfers out and
-- discharges against medical advice are excluded, as in standard readmission measures.
-- Readmitted = an unplanned (emergency) admission of the same patient within 30 days.
-- Discharges in the last 30 days of data have no outcome yet; they are scored but not labelled.

CREATE OR REPLACE TABLE model_features (feature VARCHAR, kind VARCHAR);
INSERT INTO model_features VALUES
    ('age', 'numeric'), ('los_days', 'numeric'), ('charlson', 'numeric'), ('er_visits_6m', 'numeric'),
    ('admissions_12m', 'numeric'), ('occupancy_at_discharge', 'numeric'), ('had_icu', 'numeric'),
    ('weekend_discharge', 'numeric'), ('emergency', 'numeric'), ('diagnosis', 'categorical');

CREATE OR REPLACE TABLE feat_index_discharges AS
WITH idx AS (
    SELECT a.*, coalesce(v.arrival_ts, a.requested_ts) AS anchor_ts
    FROM fact_admissions a
    LEFT JOIN fact_er_visits v ON v.visit_id = a.er_visit_id
    WHERE a.discharge_status = 'Home'
),
er_prior AS (
    SELECT i.admission_id, count(v.visit_id) AS er_visits_6m
    FROM idx i
    LEFT JOIN fact_er_visits v
      ON v.patient_id = i.patient_id AND v.arrival_ts >= i.anchor_ts - INTERVAL 182 DAY AND v.arrival_ts < i.anchor_ts
    GROUP BY i.admission_id
),
adm_prior AS (
    SELECT i.admission_id, count(p.admission_id) AS admissions_12m
    FROM idx i
    LEFT JOIN fact_admissions p
      ON p.patient_id = i.patient_id AND p.admit_ts >= i.admit_ts - INTERVAL 365 DAY AND p.admit_ts < i.admit_ts
    GROUP BY i.admission_id
),
readmit AS (
    SELECT i.admission_id, min(r.admit_ts) AS readmit_ts
    FROM idx i
    JOIN fact_admissions r
      ON r.patient_id = i.patient_id AND r.admission_type = 'Emergency'
     AND r.admit_ts > i.discharge_ts AND r.admit_ts <= i.discharge_ts + INTERVAL 30 DAY
    GROUP BY i.admission_id
),
pressure AS (
    SELECT i.admission_id, h.occupied::DOUBLE / h.beds_in_service AS occupancy_at_discharge
    FROM idx i
    JOIN fact_unit_hourly h ON h.unit = i.discharge_unit AND h.hour_ts = date_trunc('hour', i.discharge_ts)
)
SELECT
    i.admission_id,
    i.patient_id,
    i.discharge_ts,
    i.discharge_ts::DATE AS discharge_date,
    i.discharge_unit,
    i.diagnosis,
    i.admission_type,
    i.age,
    CASE WHEN i.age < 40 THEN '18-39' WHEN i.age < 60 THEN '40-59' WHEN i.age < 75 THEN '60-74' ELSE '75+' END AS age_group,
    i.los_days,
    CASE WHEN i.los_days < 2 THEN 'Under 2 days' WHEN i.los_days < 4 THEN '2-3 days' WHEN i.los_days < 7 THEN '4-6 days'
         WHEN i.los_days < 14 THEN '7-13 days' ELSE '14+ days' END AS los_band,
    i.charlson,
    e.er_visits_6m,
    a.admissions_12m,
    p.occupancy_at_discharge,
    CASE WHEN p.occupancy_at_discharge >= 0.95 THEN '95% or more' WHEN p.occupancy_at_discharge >= 0.85 THEN '85-94%'
         ELSE 'Under 85%' END AS occupancy_band,
    i.had_icu::INTEGER AS had_icu,
    (isodow(i.discharge_ts) >= 6)::INTEGER AS weekend_discharge,
    (i.admission_type = 'Emergency')::INTEGER AS emergency,
    i.discharge_ts + INTERVAL 30 DAY <= data_end() AS outcome_known,
    CASE WHEN i.discharge_ts + INTERVAL 30 DAY <= data_end() THEN (r.readmit_ts IS NOT NULL)::INTEGER END AS readmitted_30d,
    r.readmit_ts,
    -- LACE: Length of stay, Acuity, Comorbidity (Charlson), Emergency visits in the prior 6 months
    CASE WHEN i.los_days < 1 THEN 0 WHEN i.los_days < 2 THEN 1 WHEN i.los_days < 3 THEN 2 WHEN i.los_days < 4 THEN 3
         WHEN i.los_days < 7 THEN 4 WHEN i.los_days < 14 THEN 5 ELSE 7 END AS lace_l,
    CASE WHEN i.admission_type = 'Emergency' THEN 3 ELSE 0 END AS lace_a,
    CASE WHEN i.charlson >= 4 THEN 5 ELSE i.charlson END AS lace_c,
    least(e.er_visits_6m, 4) AS lace_e
FROM idx i
JOIN er_prior e USING (admission_id)
JOIN adm_prior a USING (admission_id)
LEFT JOIN pressure p USING (admission_id)
LEFT JOIN readmit r USING (admission_id);

ALTER TABLE feat_index_discharges ADD COLUMN lace INTEGER;
UPDATE feat_index_discharges SET lace = lace_l + lace_a + lace_c + lace_e;
