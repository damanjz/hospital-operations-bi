-- Marts: the tables the report reads. Model outputs (readmission_scores, model_*) come from src/model.py.

CREATE OR REPLACE TABLE mart_discharges AS
SELECT
    f.admission_id,
    f.discharge_ts,
    f.discharge_date,
    f.discharge_unit,
    f.diagnosis,
    f.admission_type,
    f.age,
    f.age_group,
    f.los_days,
    f.los_band,
    CASE f.los_band WHEN 'Under 2 days' THEN 1 WHEN '2-3 days' THEN 2 WHEN '4-6 days' THEN 3 WHEN '7-13 days' THEN 4 ELSE 5 END
        AS los_band_order,
    f.charlson,
    f.er_visits_6m,
    f.occupancy_at_discharge,
    f.occupancy_band,
    CASE f.occupancy_band WHEN 'Under 85%' THEN 1 WHEN '85-94%' THEN 2 ELSE 3 END AS occupancy_band_order,
    f.weekend_discharge,
    f.outcome_known,
    f.readmitted_30d,
    f.lace,
    f.lace >= 10 AS lace_high,
    s.risk_score,
    s.risk_percentile,
    s.risk_band,
    s.drivers,
    p.sex,
    p.payer
FROM feat_index_discharges f
JOIN readmission_scores s USING (admission_id)
JOIN dim_patient p USING (patient_id);

CREATE OR REPLACE TABLE mart_er_visits AS
SELECT visit_id, arrival_ts, arrival_date, arrival_hour, triage_label, complaint, seen_ts, decision_ts, departure_ts,
       disposition, wait_min, er_stay_min, boarding_min, left_unseen, admitted
FROM fact_er_visits;
