"""Generate three years of synthetic hospital exports for a 288-bed multi-specialty hospital in Hyderabad.

One event-driven simulation, April 2023 to March 2026:
- ER: arrivals by hour, weekday, season and trend; triage; a doctor queue staffed by a fixed roster;
  patients with low acuity leave without being seen when the wait runs past their patience.
- Beds: admitted patients board in the ER until a bed is free; beds need cleaning after every stay;
  ICU patients step down to a ward bed and stay in the ICU while none is free; planned surgery
  competes for surgical beds; when a unit is full in the daytime, the patient closest to discharge
  is sent home early.
- Readmissions: every discharge home carries a planted 30-day readmission risk (diagnosis, age,
  comorbidity, prior ER use, short or long stays, weekend discharge, early discharge under bed
  pressure, plus a hidden per-patient factor). Readmissions come back through the ER.

Writes nine raw exports to data/raw, shaped like hospital information system extracts.
"""
import heapq
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 7
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
START = datetime(2023, 4, 1)
END = datetime(2026, 4, 1)
MINUTES = int((END - START).total_seconds() // 60)
DAYS = MINUTES // 1440
DAY = 1440
WARD_EXPANSION = datetime(2024, 10, 1)      # General Ward opens bay I (16 beds)
ROSTER_CHANGE = datetime(2025, 4, 1)        # ER adds an evening doctor and two evening nurses
OVERFLOW_AFTER = 240                        # minutes a ward patient waits before taking a surgical bed
SURGERY_RESERVE = 6

rng = np.random.default_rng(SEED)

# ---------------------------------------------------------------- beds and staff
UNITS = {"ICU": ("ICU", "AB"), "Surgery": ("SUR", "ABCDE"), "General Ward": ("GW", "ABCDEFGHI")}
SLOTS = 16
SHIFTS = [("Morning", 7), ("Evening", 15), ("Night", 23)]
ER_DOCTORS = {"Morning": 4, "Evening": 3, "Night": 3}
NURSES = {"ER": {"Morning": 11, "Evening": 11, "Night": 8},
          "ICU": {"Morning": 15, "Evening": 14, "Night": 13},
          "Surgery": {"Morning": 15, "Evening": 13, "Night": 11},
          "General Ward": {"Morning": 22, "Evening": 20, "Night": 16}}

# ---------------------------------------------------------------- demand
ER_PER_DAY = 132
HOUR_SHAPE = np.array([2.4, 1.9, 1.5, 1.3, 1.2, 1.4, 2.0, 3.0, 4.2, 5.4, 6.1, 6.2, 5.8, 5.3, 5.0,
                       4.9, 5.1, 5.6, 6.3, 6.8, 6.5, 5.4, 4.1, 3.0])
HOUR_SHAPE = HOUR_SHAPE / HOUR_SHAPE.sum()
WEEKDAY = [1.13, 1.04, 1.0, 0.98, 1.0, 0.95, 0.90]             # Monday first
SEASON = {1: 0.95, 2: 0.93, 3: 0.95, 4: 1.0, 5: 1.02, 6: 1.04, 7: 1.12, 8: 1.24, 9: 1.26, 10: 1.14, 11: 1.0, 12: 0.97}
TRIAGE_P = [0.02, 0.12, 0.38, 0.33, 0.15]
ADMIT_P = [0.85, 0.50, 0.19, 0.035, 0.01]
DOCTOR_MIN = [65, 45, 31, 19, 13]
# queue order: arrival time shifted by acuity, so low-acuity patients are not starved
QUEUE_OFFSET = [-100000, -90, 0, 40, 60]
COMPLAINTS = ["Fever", "Chest pain", "Breathlessness", "Abdominal pain", "Injury", "Weakness or dizziness",
              "Vomiting or diarrhoea", "Headache", "Urinary symptoms", "Skin or minor wound"]
COMPLAINT_P = [0.17, 0.08, 0.09, 0.13, 0.15, 0.08, 0.11, 0.06, 0.06, 0.07]

# inpatient diagnosis: (complaint, mean length of stay in days, share of ICU starts, base readmission log-odds)
DIAGNOSES = {
    "Heart failure": ("Breathlessness", 5.5, 0.15, 1.05),
    "Acute MI": ("Chest pain", 5.0, 0.65, 0.45),
    "Pneumonia": ("Breathlessness", 5.0, 0.12, 0.35),
    "COPD exacerbation": ("Breathlessness", 4.5, 0.10, 0.85),
    "Dengue": ("Fever", 3.5, 0.05, -0.65),
    "Sepsis": ("Fever", 7.5, 0.55, 0.45),
    "Stroke": ("Weakness or dizziness", 7.0, 0.40, 0.20),
    "Diabetes complications": ("Weakness or dizziness", 4.0, 0.08, 0.55),
    "Kidney disease": ("Urinary symptoms", 5.0, 0.10, 0.70),
    "Gastrointestinal": ("Abdominal pain", 3.5, 0.05, 0.20),
    "Emergency surgery": ("Abdominal pain", 5.0, 0.15, 0.0),
    "Trauma": ("Injury", 6.0, 0.30, -0.30),
    "Planned orthopaedic surgery": (None, 3.5, 0.03, -1.0),
    "Planned general surgery": (None, 2.5, 0.02, -1.2),
}
SURGICAL = {"Emergency surgery", "Trauma", "Planned orthopaedic surgery", "Planned general surgery"}
# Charlson conditions with weights; prevalence rises with age
CHARLSON = {"Myocardial infarction": 1, "Congestive heart failure": 1, "Chronic pulmonary disease": 1,
            "Diabetes": 1, "Diabetes with complications": 2, "Renal disease": 2, "Cerebrovascular disease": 1,
            "Mild liver disease": 1, "Cancer": 2, "Dementia": 1}
CHARLSON_BASE = {"Myocardial infarction": 0.04, "Congestive heart failure": 0.05, "Chronic pulmonary disease": 0.06,
                 "Diabetes": 0.14, "Diabetes with complications": 0.04, "Renal disease": 0.04,
                 "Cerebrovascular disease": 0.03, "Mild liver disease": 0.03, "Cancer": 0.025, "Dementia": 0.015}
DIAG_CONDITION = {"Heart failure": "Congestive heart failure", "Acute MI": "Myocardial infarction",
                  "COPD exacerbation": "Chronic pulmonary disease", "Diabetes complications": "Diabetes with complications",
                  "Kidney disease": "Renal disease", "Stroke": "Cerebrovascular disease"}
PAYERS = ["Cash", "Private insurance", "Aarogyasri", "CGHS"]
PAYER_P = [0.38, 0.30, 0.25, 0.07]


def minute_dt(m):
    return START + timedelta(minutes=int(m))


def shift_of(minute_of_day):
    h = minute_of_day // 60
    return "Morning" if 7 <= h < 15 else "Evening" if 15 <= h < 23 else "Night"


def after_change(m):
    return minute_dt(m) >= ROSTER_CHANGE


# ---------------------------------------------------------------- patients
class Patients:
    def __init__(self):
        self.rows = []
        self.er_visits = {}       # patient -> list of arrival minutes
        self.hidden = []

    def new(self, minute, min_age=18):
        pid = len(self.rows)
        age = int(np.clip(rng.gamma(6.5, 7.6), min_age, 95))
        birth = minute_dt(minute) - timedelta(days=int(age * 365.25 + rng.integers(0, 365)))
        sex = "Female" if rng.random() < 0.47 else "Male"
        conds = {c for c, p in CHARLSON_BASE.items() if rng.random() < min(0.9, p * math.exp(0.035 * (age - 50)))}
        if "Diabetes with complications" in conds:
            conds.discard("Diabetes")
        self.rows.append(dict(pid=pid, sex=sex, birth=birth, payer=rng.choice(PAYERS, p=PAYER_P),
                              registered=minute, conds=conds))
        self.hidden.append(rng.normal(0, 0.9))
        return pid

    def age(self, pid, minute):
        return (minute_dt(minute) - self.rows[pid]["birth"]).days / 365.25


# ---------------------------------------------------------------- simulation
class Hospital:
    def __init__(self):
        self.events = []
        self.seq = 0
        self.patients = Patients()
        self.visits = []          # ER visit dicts
        self.admissions = []
        self.stays = []
        self.comorb = []
        self.beds = self.make_beds()
        self.free = {u: set() for u in UNITS}
        self.cleaning = {u: 0 for u in UNITS}
        self.occupant = {}        # bed -> admission id
        for b in self.beds:
            if b["from"] == 0:
                self.free[b["unit"]].add(b["bed_id"])
        self.waiting = []         # ER queue: (triage, arrival, seq, visit)
        self.doctors_busy = 0
        self.bed_queue = {u: [] for u in UNITS}    # (priority, request minute, seq, request)
        self.planned = {u: {} for u in UNITS}     # admission -> planned discharge minute (current unit only)
        self.pending_readmit = {}

    def make_beds(self):
        beds = []
        expand = int((WARD_EXPANSION - START).total_seconds() // 60)
        for unit, (code, bays) in UNITS.items():
            for bay in bays:
                for slot in range(1, SLOTS + 1):
                    opened = expand if (unit == "General Ward" and bay == "I") else 0
                    beds.append(dict(bed_id=f"{code}-{bay}{slot:02d}", unit=unit, bay=bay, slot=slot, **{"from": opened}))
        return beds

    def push(self, minute, kind, payload=None):
        self.seq += 1
        heapq.heappush(self.events, (minute, self.seq, kind, payload))

    def doctors_on(self, minute):
        s = shift_of(minute % DAY)
        return ER_DOCTORS[s] + (1 if s == "Evening" and after_change(minute) else 0)

    # ---- ER
    def schedule_arrivals(self):
        for d in range(DAYS):
            day = START + timedelta(days=d)
            years = d / 365.25
            rate = ER_PER_DAY * WEEKDAY[day.weekday()] * SEASON[day.month] * (1.04 ** years)
            for h in range(24):
                for _ in range(rng.poisson(rate * HOUR_SHAPE[h])):
                    self.push(d * DAY + h * 60 + int(rng.integers(0, 60)), "arrive", None)
        for d in range(DAYS):
            day = START + timedelta(days=d)
            if day.weekday() == 6:
                continue
            n = rng.poisson(8 if day.weekday() == 5 else 16)
            for _ in range(n):
                self.push(d * DAY + 7 * 60 + int(rng.integers(0, 150)), "planned", None)
        for d in range(DAYS + 1):
            for _, h in SHIFTS:
                self.push(d * DAY + h * 60, "shift", None)
        for hr in range(DAYS * 24):
            self.push(hr * 60 + 30, "tick", None)

    def arrive(self, minute, readmit=None):
        p = self.patients
        if readmit is not None:
            pid, diag = readmit
            triage = int(rng.choice([2, 3], p=[0.45, 0.55]))
            admit = True
        else:
            existing = len(p.rows) > 2000 and rng.random() < 0.22
            pid = int(rng.integers(0, len(p.rows))) if existing else p.new(minute)
            age = p.age(pid, minute)
            tp = np.array(TRIAGE_P) * np.array([1 + 0.012 * max(age - 50, 0)] * 2 + [1, 1, 1])
            triage = int(rng.choice(5, p=tp / tp.sum())) + 1
            admit = rng.random() < min(0.97, ADMIT_P[triage - 1] * (1 + 0.012 * max(age - 45, 0)))
            diag = self.pick_diagnosis(minute, age) if admit else None
        complaint = DIAGNOSES[diag][0] if diag else self.pick_complaint(minute)
        v = dict(visit_id=len(self.visits), pid=pid, arrival=minute, triage=triage, complaint=complaint, admit=admit,
                 diag=diag, seen=None, decision=None, departure=None, disposition=None,
                 patience=rng.gamma(5, 50) if triage >= 4 else None)
        self.visits.append(v)
        p.er_visits.setdefault(pid, []).append(minute)
        heapq.heappush(self.waiting, (minute + QUEUE_OFFSET[triage - 1], v["visit_id"], v))
        self.start_doctors(minute)

    def pick_complaint(self, minute):
        pr = np.array(COMPLAINT_P)
        if minute_dt(minute).month in (7, 8, 9, 10):
            pr[0] *= 1.8
        return COMPLAINTS[rng.choice(len(COMPLAINTS), p=pr / pr.sum())]

    def pick_diagnosis(self, minute, age):
        names = [d for d in DIAGNOSES if not d.startswith("Planned")]
        w = np.array([1.0, 0.7, 1.0, 0.7, 0.9, 0.6, 0.6, 0.8, 0.6, 1.1, 0.9, 0.9])
        old = {"Heart failure", "Acute MI", "COPD exacerbation", "Stroke", "Kidney disease"}
        w = w * np.array([math.exp(0.03 * (age - 50)) if n in old else 1.0 for n in names])
        if minute_dt(minute).month in (7, 8, 9, 10):
            w[names.index("Dengue")] *= 3.2
        else:
            w[names.index("Dengue")] *= 0.25
        return names[rng.choice(len(names), p=w / w.sum())]

    def start_doctors(self, minute):
        while self.waiting and self.doctors_busy < self.doctors_on(minute):
            _, _, v = heapq.heappop(self.waiting)
            triage, arrival = v["triage"], v["arrival"]
            if v["patience"] is not None and minute - arrival > v["patience"]:
                v["departure"] = arrival + int(v["patience"])
                v["disposition"] = "Left without being seen"
                continue
            v["seen"] = minute
            self.doctors_busy += 1
            busy = max(4, rng.gamma(4, DOCTOR_MIN[triage - 1] / 4))
            self.push(minute + int(busy), "doctor_free", v)

    def doctor_free(self, minute, v):
        self.doctors_busy -= 1
        if v["admit"]:
            v["decision"] = minute + int(rng.gamma(3, 30)) + 15
            self.push(v["decision"], "admit_request", v)
        else:
            v["departure"] = minute + int(rng.gamma(2.5, 40)) + 10
            v["disposition"] = "Referred out" if rng.random() < 0.02 else "Discharged"
        self.start_doctors(minute)

    def drain_lwbs(self, minute):
        """Patients whose patience ran out while the doctors were busy leave on their own clock."""
        keep = []
        for item in self.waiting:
            v = item[2]
            if v["patience"] is not None and minute - v["arrival"] > v["patience"]:
                v["departure"] = v["arrival"] + int(v["patience"])
                v["disposition"] = "Left without being seen"
            else:
                keep.append(item)
        if len(keep) != len(self.waiting):
            heapq.heapify(keep)
            self.waiting = keep

    # ---- admissions and beds
    def new_admission(self, minute, pid, diag, kind, visit=None):
        a = dict(admission_id=len(self.admissions), pid=pid, diag=diag, kind=kind, visit=visit,
                 requested=minute, admit=None, discharge=None, status=None, icu_days=0.0, hurried=False)
        age = self.patients.age(pid, minute)
        mean = DIAGNOSES[diag][1] * 0.93 * (1 + 0.01 * (age - 50))
        a["los"] = max(0.4, rng.lognormal(math.log(mean) - 0.18, 0.6))
        icu_p = DIAGNOSES[diag][2] + (0.5 if visit and visit["triage"] == 1 else 0)
        if rng.random() < icu_p:
            a["icu_days"] = min(a["los"] * 0.8, max(0.8, rng.gamma(2.6, 1.8)))
        conds = set(self.patients.rows[pid]["conds"])
        if diag in DIAG_CONDITION:
            conds.add(DIAG_CONDITION[diag])
            self.patients.rows[pid]["conds"].add(DIAG_CONDITION[diag])
        if "Diabetes with complications" in conds:
            conds.discard("Diabetes")
        for c in sorted(conds):
            self.comorb.append((a["admission_id"], c))
        a["charlson"] = sum(CHARLSON[c] for c in conds)
        self.admissions.append(a)
        if a["icu_days"] > 0:
            first = "ICU"
        else:
            first = "Surgery" if diag in SURGICAL else "General Ward"
        a["final_unit"] = "Surgery" if diag in SURGICAL else "General Ward"
        prio = 0 if first == "ICU" else 1
        self.request_bed(minute, first, dict(a=a, unit=first, hold=None), prio)

    def request_bed(self, minute, unit, req, prio):
        heapq.heappush(self.bed_queue[unit], (prio, minute, self.seq, req))
        self.seq += 1
        self.assign_beds(minute, unit)

    def assign_beds(self, minute, unit):
        q = self.bed_queue[unit]
        while q and self.free[unit]:
            self.place(minute, unit, heapq.heappop(q)[3])
        if q and 9 * 60 <= minute % DAY < 20 * 60:
            self.pull_discharge(minute, unit)

    def overflow(self, minute):
        """Ward patients waiting over 4 hours go to a spare surgical bed; 6 stay free for surgery."""
        q = self.bed_queue["General Ward"]
        while q and q[0][1] <= minute - OVERFLOW_AFTER and len(self.free["Surgery"]) > SURGERY_RESERVE:
            self.place(minute, "Surgery", heapq.heappop(q)[3])

    def place(self, minute, unit, req):
        bed = sorted(self.free[unit])[int(rng.integers(0, len(self.free[unit])))]
        self.free[unit].discard(bed)
        a = req["a"]
        if req["hold"]:
            self.leave_bed(minute, a)          # step-down: the ICU bed frees only now
        else:
            a["admit"] = minute
            if a["visit"]:
                a["visit"]["departure"] = minute
                a["visit"]["disposition"] = "Admitted"
        self.occupant[bed] = a["admission_id"]
        self.stays.append(dict(admission_id=a["admission_id"], bed_id=bed, unit=unit, start=minute, end=None, ready=None))
        a["bed"] = bed
        a["stay"] = len(self.stays) - 1
        if unit == "ICU":
            self.push(minute + int(a["icu_days"] * DAY), "stepdown", a)
        else:
            self.plan_discharge(minute, unit, a)

    def plan_discharge(self, minute, unit, a):
        target = a["admit"] + a["los"] * DAY
        target = max(target, minute + 0.25 * DAY)
        day = int(target // DAY)
        hour = float(np.clip(rng.normal(13.5, 2.2), 10, 19.5))
        t = day * DAY + int(hour * 60)
        if t < minute + 240:
            t += DAY
        if minute_dt(t).weekday() == 6 and rng.random() < 0.55:
            t += DAY
        self.planned[unit][a["admission_id"]] = t
        self.push(t, "discharge", (a, t))

    def pull_discharge(self, minute, unit):
        """Bed pressure: send home the patient nearest to planned discharge if within 30 hours."""
        if not self.planned[unit] or rng.random() > 0.5:
            return
        aid, t = min(self.planned[unit].items(), key=lambda kv: kv[1])
        if t - minute > 30 * 60:
            return
        a = self.admissions[aid]
        a["hurried"] = True
        self.planned[unit][aid] = minute
        self.push(minute, "discharge", (a, minute))

    def stays_unit(self, bed):
        return {"ICU": "ICU", "SUR": "Surgery", "GW": "General Ward"}[bed.split("-")[0]]

    def leave_bed(self, minute, a):
        st = self.stays[a["stay"]]
        st["end"] = minute
        bed = st["bed_id"]
        self.occupant.pop(bed, None)
        unit = st["unit"]
        ready = minute + int(np.clip(rng.gamma(3, 22), 25, 200))
        st["ready"] = ready
        self.cleaning[unit] += 1
        self.push(ready, "bed_ready", bed)

    def stepdown(self, minute, a):
        self.request_bed(minute, a["final_unit"], dict(a=a, unit=a["final_unit"], hold=a["bed"]), 0)

    def discharge(self, minute, a, planned_t):
        unit = self.stays[a["stay"]]["unit"]
        if a["discharge"] is not None or self.planned[unit].get(a["admission_id"]) != planned_t:
            return
        self.planned[unit].pop(a["admission_id"])
        a["discharge"] = minute
        age = self.patients.age(a["pid"], minute)
        died_p = 0.012 + 0.0009 * max(age - 50, 0) + (0.05 if a["icu_days"] > 0 else 0)
        r = rng.random()
        a["status"] = "Died" if r < died_p else "Left against medical advice" if r < died_p + 0.02 else \
            "Transferred out" if r < died_p + 0.03 else "Home"
        self.leave_bed(minute, a)
        if a["status"] == "Home":
            self.maybe_readmit(minute, a, age)

    def maybe_readmit(self, minute, a, age):
        p = self.patients
        los = (a["discharge"] - a["admit"]) / DAY
        anchor = a["visit"]["arrival"] if a["visit"] else a["requested"]
        prior_er = sum(1 for t in p.er_visits.get(a["pid"], []) if anchor - 182 * DAY <= t < anchor)
        dt = minute_dt(minute)
        logit = (-3.25 + DIAGNOSES[a["diag"]][3] + 0.016 * (age - 50) + 0.2 * min(a["charlson"], 6)
                 + 0.22 * min(prior_er, 4) + (0.55 if los < 1.5 and a["kind"] == "Emergency" else 0)
                 + (0.35 if los > 10 else 0) + (0.3 if dt.weekday() >= 5 else 0)
                 + (0.8 if a["hurried"] else 0) + p.hidden[a["pid"]])
        if rng.random() < 1 / (1 + math.exp(-logit)):
            days = rng.gamma(1.3, 9.0) + 0.5
            diag = a["diag"] if rng.random() < 0.55 else self.pick_diagnosis(minute, age)
            if diag.startswith("Planned"):
                diag = "Emergency surgery"
            t = minute + int(days * DAY)
            if t < MINUTES:
                self.push(t, "readmit", (a["pid"], diag))

    # ---- main loop
    def run(self):
        self.schedule_arrivals()
        expand = int((WARD_EXPANSION - START).total_seconds() // 60)
        self.push(expand, "expand", None)
        while self.events:
            minute, _, kind, payload = heapq.heappop(self.events)
            if minute >= MINUTES:
                break
            if kind == "arrive":
                if minute < MINUTES:
                    self.arrive(minute)
            elif kind == "readmit":
                self.arrive(minute, readmit=payload)
            elif kind == "planned":
                if minute < MINUTES:
                    pid = self.patients.new(minute) if rng.random() < 0.8 or len(self.patients.rows) < 2000 \
                        else int(rng.integers(0, len(self.patients.rows)))
                    diag = "Planned orthopaedic surgery" if rng.random() < 0.45 else "Planned general surgery"
                    self.new_admission(minute, pid, diag, "Planned")
            elif kind == "tick":
                self.overflow(minute)
            elif kind == "shift":
                self.drain_lwbs(minute)
                self.start_doctors(minute)
            elif kind == "doctor_free":
                self.doctor_free(minute, payload)
            elif kind == "admit_request":
                v = payload
                self.new_admission(minute, v["pid"], v["diag"], "Emergency", visit=v)
            elif kind == "stepdown":
                self.stepdown(minute, payload)
            elif kind == "discharge":
                self.discharge(minute, *payload)
            elif kind == "bed_ready":
                unit = self.stays_unit(payload)
                self.cleaning[unit] -= 1
                self.free[unit].add(payload)
                self.assign_beds(minute, unit)
            elif kind == "expand":
                for b in self.beds:
                    if b["from"] == expand:
                        self.free["General Ward"].add(b["bed_id"])
                self.assign_beds(minute, "General Ward")


# ---------------------------------------------------------------- export
def ts(m, fmt="%Y-%m-%d %H:%M"):
    return "" if m is None else minute_dt(m).strftime(fmt)


def export(h):
    RAW.mkdir(parents=True, exist_ok=True)
    cutoff = MINUTES
    p = h.patients
    pd.DataFrame([dict(patient_id=f"P{r['pid']:06d}", sex=r["sex"], date_of_birth=r["birth"].strftime("%Y-%m-%d"),
                       payer=r["payer"], registered_on=ts(r["registered"], "%Y-%m-%d"))
                  for r in p.rows if r["registered"] < cutoff]).to_csv(RAW / "patients.csv", index=False)

    # The ER system exports Indian day-first timestamps and "ESI-n" triage codes, and resends some rows.
    visits = [v for v in h.visits if v["arrival"] < cutoff]
    rows = []
    for v in visits:
        dep = v["departure"] if v["departure"] is not None and v["departure"] < cutoff else None
        open_ = dep is None
        rows.append(dict(visit_id=f"ER{v['visit_id']:07d}", patient_id=f"P{v['pid']:06d}",
                         arrival_time=ts(v["arrival"], "%d-%m-%Y %H:%M"), triage=f"ESI-{v['triage']}",
                         complaint=v["complaint"],
                         seen_time=ts(v["seen"] if v["seen"] is not None and v["seen"] < cutoff else None, "%d-%m-%Y %H:%M"),
                         admit_decision_time=ts(v["decision"] if v["decision"] is not None and v["decision"] < cutoff else None,
                                                "%d-%m-%Y %H:%M"),
                         departure_time=ts(dep, "%d-%m-%Y %H:%M"),
                         disposition="" if open_ else v["disposition"]))
    er = pd.DataFrame(rows)
    dup = er.sample(frac=0.004, random_state=SEED)
    pd.concat([er, dup]).sort_values("visit_id", kind="stable").to_csv(RAW / "er_visits.csv", index=False)

    adm = [a for a in h.admissions if a["admit"] is not None and a["admit"] < cutoff]
    pd.DataFrame([dict(admission_id=f"A{a['admission_id']:06d}", patient_id=f"P{a['pid']:06d}",
                       er_visit_id=f"ER{a['visit']['visit_id']:07d}" if a["visit"] else "",
                       admission_type=a["kind"], bed_requested_time=ts(a["requested"]), admit_time=ts(a["admit"]),
                       discharge_time=ts(a["discharge"] if a["discharge"] is not None and a["discharge"] < cutoff else None),
                       primary_diagnosis=a["diag"],
                       discharge_status=a["status"] if a["discharge"] is not None and a["discharge"] < cutoff else "")
                  for a in adm]).to_csv(RAW / "admissions.csv", index=False)
    kept = {a["admission_id"] for a in adm}
    pd.DataFrame([dict(admission_id=f"A{aid:06d}", condition=c) for aid, c in h.comorb if aid in kept]) \
        .to_csv(RAW / "comorbidities.csv", index=False)

    st = [s for s in h.stays if s["start"] < cutoff]
    pd.DataFrame([dict(stay_id=f"S{i:07d}", admission_id=f"A{s['admission_id']:06d}", bed_id=s["bed_id"],
                       start_time=ts(s["start"]),
                       end_time=ts(s["end"] if s["end"] is not None and s["end"] < cutoff else None),
                       bed_ready_time=ts(s["ready"] if s["ready"] is not None and s["end"] is not None and s["end"] < cutoff
                                         and s["ready"] < cutoff else None))
                  for i, s in enumerate(st)]).to_csv(RAW / "bed_stays.csv", index=False)

    pd.DataFrame([dict(bed_id=b["bed_id"], unit=b["unit"], bay=b["bay"], slot=b["slot"],
                       in_service_from=ts(b["from"], "%Y-%m-%d"))
                  for b in h.beds]).to_csv(RAW / "beds.csv", index=False)

    roster = []
    for d in range(-1, DAYS):                                   # the night shift before day one covers 00:00-07:00
        day = START + timedelta(days=d)
        for shift, hour in SHIFTS:
            if d < 0 and shift != "Night":
                continue
            late = day >= ROSTER_CHANGE
            for unit, plan in NURSES.items():
                n = plan[shift] + (2 if unit == "ER" and shift == "Evening" and late else 0)
                n -= int(rng.random() < 0.12)                    # sick leave not covered
                roster.append(dict(roster_date=day.strftime("%Y-%m-%d"), shift=shift, unit=unit, role="Nurse", on_duty=n))
            roster.append(dict(roster_date=day.strftime("%Y-%m-%d"), shift=shift, unit="ER", role="Doctor",
                               on_duty=ER_DOCTORS[shift] + (1 if shift == "Evening" and late else 0)))
    pd.DataFrame(roster).to_csv(RAW / "staff_roster.csv", index=False)

    pd.DataFrame([dict(shift=s, starts=f"{h:02d}:00", ends=f"{(h + 8) % 24:02d}:00") for s, h in SHIFTS]) \
        .to_csv(RAW / "shifts.csv", index=False)
    pd.DataFrame([dict(unit="ER", beds=0, patients_per_nurse_target=4.0),
                  dict(unit="ICU", beds=32, patients_per_nurse_target=2.0),
                  dict(unit="Surgery", beds=80, patients_per_nurse_target=5.0),
                  dict(unit="General Ward", beds=144, patients_per_nurse_target=6.0)]) \
        .to_csv(RAW / "units.csv", index=False)


def main():
    h = Hospital()
    h.run()
    hurried = sum(a["hurried"] for a in h.admissions)
    print(f"patients {len(h.patients.rows):,}  ER visits {len(h.visits):,}  admissions {len(h.admissions):,}  "
          f"bed stays {len(h.stays):,}  early discharges {hurried:,}")
    export(h)
    print(f"wrote raw exports to {RAW}")


if __name__ == "__main__":
    main()
