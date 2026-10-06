"""30-day readmission risk model, compared against the LACE index.

Reads feat_index_discharges from DuckDB and writes back:
model_metrics, model_calibration, model_fairness, model_importance, readmission_scores.

Validation is out-of-time: train on discharges up to 30 Sep 2024, test on discharges from
1 Apr 2025 to the last discharge with a known 30-day outcome. The six months between are left out,
so no test patient's history overlaps a training label. The chosen model is refit on every labelled
discharge and scores all discharges home, including the last 30 days, which have no outcome yet:
that is the follow-up call list.
"""
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

SEED = 7
TRAIN_END = "2024-09-30"
TEST_START = "2025-04-01"
PROTECTED = {"sex", "payer", "age_group"}


def make_models(numeric, categorical):
    def prep(scale):
        num = [("scale", StandardScaler())] if scale else [("pass", "passthrough")]
        return ColumnTransformer([("num", Pipeline(num), numeric),
                                  ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical)])
    return {
        "Logistic regression": Pipeline([("prep", prep(True)), ("clf", LogisticRegression(max_iter=2000, C=0.5))]),
        "Gradient boosting": Pipeline([("prep", prep(False)),
                                       ("clf", HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                                                              max_iter=300, random_state=SEED))]),
    }


def top_decile_capture(y, p):
    cut = np.quantile(p, 0.9)
    return y[p >= cut].sum() / max(y.sum(), 1)


def evaluate(name, y, p, calibrated=True):
    return dict(model=name, roc_auc=roc_auc_score(y, p), pr_auc=average_precision_score(y, p),
                brier=brier_score_loss(y, p) if calibrated else None, base_rate=y.mean(),
                top_decile_capture=top_decile_capture(y, p))


def run(con):
    feats = con.sql("SELECT feature, kind FROM model_features").df()
    assert not PROTECTED & set(feats.feature), "protected attribute in feature list"
    numeric = feats[feats.kind == "numeric"].feature.tolist()
    categorical = feats[feats.kind == "categorical"].feature.tolist()
    cols = numeric + categorical

    # fixed row order: the gradient-boosting validation split follows it
    d = con.sql("""SELECT f.*, p.sex, p.payer FROM feat_index_discharges f JOIN dim_patient p USING (patient_id)
                   ORDER BY discharge_ts, admission_id""").df()
    d["discharge_ts"] = pd.to_datetime(d.discharge_ts)
    labelled = d[d.outcome_known]
    train = labelled[labelled.discharge_ts <= f"{TRAIN_END} 23:59"]
    test = labelled[labelled.discharge_ts >= TEST_START]
    y_test = test.readmitted_30d.to_numpy().astype(int)

    metrics, preds = [], {}
    for name, model in make_models(numeric, categorical).items():
        model.fit(train[cols], train.readmitted_30d.astype(int))
        p = model.predict_proba(test[cols])[:, 1]
        preds[name] = (model, p)
        metrics.append(evaluate(name, y_test, p))
    metrics.append(evaluate("LACE index", y_test, test.lace.to_numpy().astype(float), calibrated=False))
    metrics = pd.DataFrame(metrics)
    ml = metrics[metrics.model != "LACE index"]
    lr_auc = ml.loc[ml.model == "Logistic regression", "roc_auc"].item()
    # prefer the interpretable model unless the other is clearly better
    chosen = "Logistic regression" if ml.roc_auc.max() - lr_auc < 0.01 else ml.loc[ml.roc_auc.idxmax(), "model"]
    metrics["chosen"] = metrics.model == chosen
    metrics["train_rows"], metrics["test_rows"] = len(train), len(test)
    metrics["train_end"], metrics["test_start"] = TRAIN_END, TEST_START
    lace_high = test.lace >= 10
    metrics["lace_high_share"] = lace_high.mean()
    metrics["lace_high_capture"] = y_test[lace_high.to_numpy()].sum() / max(y_test.sum(), 1)
    # like for like: each model flags the same share of discharges as the LACE flag does
    scores = {name: p for name, (_, p) in preds.items()}
    scores["LACE index"] = test.lace.to_numpy().astype(float)
    def capture_at(p, share):
        flagged = np.argsort(-p, kind="stable")[: int(round(share * len(p)))]
        return y_test[flagged].sum() / max(y_test.sum(), 1)
    metrics["capture_at_lace_share"] = [capture_at(scores[m], lace_high.mean()) for m in metrics.model]

    model, p_test = preds[chosen]
    bins = pd.qcut(p_test, 10, labels=False, duplicates="drop")
    calib = pd.DataFrame({"decile": bins + 1, "predicted": p_test, "actual": y_test}) \
        .groupby("decile").agg(predicted=("predicted", "mean"), actual=("actual", "mean"),
                               discharges=("actual", "size")).reset_index()

    imp = permutation_importance(model, test[cols], y_test, scoring="roc_auc", n_repeats=5, random_state=SEED)
    importance = pd.DataFrame({"feature": cols, "importance": imp.importances_mean, "std": imp.importances_std}) \
        .sort_values("importance", ascending=False)

    # fairness: the model never sees sex, payer or age group; check it ranks each group about as well
    top_cut = np.quantile(p_test, 0.9)
    t = test.assign(score=p_test)
    fairness = []
    for attr in ("sex", "payer", "age_group"):
        for grp, g in t.groupby(attr):
            yg = g.readmitted_30d.astype(int)
            fairness.append(dict(attribute=attr, group=grp, discharges=len(g), actual_rate=yg.mean(),
                                 mean_score=g.score.mean(), roc_auc=roc_auc_score(yg, g.score),
                                 flagged_share=(g.score >= top_cut).mean(),
                                 recall_in_top_decile=yg[g.score >= top_cut].sum() / max(yg.sum(), 1)))
    fairness = pd.DataFrame(fairness)

    # refit on every labelled discharge, then score every discharge home
    final = make_models(numeric, categorical)[chosen].fit(labelled[cols], labelled.readmitted_30d.astype(int))
    d["risk_score"] = final.predict_proba(d[cols])[:, 1]
    hi, mid = np.quantile(d.risk_score, 0.9), np.quantile(d.risk_score, 0.7)
    d["risk_band"] = np.where(d.risk_score >= hi, "High", np.where(d.risk_score >= mid, "Medium", "Low"))
    d["risk_percentile"] = d.risk_score.rank(pct=True)
    d["drivers"] = drivers(final, d[cols], labelled)
    scores = d[["admission_id", "risk_score", "risk_percentile", "risk_band", "drivers"]]

    for name, df in dict(model_metrics=metrics, model_calibration=calib, model_importance=importance,
                         model_fairness=fairness, readmission_scores=scores).items():
        con.register("tmp_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM tmp_df")
        con.unregister("tmp_df")
    return metrics


# Each driver resets one factor to a typical value and measures how far the risk falls.
DRIVERS = {"los_days": ("short stay", "median"), "occupancy_at_discharge": ("full unit at discharge", "median"),
           "weekend_discharge": ("weekend discharge", 0), "er_visits_6m": ("frequent ER visits", 0),
           "admissions_12m": ("recent admissions", 0), "charlson": ("chronic conditions", 0),
           "age": ("older age", "median")}


def drivers(model, X, history):
    base = model.predict_proba(X)[:, 1]
    drops = {}
    for col, (label, ref) in DRIVERS.items():
        X2 = X.copy()
        X2[col] = history[col].median() if ref == "median" else ref
        drops[label] = base - model.predict_proba(X2)[:, 1]
    d = pd.DataFrame(drops)
    out = []
    for _, row in d.iterrows():
        top = row[row > 0.005].sort_values(ascending=False).index[:2]
        out.append(", ".join(top))
    return out
