"""
ML lifecycle API (ml_lifecycle.py): datasets, models, training, the versioned registry with stages and a quality
gate, serving, outcome recording, drift/performance monitoring, and a forecasting helper for dashboards.

Off by default per workspace (`ml_lifecycle` flag): training is compute-heavy. Managers and above build and
promote models and record ground truth (an outcome from an arbitrary user would poison the monitor); any
standard user can score with the production version. Everything is tenant-scoped — another workspace's model id is
a 404.
"""

import json
from typing import Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import ml_lifecycle as ml
import models
from auth import require_role
from database import get_db
from feature_flags import is_enabled
from rate_limit import limiter
from security import log_audit

router = APIRouter(prefix="/api/ml", tags=["ml"])
STAGES = ("none", "staging", "production", "archived")
MAX_TRAINING_IN_FLIGHT = 3


class DatasetIn(BaseModel):
    name: str
    csv: str


class ModelIn(BaseModel):
    name: str
    task: str
    target: str
    description: str = ""
    min_score: Optional[float] = None


class TrainIn(BaseModel):
    dataset_id: int
    algorithm: str = "auto"
    features: Optional[list] = None
    params: dict = {}


class PromoteIn(BaseModel):
    stage: str


class PredictIn(BaseModel):
    inputs: Union[dict, list]
    version: Optional[int] = None
    stage: str = "production"


class OutcomeIn(BaseModel):
    actual: Union[str, float, int]


class ForecastIn(BaseModel):
    points: list
    horizon: int = 6


def _gate(db: Session, user: models.User) -> models.User:
    if not is_enabled(db, user.tenant_id, "ml_lifecycle"):
        raise HTTPException(403, "ML lifecycle is turned off for this workspace — an org admin can turn it on under Feature Flags")
    return user


def ml_manager(db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))) -> models.User:
    return _gate(db, user)


def ml_user(db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))) -> models.User:
    return _gate(db, user)


def _model(db: Session, user: models.User, model_id: int) -> models.MLModel:
    m = db.query(models.MLModel).filter(models.MLModel.id == model_id, models.MLModel.tenant_id == user.tenant_id).first()
    if m is None:
        raise HTTPException(404, "Model not found")
    return m


def _version(db: Session, user: models.User, version_id: int) -> models.MLModelVersion:
    v = db.query(models.MLModelVersion).filter(models.MLModelVersion.id == version_id, models.MLModelVersion.tenant_id == user.tenant_id).first()
    if v is None:
        raise HTTPException(404, "Version not found")
    return v


def _version_out(v: models.MLModelVersion, full: bool = True) -> dict:
    out = {"id": v.id, "model_id": v.model_id, "version": v.version, "status": v.status, "error": v.error, "algorithm": v.algorithm, "stage": v.stage, "dataset_id": v.dataset_id,
           "dataset_hash": v.dataset_hash, "train_rows": v.train_rows, "duration_ms": v.duration_ms, "created_at": v.created_at.isoformat() if v.created_at else None,
           "metrics": json.loads(v.metrics) if v.metrics else None, "params": json.loads(v.params) if v.params else None}
    if full:
        out["schema"] = json.loads(v.schema) if v.schema else None
    return out


def _wrap(fn):
    try:
        return fn()
    except ml.MLError as e:
        raise HTTPException(e.status, str(e))


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def _store_dataset(db: Session, user: models.User, name: str, text: str) -> dict:
    name = name.strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "A dataset name is required (80 characters at most)")
    df = _wrap(lambda: ml.parse_csv(text))
    columns = ml.describe_columns(df)
    row = models.MLDataset(tenant_id=user.tenant_id, name=name, content=text, content_hash=ml.dataset_hash(text), n_rows=len(df), columns=json.dumps(columns), created_by=user.id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A dataset named '{name}' already exists")
    log_audit(db, user.tenant_id, user, "ml.dataset_created", "ml_dataset", row.id, after={"name": name, "rows": len(df)})
    db.commit()
    return {"id": row.id, "name": name, "n_rows": row.n_rows, "columns": columns, "content_hash": row.content_hash}


@router.post("/datasets")
def create_dataset(payload: DatasetIn, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    return _store_dataset(db, user, payload.name, payload.csv)


@router.post("/datasets/upload")
async def upload_dataset(file: UploadFile = File(...), name: Optional[str] = Form(None), db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    data = await file.read(ml.MAX_CSV_BYTES + 1)
    if len(data) > ml.MAX_CSV_BYTES:
        raise HTTPException(413, f"The CSV is larger than {ml.MAX_CSV_BYTES // 1_000_000} MB")
    return _store_dataset(db, user, name or (file.filename or "dataset").rsplit(".", 1)[0], data.decode("utf-8", errors="replace"))


@router.get("/datasets")
def list_datasets(db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    rows = db.query(models.MLDataset).filter(models.MLDataset.tenant_id == user.tenant_id).order_by(models.MLDataset.id).all()
    return [{"id": d.id, "name": d.name, "n_rows": d.n_rows, "columns": json.loads(d.columns), "content_hash": d.content_hash, "created_at": d.created_at.isoformat()} for d in rows]


@router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: int, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    d = db.query(models.MLDataset).filter(models.MLDataset.id == dataset_id, models.MLDataset.tenant_id == user.tenant_id).first()
    if d is None:
        raise HTTPException(404, "Dataset not found")
    if db.query(models.MLModelVersion).filter(models.MLModelVersion.dataset_id == d.id).first():
        raise HTTPException(409, "Model versions were trained on this dataset — keep it so they stay reproducible")
    db.delete(d)
    log_audit(db, user.tenant_id, user, "ml.dataset_deleted", "ml_dataset", dataset_id, after={"name": d.name})
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Models and versions
# ---------------------------------------------------------------------------

@router.post("/models")
def create_model(payload: ModelIn, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    if payload.task not in ("classification", "regression"):
        raise HTTPException(400, "task must be classification or regression")
    if not payload.name.strip() or not payload.target.strip():
        raise HTTPException(400, "A name and a target column are required")
    if payload.min_score is not None and not -1.0 <= payload.min_score <= 1.0:
        raise HTTPException(400, "min_score must be between -1 and 1")
    m = models.MLModel(tenant_id=user.tenant_id, name=payload.name.strip()[:80], description=payload.description[:500], task=payload.task, target=payload.target.strip(),
                       min_score=payload.min_score, created_by=user.id)
    db.add(m)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A model named '{payload.name.strip()}' already exists")
    log_audit(db, user.tenant_id, user, "ml.model_created", "ml_model", m.id, after={"name": m.name, "task": m.task, "target": m.target})
    db.commit()
    return {"id": m.id, "name": m.name, "task": m.task, "target": m.target, "min_score": m.min_score}


@router.get("/models")
def list_models(db: Session = Depends(get_db), user: models.User = Depends(ml_user)):
    out = []
    for m in db.query(models.MLModel).filter(models.MLModel.tenant_id == user.tenant_id).order_by(models.MLModel.id):
        prod = db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id, models.MLModelVersion.stage == "production").first()
        out.append({"id": m.id, "name": m.name, "task": m.task, "target": m.target, "min_score": m.min_score, "production_version": prod.version if prod else None,
                    "versions": db.query(func.count(models.MLModelVersion.id)).filter(models.MLModelVersion.model_id == m.id).scalar()})
    return out


@router.get("/models/{model_id}")
def get_model(model_id: int, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    m = _model(db, user, model_id)
    versions = db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id).order_by(models.MLModelVersion.version.desc()).all()
    return {"id": m.id, "name": m.name, "description": m.description, "task": m.task, "target": m.target, "min_score": m.min_score, "versions": [_version_out(v, full=False) for v in versions]}


@router.delete("/models/{model_id}")
def delete_model(model_id: int, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    m = _model(db, user, model_id)
    if db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id, models.MLModelVersion.stage == "production").first():
        raise HTTPException(409, "A version of this model is in production — move it out of production first")
    db.query(models.MLPrediction).filter(models.MLPrediction.model_id == m.id).delete(synchronize_session=False)
    db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id).delete(synchronize_session=False)
    log_audit(db, user.tenant_id, user, "ml.model_deleted", "ml_model", m.id, after={"name": m.name})
    db.delete(m)
    db.commit()
    return {"deleted": True}


@router.post("/models/{model_id}/train", status_code=202)
def train(model_id: int, payload: TrainIn, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    m = _model(db, user, model_id)
    dataset = db.query(models.MLDataset).filter(models.MLDataset.id == payload.dataset_id, models.MLDataset.tenant_id == user.tenant_id).first()
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    if db.query(func.count(models.MLModelVersion.id)).filter(models.MLModelVersion.tenant_id == user.tenant_id, models.MLModelVersion.status == "training").scalar() >= MAX_TRAINING_IN_FLIGHT:
        raise HTTPException(429, f"This workspace already has {MAX_TRAINING_IN_FLIGHT} training runs in progress")
    if payload.algorithm != "auto" and payload.algorithm not in ml.algorithms_for(m.task):
        raise HTTPException(400, f"algorithm must be one of: auto, {', '.join(ml.algorithms_for(m.task))}")
    number = (db.query(func.max(models.MLModelVersion.version)).filter(models.MLModelVersion.model_id == m.id).scalar() or 0) + 1
    v = models.MLModelVersion(tenant_id=user.tenant_id, model_id=m.id, version=number, dataset_id=dataset.id, dataset_hash=dataset.content_hash, status="training", created_by=user.id)
    db.add(v)
    db.flush()
    log_audit(db, user.tenant_id, user, "ml.training_started", "ml_model_version", v.id, after={"model": m.name, "version": number, "algorithm": payload.algorithm, "dataset": dataset.name})
    db.commit()
    ml.submit_training(v.id, dataset.content, {**payload.params, "algorithm": payload.algorithm, "features": payload.features})
    return {"version_id": v.id, "version": number, "status": "training"}


@router.get("/versions/{version_id}")
def get_version(version_id: int, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    return _version_out(_version(db, user, version_id))


@router.post("/versions/{version_id}/promote")
def promote(version_id: int, payload: PromoteIn, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    v = _version(db, user, version_id)
    m = db.query(models.MLModel).filter(models.MLModel.id == v.model_id).first()
    if payload.stage not in STAGES:
        raise HTTPException(400, f"stage must be one of: {', '.join(STAGES)}")
    if payload.stage in ("staging", "production") and v.status != "ready":
        raise HTTPException(409, f"Only a trained (ready) version can be promoted — this one is {v.status}")
    if payload.stage == "production" and m.min_score is not None:
        primary = json.loads(v.metrics)["primary"]
        if primary["value"] < m.min_score:
            raise HTTPException(409, f"Quality gate: this version's {primary['name']} is {primary['value']:.3f}, below the model's minimum of {m.min_score}")
    if payload.stage in ("staging", "production"):
        for other in db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id, models.MLModelVersion.stage == payload.stage, models.MLModelVersion.id != v.id):
            other.stage = "archived" if payload.stage == "production" else "none"
    before, v.stage = v.stage, payload.stage
    log_audit(db, user.tenant_id, user, "ml.version_promoted", "ml_model_version", v.id, before={"stage": before}, after={"stage": payload.stage, "model": m.name, "version": v.version})
    db.commit()
    return _version_out(v, full=False)


# ---------------------------------------------------------------------------
# Serving and monitoring
# ---------------------------------------------------------------------------

@router.post("/models/{model_id}/predict")
@limiter.limit("120/minute")
def predict(model_id: int, payload: PredictIn, request: Request, db: Session = Depends(get_db), user: models.User = Depends(ml_user)):
    m = _model(db, user, model_id)
    q = db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id, models.MLModelVersion.status == "ready")
    if payload.version is not None:
        v = q.filter(models.MLModelVersion.version == payload.version).first()
        if v is None:
            raise HTTPException(404, f"Version {payload.version} isn't a trained version of this model")
    else:
        if payload.stage not in ("production", "staging"):
            raise HTTPException(400, "stage must be production or staging")
        v = q.filter(models.MLModelVersion.stage == payload.stage).first()
        if v is None:
            raise HTTPException(409, f"No version of this model is in {payload.stage} — promote one first")
    rows = payload.inputs if isinstance(payload.inputs, list) else [payload.inputs]
    schema = json.loads(v.schema)

    def run():
        pipe = ml.load_pipeline(v)
        frame, warnings = ml.build_frame(rows, schema)
        return frame, warnings, ml.predict_rows(pipe, frame, m.task)

    frame, warnings, outputs = _wrap(run)
    logged = []
    for record, output in zip(frame.astype(object).where(frame.notna(), None).to_dict(orient="records"), outputs):
        row = models.MLPrediction(tenant_id=user.tenant_id, model_id=m.id, version_id=v.id, inputs=json.dumps(record), output=json.dumps(output))
        db.add(row)
        logged.append(row)
    db.commit()
    return {"model": m.name, "version": v.version, "stage": v.stage, "predictions": [{"prediction_id": r.id, **o} for r, o in zip(logged, outputs)], "warnings": warnings}


@router.post("/predictions/{prediction_id}/outcome")
def record_outcome(prediction_id: int, payload: OutcomeIn, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    p = db.query(models.MLPrediction).filter(models.MLPrediction.id == prediction_id, models.MLPrediction.tenant_id == user.tenant_id).first()
    if p is None:
        raise HTTPException(404, "Prediction not found")
    p.actual = str(payload.actual)
    db.commit()
    return {"recorded": True}


@router.get("/models/{model_id}/monitor")
def monitor(model_id: int, version: Optional[int] = None, window: int = 500, db: Session = Depends(get_db), user: models.User = Depends(ml_manager)):
    m = _model(db, user, model_id)
    q = db.query(models.MLModelVersion).filter(models.MLModelVersion.model_id == m.id, models.MLModelVersion.status == "ready")
    v = q.filter(models.MLModelVersion.version == version).first() if version is not None else (q.filter(models.MLModelVersion.stage == "production").first() or q.order_by(models.MLModelVersion.version.desc()).first())
    if v is None:
        raise HTTPException(404, "No trained version to monitor")
    rows = db.query(models.MLPrediction).filter(models.MLPrediction.version_id == v.id).order_by(models.MLPrediction.id.desc()).limit(min(max(window, 30), 5000)).all()
    schema, baseline, metrics = json.loads(v.schema), json.loads(v.baseline), json.loads(v.metrics)
    inputs, outputs = [json.loads(r.inputs) for r in rows], [json.loads(r.output) for r in rows]
    drift = ml.drift_report(baseline, schema, inputs, outputs, m.task)
    pairs = [(r.actual, json.loads(r.output)["label" if m.task == "classification" else "value"]) for r in rows if r.actual is not None]
    performance = _wrap(lambda: ml.performance_report(metrics, m.task, pairs))
    alerts = []
    for name in drift.get("drifted_features", []):
        alerts.append(f"Feature '{name}' has drifted from its training distribution (PSI {drift['features'][name]['psi']})")
    if drift.get("prediction_drift", {}).get("status") == "alert":
        alerts.append(f"The model's predictions have shifted (PSI {drift['prediction_drift']['psi']})")
    if performance["status"] == "degraded":
        alerts.append("Live performance is well below the holdout the model was promoted on")
    return {"model": m.name, "version": v.version, "stage": v.stage, "window": len(rows), "drift": drift, "performance": performance, "alerts": alerts}


@router.post("/forecast")
def forecast(payload: ForecastIn, user: models.User = Depends(ml_user)):
    return _wrap(lambda: ml.forecast_series(payload.points, payload.horizon))
