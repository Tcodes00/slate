"""
ML lifecycle management: datasets -> training -> a versioned registry -> serving -> monitoring.

    CSV dataset ──train──▶ version N (params, dataset hash, schema, holdout metrics, baselines, signed artifact)
                              │ promote: none → staging → production (quality gate) → archived
                              ▼
                   predict (every call logged) ──▶ drift (PSI vs training baseline) + live performance
                                                   (once ground truth arrives) ──▶ alerts

Scope, stated plainly: this is *tabular* ML on scikit-learn — classification and regression with preprocessing
(imputation, scaling, one-hot), three model families, a built-in candidate comparison ('auto'), and permutation
importance. It is a real train/serve/monitor loop, not a deep-learning platform: no GPUs, no distributed training,
no feature store, no neural nets, no scheduled retraining.

Design decisions worth knowing:

  * An artifact is a pickle, and unpickling untrusted bytes is arbitrary code execution. So every artifact is
    HMAC-signed with the server's secret (bound to the tenant and version id) at training time and the signature is
    verified BEFORE anything is unpickled; a tampered or swapped artifact is refused. Whoever can write the
    database but doesn't hold the secret cannot get code to run by editing a model.
  * Quality gate: a model can carry a minimum score, and promotion to production is refused below it.
  * Drift is measured against baselines captured at training time — numeric feature histograms on training
    quantiles, categorical frequencies — using the Population Stability Index (PSI < 0.1 stable, 0.1-0.25 shifting,
    >= 0.25 drifted), plus the drift of the model's *outputs*. Live performance needs ground truth, which arrives
    later: POST the actual outcome for a prediction and the monitor compares live accuracy / error with what the
    holdout set promised.
  * Identifier-like columns (nearly all values unique) are excluded from features automatically, with the reason
    recorded — a customer_id column otherwise lets a tree model memorise its training set and look brilliant.
"""

import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import pickle
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor, RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, mean_squared_error, r2_score, roc_auc_score
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

import models
from auth import SECRET_KEY
from database import SessionLocal

logger = logging.getLogger("slate.ml")

MAX_ROWS, MAX_COLS, MAX_CSV_BYTES, MAX_PREDICT_ROWS = 50_000, 100, 5_000_000, 1000
MIN_MONITOR_ROWS = 30
PSI_WARN, PSI_ALERT = 0.10, 0.25
_pool = ThreadPoolExecutor(max_workers=int(os.getenv("ML_WORKERS", "2")), thread_name_prefix="ml")
_CACHE: OrderedDict = OrderedDict()


class MLError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ===========================================================================
# Datasets
# ===========================================================================

def parse_csv(text: str) -> pd.DataFrame:
    if len(text.encode()) > MAX_CSV_BYTES:
        raise MLError(f"The CSV is larger than {MAX_CSV_BYTES // 1_000_000} MB", 413)
    try:
        header = next(csv.reader(io.StringIO(text)))
    except (StopIteration, csv.Error):
        raise MLError("That doesn't parse as a CSV with a header row", 400)
    if len({h.strip() for h in header}) != len(header):
        # pandas would silently rename the second 'a' to 'a.1' — a column the user never named
        raise MLError("Column names must be unique", 400)
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=True, nrows=MAX_ROWS + 1)
    except Exception:  # noqa: BLE001
        raise MLError("That doesn't parse as a CSV with a header row", 400)
    if len(df) > MAX_ROWS:
        raise MLError(f"A dataset can have at most {MAX_ROWS} rows", 413)
    if len(df) < 20:
        raise MLError("A dataset needs at least 20 rows to train on", 400)
    if df.shape[1] > MAX_COLS or df.shape[1] < 2:
        raise MLError(f"A dataset needs between 2 and {MAX_COLS} columns", 400)
    names = [str(c).strip()[:60] or f"column_{i}" for i, c in enumerate(df.columns)]
    if len(set(names)) != len(names):
        raise MLError("Column names must be unique", 400)
    df.columns = names
    return df


def type_frame(df: pd.DataFrame) -> tuple:
    """-> (typed frame, {column: 'numeric'|'categorical'}). A column is numeric when at least 90% of its non-empty
    values parse as numbers; unparseable cells in a numeric column become missing, not errors."""
    out, kinds = pd.DataFrame(index=df.index), {}
    for col in df.columns:
        stripped = df[col].astype("string").str.strip().replace("", pd.NA)
        nums = pd.to_numeric(stripped, errors="coerce")
        present = stripped.notna().sum()
        if present and nums.notna().sum() / present >= 0.9:
            out[col], kinds[col] = nums.astype(float), "numeric"
        else:
            out[col], kinds[col] = stripped.astype(object).where(stripped.notna(), np.nan), "categorical"
    return out, kinds


def describe_columns(df: pd.DataFrame) -> list:
    typed, kinds = type_frame(df)
    return [{"name": c, "kind": kinds[c], "missing": int(typed[c].isna().sum()), "unique": int(typed[c].nunique(dropna=True))} for c in typed.columns]


def dataset_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ===========================================================================
# Training
# ===========================================================================

def _estimator(task: str, algorithm: str, params: dict):
    n = int(min(max(params.get("n_estimators", 100), 10), 200))
    depth = params.get("max_depth")
    depth = int(min(max(depth, 1), 20)) if depth else None
    if task == "classification":
        return {"logistic": lambda: LogisticRegression(max_iter=1000, C=float(min(max(params.get("C", 1.0), 1e-3), 1e3))),
                "random_forest": lambda: RandomForestClassifier(n_estimators=n, max_depth=depth, random_state=42, n_jobs=1),
                "gradient_boosting": lambda: GradientBoostingClassifier(n_estimators=n, max_depth=depth or 3, learning_rate=float(min(max(params.get("learning_rate", 0.1), 0.01), 1.0)), random_state=42)}[algorithm]()
    return {"linear": lambda: Ridge(alpha=float(min(max(params.get("alpha", 1.0), 1e-3), 1e3))),
            "random_forest": lambda: RandomForestRegressor(n_estimators=n, max_depth=depth, random_state=42, n_jobs=1),
            "gradient_boosting": lambda: GradientBoostingRegressor(n_estimators=n, max_depth=depth or 3, learning_rate=float(min(max(params.get("learning_rate", 0.1), 0.01), 1.0)), random_state=42)}[algorithm]()


def algorithms_for(task: str) -> list:
    return ["logistic", "random_forest", "gradient_boosting"] if task == "classification" else ["linear", "random_forest", "gradient_boosting"]


def _pipeline(numeric: list, categorical: list, estimator) -> Pipeline:
    parts = []
    if numeric:
        parts.append(("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric))
    if categorical:
        parts.append(("cat", Pipeline([("impute", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), categorical))
    return Pipeline([("prep", ColumnTransformer(parts)), ("model", estimator)])


def _psi(expected: list, actual: list) -> float:
    eps = 1e-4
    return float(sum((max(a, eps) - max(e, eps)) * math.log(max(a, eps) / max(e, eps)) for e, a in zip(expected, actual)))


def _numeric_baseline(series: pd.Series) -> dict:
    x = series.dropna().to_numpy(dtype=float)
    edges = sorted(set(np.quantile(x, np.linspace(0, 1, 11))[1:-1].tolist())) if len(x) else []
    counts, _ = np.histogram(x, bins=[-np.inf, *edges, np.inf])
    return {"kind": "numeric", "edges": edges, "proportions": (counts / max(counts.sum(), 1)).tolist(), "missing_rate": float(series.isna().mean()),
            "min": float(x.min()) if len(x) else None, "max": float(x.max()) if len(x) else None, "mean": float(x.mean()) if len(x) else None}


def _categorical_baseline(series: pd.Series) -> dict:
    freq = series.dropna().astype(str).value_counts(normalize=True)
    top = freq.head(20)
    return {"kind": "categorical", "categories": {k: float(v) for k, v in top.items()}, "other": float(1 - top.sum()), "missing_rate": float(series.isna().mean())}


def train_version(version_id: int, dataset_text: str, params: dict) -> None:
    """Runs on a worker thread. Never raises: every outcome is recorded on the version row."""
    db = SessionLocal()
    started = time.monotonic()
    try:
        version = db.get(models.MLModelVersion, version_id)
        model = db.get(models.MLModel, version.model_id)
        df, kinds = type_frame(parse_csv(dataset_text))
        target, task = model.target, model.task
        if target not in df.columns:
            raise MLError(f"The dataset has no column named '{target}'")
        df = df[df[target].notna()].copy()
        if task == "regression":
            if kinds[target] != "numeric":
                raise MLError(f"'{target}' isn't numeric, so it can't be a regression target")
            y = df[target].astype(float)
            classes = None
        else:
            y = df[target].astype(str)
            counts = y.value_counts()
            classes = sorted(counts.index.tolist())
            if len(classes) < 2:
                raise MLError(f"'{target}' has only one value — there's nothing to learn")
            if counts.min() < 3:
                raise MLError(f"The class '{counts.idxmin()}' has only {int(counts.min())} example(s) — each class needs at least 3")
            if len(classes) > 20:
                raise MLError(f"'{target}' has {len(classes)} distinct values — that looks like a number or an identifier, not a class")

        wanted = params.get("features")
        excluded, features = [], []
        for col in df.columns:
            if col == target or (wanted and col not in wanted):
                continue
            nunique = df[col].nunique(dropna=True)
            if nunique <= 1:
                excluded.append({"name": col, "reason": "constant"})
            elif kinds[col] == "categorical" and (nunique / max(len(df), 1) > 0.5 or nunique > 100):
                excluded.append({"name": col, "reason": f"looks like an identifier ({nunique} distinct values) — would let the model memorise rows"})
            else:
                features.append(col)
        if not features:
            raise MLError("No usable feature columns remain after excluding the target, constants and identifiers")
        numeric, categorical = [c for c in features if kinds[c] == "numeric"], [c for c in features if kinds[c] == "categorical"]
        X = df[features]
        stratify = y if task == "classification" else None
        X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=stratify)

        requested = params.get("algorithm", "auto")
        candidates = algorithms_for(task) if requested == "auto" else [requested]
        if any(c not in algorithms_for(task) for c in candidates):
            raise MLError(f"algorithm must be one of: auto, {', '.join(algorithms_for(task))}")
        scoring = "roc_auc" if task == "classification" and len(classes) == 2 else ("accuracy" if task == "classification" else "r2")
        cv_scores = {}
        if len(candidates) > 1:
            for name in candidates:
                cv_scores[name] = float(np.mean(cross_val_score(_pipeline(numeric, categorical, _estimator(task, name, params)), X_tr, y_tr, cv=3, scoring=scoring)))
            chosen = max(cv_scores, key=cv_scores.get)
        else:
            chosen = candidates[0]
        pipe = _pipeline(numeric, categorical, _estimator(task, chosen, params)).fit(X_tr, y_tr)

        pred = pipe.predict(X_te)
        if task == "classification":
            proba = pipe.predict_proba(X_te)
            metrics = {"accuracy": float(accuracy_score(y_te, pred)), "f1_macro": float(f1_score(y_te, pred, average="macro")),
                       "baseline_accuracy": float(y_te.value_counts(normalize=True).max())}
            if len(classes) == 2:
                metrics["roc_auc"] = float(roc_auc_score((y_te == classes[1]).astype(int), proba[:, 1]))
            else:
                metrics["roc_auc"] = float(roc_auc_score(y_te, proba, multi_class="ovr", average="macro", labels=pipe.classes_))
            primary = ("roc_auc", metrics["roc_auc"]) if len(classes) == 2 else ("accuracy", metrics["accuracy"])
            pred_baseline = {"kind": "categorical", "categories": {str(k): float(v) for k, v in pd.Series(pred).value_counts(normalize=True).items()}, "other": 0.0}
        else:
            metrics = {"r2": float(r2_score(y_te, pred)), "mae": float(mean_absolute_error(y_te, pred)), "rmse": float(math.sqrt(mean_squared_error(y_te, pred))),
                       "baseline_mae": float(mean_absolute_error(y_te, np.full(len(y_te), y_tr.mean())))}
            primary = ("r2", metrics["r2"])
            pred_baseline = _numeric_baseline(pd.Series(pred))
        metrics["primary"] = {"name": primary[0], "value": primary[1]}
        if cv_scores:
            metrics["candidate_cv_scores"] = cv_scores

        sample = X_te.sample(min(len(X_te), 400), random_state=42)
        imp = permutation_importance(pipe, sample, y_te.loc[sample.index], n_repeats=3, random_state=42, scoring="accuracy" if task == "classification" else "r2")
        importance = sorted(({"feature": f, "importance": float(v)} for f, v in zip(features, imp.importances_mean)), key=lambda r: -r["importance"])

        schema = {"features": [{"name": c, "kind": kinds[c], "categories": (sorted(df[c].dropna().astype(str).unique().tolist())[:50] if kinds[c] == "categorical" else None)} for c in features],
                  "target": {"name": target, "kind": kinds[target], "classes": classes}, "excluded": excluded, "n_train": int(len(X_tr)), "n_test": int(len(X_te)), "importance": importance}
        baseline = {"features": {c: (_numeric_baseline(X_tr[c]) if kinds[c] == "numeric" else _categorical_baseline(X_tr[c])) for c in features}, "predictions": pred_baseline}

        artifact = pickle.dumps(pipe, protocol=pickle.HIGHEST_PROTOCOL)
        version.algorithm, version.params = chosen, json.dumps({k: v for k, v in params.items() if k != "features"})
        version.schema, version.metrics, version.baseline = json.dumps(schema), json.dumps(metrics), json.dumps(baseline)
        version.artifact, version.artifact_sig = artifact, sign(version.tenant_id, version.id, artifact)
        version.train_rows, version.status, version.error = int(len(X_tr)), "ready", None
    except MLError as e:
        db.rollback()
        _fail(db, version_id, str(e))
    except Exception as e:  # noqa: BLE001 — any training failure must land on the row, not kill the worker
        logger.exception("training version %s failed", version_id)
        db.rollback()
        _fail(db, version_id, f"Training failed ({type(e).__name__}) — check the dataset's columns and target")
    else:
        version.duration_ms = int((time.monotonic() - started) * 1000)
        db.commit()
    finally:
        db.close()


def _fail(db, version_id: int, message: str) -> None:
    version = db.get(models.MLModelVersion, version_id)
    version.status, version.error = "failed", message[:500]
    db.commit()


def submit_training(version_id: int, dataset_text: str, params: dict) -> None:
    _pool.submit(train_version, version_id, dataset_text, params)


# ===========================================================================
# Signed artifacts
# ===========================================================================

def sign(tenant_id: int, version_id: int, artifact: bytes) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{tenant_id}:{version_id}:".encode() + artifact, hashlib.sha256).hexdigest()


def load_pipeline(version: models.MLModelVersion) -> Pipeline:
    """Verify the signature, THEN unpickle. Never the other way round."""
    cached = _CACHE.get(version.id)
    if cached is not None and cached[0] == version.artifact_sig:
        return cached[1]
    if not version.artifact or not version.artifact_sig or not hmac.compare_digest(version.artifact_sig, sign(version.tenant_id, version.id, version.artifact)):
        raise MLError("This model artifact failed its integrity check and was not loaded", 409)
    pipe = pickle.loads(version.artifact)  # noqa: S301 — signature verified above
    _CACHE[version.id] = (version.artifact_sig, pipe)
    while len(_CACHE) > 16:
        _CACHE.popitem(last=False)
    return pipe


# ===========================================================================
# Serving
# ===========================================================================

def build_frame(rows: list, schema: dict) -> tuple:
    """-> (DataFrame in training column order, warnings). Missing features are imputed by the pipeline (and said so);
    extra columns are ignored (and said so); a non-numeric value in a numeric feature becomes missing."""
    if not rows or len(rows) > MAX_PREDICT_ROWS:
        raise MLError(f"Send between 1 and {MAX_PREDICT_ROWS} rows per call", 413 if rows else 400)
    names = [f["name"] for f in schema["features"]]
    warnings, missing, extra, coerced = [], set(), set(), 0
    frame = {}
    for f in schema["features"]:
        col = []
        for r in rows:
            if not isinstance(r, dict):
                raise MLError("Each input must be an object of feature → value", 400)
            v = r.get(f["name"])
            if v is None or v == "":
                missing.add(f["name"])
                col.append(np.nan)
            elif f["kind"] == "numeric":
                try:
                    col.append(float(v))
                except (TypeError, ValueError):
                    coerced += 1
                    col.append(np.nan)
            else:
                col.append(str(v))
        frame[f["name"]] = col
    for r in rows:
        extra |= {k for k in r if k not in names}
    if missing:
        warnings.append(f"Missing values for {', '.join(sorted(missing))} were imputed from the training data")
    if coerced:
        warnings.append(f"{coerced} non-numeric value(s) in numeric features were treated as missing")
    if extra:
        warnings.append(f"Ignored columns the model wasn't trained on: {', '.join(sorted(extra))}")
    return pd.DataFrame(frame, columns=names).astype({f["name"]: float for f in schema["features"] if f["kind"] == "numeric"}), warnings


def predict_rows(pipe: Pipeline, frame: pd.DataFrame, task: str) -> list:
    if task == "classification":
        proba = pipe.predict_proba(frame)
        classes = [str(c) for c in pipe.classes_]
        return [{"label": classes[int(np.argmax(p))], "probability": float(np.max(p)), "probabilities": {c: float(v) for c, v in zip(classes, p)}} for p in proba]
    return [{"value": float(v)} for v in pipe.predict(frame)]


# ===========================================================================
# Monitoring
# ===========================================================================

def _status(value: float) -> str:
    return "alert" if value >= PSI_ALERT else "warn" if value >= PSI_WARN else "ok"


def drift_report(baseline: dict, schema: dict, input_rows: list, output_rows: list, task: str) -> dict:
    n = len(input_rows)
    if n < MIN_MONITOR_ROWS:
        return {"status": "insufficient_data", "rows": n, "needed": MIN_MONITOR_ROWS, "features": {}}
    live = pd.DataFrame(input_rows)
    out: dict = {}
    for f in schema["features"]:
        name = f["name"]
        base = baseline["features"][name]
        col = live[name] if name in live else pd.Series([np.nan] * n)
        if base["kind"] == "numeric":
            x = pd.to_numeric(col, errors="coerce").dropna().to_numpy(dtype=float)
            counts, _ = np.histogram(x, bins=[-np.inf, *base["edges"], np.inf])
            actual = (counts / max(counts.sum(), 1)).tolist() if len(x) else base["proportions"]
            value = _psi(base["proportions"], actual)
        else:
            cats = list(base["categories"])
            freq = col.dropna().astype(str).value_counts(normalize=True)
            actual = [float(freq.get(c, 0.0)) for c in cats] + [float(max(0.0, 1 - sum(freq.get(c, 0.0) for c in cats)))]
            value = _psi(list(base["categories"].values()) + [base["other"]], actual)
        out[name] = {"psi": round(value, 4), "status": _status(value), "missing_rate": round(float(col.isna().mean()), 3), "baseline_missing_rate": round(base["missing_rate"], 3)}
    pb = baseline["predictions"]
    if task == "classification":
        labels = pd.Series([o["label"] for o in output_rows]).value_counts(normalize=True)
        cats = list(pb["categories"])
        pred_psi = _psi(list(pb["categories"].values()), [float(labels.get(c, 0.0)) for c in cats])
    else:
        vals = np.array([o["value"] for o in output_rows], dtype=float)
        counts, _ = np.histogram(vals, bins=[-np.inf, *pb["edges"], np.inf])
        pred_psi = _psi(pb["proportions"], (counts / max(counts.sum(), 1)).tolist())
    worst = max([v["psi"] for v in out.values()] + [pred_psi])
    return {"status": _status(worst), "rows": n, "features": out, "drifted_features": sorted(k for k, v in out.items() if v["status"] == "alert"),
            "prediction_drift": {"psi": round(pred_psi, 4), "status": _status(pred_psi)}}


def performance_report(metrics: dict, task: str, pairs: list) -> dict:
    if len(pairs) < 20:
        return {"status": "insufficient_data", "labelled_rows": len(pairs), "needed": 20}
    if task == "classification":
        actual, pred = [str(a) for a, _ in pairs], [str(p) for _, p in pairs]
        live = {"accuracy": float(accuracy_score(actual, pred)), "f1_macro": float(f1_score(actual, pred, average="macro"))}
        drop = metrics["accuracy"] - live["accuracy"]
        degraded = drop > 0.10 or (metrics["f1_macro"] - live["f1_macro"]) > 0.10
        return {"status": "degraded" if degraded else "ok", "labelled_rows": len(pairs), "live": live, "holdout": {"accuracy": metrics["accuracy"], "f1_macro": metrics["f1_macro"]}, "accuracy_drop": round(drop, 4)}
    a, p = np.array([float(x) for x, _ in pairs]), np.array([float(x) for _, x in pairs])
    live = {"mae": float(mean_absolute_error(a, p)), "r2": float(r2_score(a, p))}
    degraded = live["mae"] > 1.5 * metrics["mae"] or (metrics["r2"] - live["r2"]) > 0.15
    return {"status": "degraded" if degraded else "ok", "labelled_rows": len(pairs), "live": live, "holdout": {"mae": metrics["mae"], "r2": metrics["r2"]}}


# ===========================================================================
# Forecasting (predictive analytics for dashboards)
# ===========================================================================

def forecast_series(points: list, horizon: int) -> dict:
    """Monthly totals -> a linear trend plus, with 24+ months, a seasonal profile; 80% and 95% prediction intervals from the
    residual spread. Deliberately simple and explainable: it returns the slope, the fit's R², and the method used."""
    if not 1 <= horizon <= 24:
        raise MLError("horizon must be between 1 and 24 months", 400)
    try:
        frame = pd.DataFrame([{"date": pd.to_datetime(p["date"]), "value": float(p["value"])} for p in points])
    except (KeyError, TypeError, ValueError):
        raise MLError("Each point needs a 'date' (YYYY-MM-DD) and a numeric 'value'", 400)
    monthly = frame.groupby(frame["date"].dt.to_period("M"))["value"].sum().sort_index()
    if len(monthly) < 6:
        raise MLError("Forecasting needs at least 6 months of history", 400)
    full = monthly.reindex(pd.period_range(monthly.index.min(), monthly.index.max(), freq="M"), fill_value=0.0)
    t = np.arange(len(full), dtype=float)
    y = full.to_numpy(dtype=float)
    slope, intercept = np.polyfit(t, y, 1)
    fitted = slope * t + intercept
    seasonal = np.zeros(12)
    method = "linear trend"
    if len(full) >= 24:
        resid = y - fitted
        months = np.array([p.month - 1 for p in full.index])
        seasonal = np.array([resid[months == m].mean() if (months == m).any() else 0.0 for m in range(12)])
        seasonal -= seasonal.mean()
        method = "linear trend + monthly seasonality"
        fitted = fitted + seasonal[months]
    resid_std = float(np.std(y - fitted, ddof=2)) if len(y) > 2 else 0.0
    ss_res, ss_tot = float(np.sum((y - fitted) ** 2)), float(np.sum((y - y.mean()) ** 2))
    out = []
    for h in range(1, horizon + 1):
        idx = len(full) - 1 + h
        period = full.index[-1] + h
        value = float(slope * idx + intercept + seasonal[period.month - 1])
        width = resid_std * math.sqrt(1 + 1 / len(y) + (idx - t.mean()) ** 2 / max(float(np.sum((t - t.mean()) ** 2)), 1e-9))
        out.append({"period": str(period), "forecast": round(value, 2), "low_80": round(value - 1.28 * width, 2), "high_80": round(value + 1.28 * width, 2),
                    "low_95": round(value - 1.96 * width, 2), "high_95": round(value + 1.96 * width, 2)})
    return {"method": method, "history_months": len(full), "slope_per_month": round(float(slope), 4), "r2": round(1 - ss_res / ss_tot, 4) if ss_tot else None, "forecast": out}
