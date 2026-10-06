-- Each check returns the rows that break a rule. A clean build returns zero rows for every check.

-- name: resent ER rows are removed and nothing else is
SELECT r.n_raw, d.n_distinct_ids, s.n_staged
FROM (SELECT count(*) AS n_raw FROM raw_er_visits) r,
     (SELECT count(DISTINCT visit_id) AS n_distinct_ids FROM raw_er_visits) d,
     (SELECT count(*) AS n_staged FROM stg_er_visits) s
WHERE s.n_staged <> d.n_distinct_ids OR r.n_raw = s.n_staged;

-- name: ER timestamps run in order
SELECT visit_id FROM fact_er_visits
WHERE seen_ts < arrival_ts OR decision_ts < seen_ts OR departure_ts < coalesce(decision_ts, seen_ts, arrival_ts);

-- name: patients who left without being seen were never seen
SELECT visit_id FROM fact_er_visits WHERE left_unseen AND seen_ts IS NOT NULL;

-- name: every admitted ER visit has exactly one admission, starting when the patient left the ER
SELECT v.visit_id, count(a.admission_id) AS n
FROM fact_er_visits v
LEFT JOIN fact_admissions a ON a.er_visit_id = v.visit_id
WHERE v.admitted
GROUP BY v.visit_id
HAVING n <> 1 OR any_value(a.admit_ts) <> any_value(v.departure_ts);

-- name: no bed holds two patients at once
SELECT a.stay_id, b.stay_id
FROM fact_bed_stays a
JOIN fact_bed_stays b ON a.bed_id = b.bed_id AND a.stay_id < b.stay_id
 AND a.start_ts < coalesce(b.end_ts, data_end()) AND b.start_ts < coalesce(a.end_ts, data_end());

-- name: a bed is cleaned before the next patient gets it
SELECT stay_id FROM (
    SELECT stay_id, start_ts, lag(ready_ts) OVER (PARTITION BY bed_id ORDER BY start_ts) AS prev_ready,
           lag(end_ts) OVER (PARTITION BY bed_id ORDER BY start_ts) AS prev_end
    FROM fact_bed_stays
) WHERE prev_end IS NOT NULL AND (prev_ready IS NULL OR start_ts < prev_ready);

-- name: each admission's bed stays join end to end from admission to discharge
SELECT admission_id FROM (
    SELECT s.admission_id, a.admit_ts, a.discharge_ts,
           min(s.start_ts) AS first_start, max(coalesce(s.end_ts, data_end())) AS last_end,
           sum(date_diff('minute', s.start_ts, coalesce(s.end_ts, data_end()))) AS minutes
    FROM fact_bed_stays s JOIN fact_admissions a USING (admission_id)
    GROUP BY ALL
) WHERE first_start <> admit_ts OR last_end <> coalesce(discharge_ts, data_end())
     OR minutes <> date_diff('minute', admit_ts, coalesce(discharge_ts, data_end()));

-- name: beds are only used while in service
SELECT s.stay_id FROM fact_bed_stays s JOIN dim_bed b USING (bed_id) WHERE s.start_ts::DATE < b.in_service_from;

-- name: hourly census never exceeds beds in service
SELECT hour_ts, unit FROM fact_unit_hourly WHERE occupied + cleaning > beds_in_service;

-- name: hourly census matches a direct count of bed stays
SELECT h.hour_ts, h.unit FROM fact_unit_hourly h
WHERE h.unit <> 'ER' AND hour(h.hour_ts) IN (3, 11, 19) AND h.occupied <> (
    SELECT count(*) FROM fact_bed_stays s
    WHERE s.unit = h.unit AND s.start_ts <= h.hour_ts AND (s.end_ts IS NULL OR s.end_ts > h.hour_ts));

-- name: ER waiting count matches a direct count of visits
SELECT h.hour_ts FROM fact_unit_hourly h
WHERE h.unit = 'ER' AND hour(h.hour_ts) IN (2, 14, 21) AND h.waiting <> (
    SELECT count(*) FROM fact_er_visits v
    WHERE v.arrival_ts <= h.hour_ts AND (coalesce(v.seen_ts, v.departure_ts) IS NULL OR coalesce(v.seen_ts, v.departure_ts) > h.hour_ts));

-- name: the roster covers every hour of every unit
SELECT hour_ts, unit FROM fact_unit_hourly WHERE nurses IS NULL OR (unit = 'ER' AND doctors IS NULL);

-- name: index discharges exclude deaths, transfers and discharges against advice
SELECT f.admission_id FROM feat_index_discharges f JOIN fact_admissions a USING (admission_id)
WHERE a.discharge_status <> 'Home';

-- name: readmission flag matches a direct recount
SELECT f.admission_id FROM feat_index_discharges f
WHERE f.outcome_known AND f.readmitted_30d <> (
    SELECT (count(*) > 0)::INTEGER FROM fact_admissions r
    WHERE r.patient_id = f.patient_id AND r.admission_type = 'Emergency'
      AND r.admit_ts > f.discharge_ts AND r.admit_ts <= f.discharge_ts + INTERVAL 30 DAY);

-- name: discharges without 30 days of follow-up carry no outcome
SELECT admission_id FROM feat_index_discharges
WHERE (discharge_ts + INTERVAL 30 DAY > data_end()) <> (readmitted_30d IS NULL);

-- name: LACE parts stay in range and add up
SELECT admission_id FROM feat_index_discharges
WHERE lace_l NOT BETWEEN 0 AND 7 OR lace_a NOT IN (0, 3) OR lace_c NOT BETWEEN 0 AND 5 OR lace_e NOT BETWEEN 0 AND 4
   OR lace <> lace_l + lace_a + lace_c + lace_e;

-- name: the model never sees sex, payer or age group
SELECT feature FROM model_features WHERE feature IN ('sex', 'payer', 'age_group', 'patient_id');

-- name: every discharge home has a risk score
SELECT f.admission_id FROM feat_index_discharges f
LEFT JOIN readmission_scores s USING (admission_id)
WHERE s.risk_score IS NULL OR s.risk_score NOT BETWEEN 0 AND 1;
