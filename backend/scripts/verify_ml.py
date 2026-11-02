#!/usr/bin/env python3
"""
End-to-end verification of ML lifecycle management (backend/ml_lifecycle.py, routers/ml.py).

    python backend/scripts/verify_ml.py

Real datasets are generated here (a churn problem with a known signal and a pricing regression), uploaded, trained
on through the real API, promoted, served, and then *monitored* — with in-distribution traffic, a deliberately
shifted population, and deliberately wrong ground truth — to check that drift and degradation are actually
detected, not just computed. It also tries to get code executed through a tampered model artifact. See
verify_common.py for the harness contract.
"""

import hashlib
import io
import json
import math
import os
import pickle
import secrets
import sys
import time
from pathlib import Path

import httpx
import numpy as np

import verify_common as v
from verify_common import Tenant, api, check, db_exec, db_one, section

TA = TB = TF = None
CTX: dict = {}
MARKER = Path(f"/tmp/slate-pwned-{secrets.token_hex(4)}")


# ---------------------------------------------------------------- data
def churn(n, seed, shift=False):
    rng = np.random.default_rng(seed)
    tenure = rng.gamma(2.0, 12.0, n) + (60 if shift else 0)
    spend = np.clip(rng.normal(80, 25, n), 5, None)
    tickets = rng.poisson(1.5 * (3 if shift else 1), n)
    plan = rng.choice(["basic", "pro", "enterprise"], n, p=[0.5, 0.35, 0.15]) if not shift else np.array(["enterprise"] * n)
    region = rng.choice(["emea", "amer", "apac"], n)
    logit = 0.4 - 0.08 * tenure + 0.9 * tickets + 1.4 * (plan == "basic") - 1.4 * (plan == "enterprise") + rng.normal(0, 0.8, n)
    churned = rng.random(n) < 1 / (1 + np.exp(-logit))
    return {"tenure": tenure, "monthly_spend": spend, "support_tickets": tickets, "plan": plan, "region": region, "churned": churned, "n": n}


def churn_csv(n, seed):
    d = churn(n, seed)
    lines = ["customer_id,tenure,monthly_spend,support_tickets,plan,region,churned"]
    for i in range(d["n"]):
        spend = "" if i % 20 == 0 else f"{d['monthly_spend'][i]:.1f}"  # 5% missing
        lines.append(f"cust_{i:05d},{d['tenure'][i]:.1f},{spend},{d['support_tickets'][i]},{d['plan'][i]},{d['region'][i]},{'yes' if d['churned'][i] else 'no'}")
    return "\n".join(lines)


def churn_rows(d):
    return [{"tenure": round(float(d["tenure"][i]), 1), "monthly_spend": round(float(d["monthly_spend"][i]), 1), "support_tickets": int(d["support_tickets"][i]),
             "plan": str(d["plan"][i]), "region": str(d["region"][i])} for i in range(d["n"])]


def pricing_csv(n, seed):
    rng = np.random.default_rng(seed)
    sqft, rooms, age = rng.normal(1500, 400, n).clip(300), rng.integers(1, 7, n), rng.integers(0, 60, n)
    city = rng.choice(["alpha", "beta", "gamma"], n)
    price = 50 * sqft + 10_000 * rooms - 800 * age + np.where(city == "beta", 30_000, np.where(city == "gamma", -20_000, 0)) + rng.normal(0, 15_000, n)
    return "sqft,rooms,age,city,price\n" + "\n".join(f"{sqft[i]:.0f},{rooms[i]},{age[i]},{city[i]},{price[i]:.0f}" for i in range(n))


def wait_done(tenant, version_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = tenant.call("GET", f"/api/ml/versions/{version_id}").json()
        if r["status"] in ("ready", "failed"):
            return r
        time.sleep(0.4)
    return tenant.call("GET", f"/api/ml/versions/{version_id}").json()


def train(tenant, model_id, dataset_id, **kw):
    r = tenant.call("POST", f"/api/ml/models/{model_id}/train", json={"dataset_id": dataset_id, **kw})
    assert r.status_code == 202, r.text
    return wait_done(tenant, r.json()["version_id"])


def predict(tenant, model_id, inputs, token=None, **kw):
    return api("POST", f"/api/ml/models/{model_id}/predict", token or tenant.token, json={"inputs": inputs, **kw})


# ===========================================================================
@section("1. Gating, roles, and dataset validation")
def sec_datasets():
    check("flag OFF (the default) → every ML endpoint is refused (403) with how to enable it", TF.call("GET", "/api/ml/models").status_code == 403 and "turned off" in TF.call("GET", "/api/ml/models").text)
    standard, _ = TA.add_user("standard_user")
    CTX["standard"] = standard
    check("a standard user can list models but can't create datasets/models, train, or promote (403) — building models is a manager's job",
          api("GET", "/api/ml/models", standard).status_code == 200 and api("POST", "/api/ml/datasets", standard, json={"name": "x", "csv": "a,b\n1,2"}).status_code == 403
          and api("POST", "/api/ml/models", standard, json={"name": "m", "task": "regression", "target": "y"}).status_code == 403)
    check("anonymous → 401", api("GET", "/api/ml/models").status_code == 401)

    def bad(csv, name="bad"):
        return TA.call("POST", "/api/ml/datasets", json={"name": name, "csv": csv})

    rows = "\n".join(f"{i},{i * 2}" for i in range(30))
    check("dataset validation: under 20 rows → 400; one column → 400; duplicate column names → 400; not a CSV → 400",
          bad("a,b\n1,2\n3,4").status_code == 400 and bad("a\n" + "\n".join(str(i) for i in range(30))).status_code == 400 and bad("a,a\n" + rows).status_code == 400 and bad("").status_code == 400)
    check("over 50,000 rows → 413; over 5 MB → 413", bad("a,b\n" + "\n".join("1,2" for _ in range(50_001))).status_code == 413 and bad("a,b\n" + ("x" * 60 + ",1\n") * 100_000).status_code == 413)

    csv = churn_csv(2000, 1)
    r = TA.call("POST", "/api/ml/datasets", json={"name": "churn", "csv": csv})
    check("a real dataset uploads, and its columns are summarised with inferred types and missing counts", r.status_code == 200 and r.json()["n_rows"] == 2000, r.text[:200])
    cols = {c["name"]: c for c in r.json()["columns"]}
    CTX["churn_ds"], CTX["churn_csv"] = r.json()["id"], csv
    check("…tenure/spend/tickets are numeric, plan/region/customer_id categorical, and the 5% empty spend cells are counted",
          cols["tenure"]["kind"] == "numeric" and cols["support_tickets"]["kind"] == "numeric" and cols["plan"]["kind"] == "categorical" and cols["customer_id"]["kind"] == "categorical"
          and cols["monthly_spend"]["missing"] == 100, str(cols["monthly_spend"]))
    check("…and a content hash is recorded (so every model version can say exactly which data it saw)", r.json()["content_hash"] == hashlib.sha256(csv.encode()).hexdigest())
    check("a duplicate dataset name → 409", TA.call("POST", "/api/ml/datasets", json={"name": "churn", "csv": csv}).status_code == 409)
    up = api("POST", "/api/ml/datasets/upload", TA.token, files={"file": ("pricing.csv", pricing_csv(1500, 5).encode(), "text/csv")})
    check("multipart CSV upload works too (the name defaults to the file name)", up.status_code == 200 and up.json()["name"] == "pricing" and up.json()["n_rows"] == 1500, up.text[:200])
    CTX["price_ds"] = up.json()["id"]
    check("workspace B sees none of A's datasets", TB.call("GET", "/api/ml/datasets").json() == [])


# ===========================================================================
@section("2. Training: real metrics, honest baselines, identifier columns excluded, failures explained")
def sec_training():
    m = TA.call("POST", "/api/ml/models", json={"name": "churn-model", "task": "classification", "target": "churned", "min_score": 0.7, "description": "who will churn"}).json()
    CTX["m"] = m["id"]
    check("duplicate model name → 409; bad task → 400; out-of-range min_score → 400", TA.call("POST", "/api/ml/models", json={"name": "churn-model", "task": "classification", "target": "x"}).status_code == 409
          and TA.call("POST", "/api/ml/models", json={"name": "z", "task": "clustering", "target": "x"}).status_code == 400 and TA.call("POST", "/api/ml/models", json={"name": "z", "task": "regression", "target": "x", "min_score": 5}).status_code == 400)
    started = TA.call("POST", f"/api/ml/models/{CTX['m']}/train", json={"dataset_id": CTX["churn_ds"], "algorithm": "auto"})
    check("training returns immediately with 202 and a version in 'training' (it runs on a worker, not in the request)",
          started.status_code == 202 and started.json()["status"] == "training" and started.json()["version"] == 1, started.text)
    v1 = wait_done(TA, started.json()["version_id"])
    met = v1["metrics"]
    check("version 1 (auto) trains successfully", v1["status"] == "ready" and v1["version"] == 1, str(v1.get("error")))
    check("it BEATS the majority-class baseline by a real margin on a held-out 20% (accuracy vs baseline, AUC)", met["accuracy"] >= met["baseline_accuracy"] + 0.08 and met["roc_auc"] >= 0.75,
          f"acc {met['accuracy']:.3f} vs base {met['baseline_accuracy']:.3f}, auc {met['roc_auc']:.3f}")
    cv = met["candidate_cv_scores"]
    check("'auto' compared all three algorithms by cross-validation and kept the best one", set(cv) == {"logistic", "random_forest", "gradient_boosting"} and v1["algorithm"] == max(cv, key=cv.get), str(cv))
    sch = v1["schema"]
    check("the identifier column (customer_id: 2000 distinct values) was EXCLUDED, with the reason recorded — otherwise a tree model could memorise rows",
          [e["name"] for e in sch["excluded"]] == ["customer_id"] and "identifier" in sch["excluded"][0]["reason"] and "customer_id" not in [f["name"] for f in sch["features"]])
    check("the schema records features with their kinds, the target's classes, and a 80/20 split",
          [f["name"] for f in sch["features"]] == ["tenure", "monthly_spend", "support_tickets", "plan", "region"] and sch["target"]["classes"] == ["no", "yes"] and sch["n_train"] == 1600 and sch["n_test"] == 400)
    top = [i["feature"] for i in sch["importance"][:3]]
    check("permutation importance ranks the features that actually drive the generated churn on top", set(top[:2]) <= {"tenure", "support_tickets", "plan"} and "region" not in top[:2], str(sch["importance"]))
    check("the version records the dataset hash it saw, its params, duration, and train rows", v1["dataset_hash"] == hashlib.sha256(CTX["churn_csv"].encode()).hexdigest() and v1["train_rows"] == 1600 and v1["duration_ms"] > 0)

    others = {a: train(TA, CTX["m"], CTX["churn_ds"], algorithm=a) for a in ("logistic", "random_forest", "gradient_boosting")}
    check("explicit algorithms produce versions 2, 3, 4, each with its own metrics (an experiment history)",
          [x["version"] for x in others.values()] == [2, 3, 4] and all(x["status"] == "ready" for x in others.values()) and [x["algorithm"] for x in others.values()] == ["logistic", "random_forest", "gradient_boosting"])
    listing = TA.call("GET", f"/api/ml/models/{CTX['m']}").json()["versions"]
    check("the model page lists every version newest first, with metrics, for comparison", [x["version"] for x in listing] == [4, 3, 2, 1] and all(x["metrics"]["primary"]["name"] == "roc_auc" for x in listing))
    CTX["versions"] = {x["version"]: x["id"] for x in listing}
    check("an unknown algorithm is refused up front (400)", TA.call("POST", f"/api/ml/models/{CTX['m']}/train", json={"dataset_id": CTX["churn_ds"], "algorithm": "deep_magic"}).status_code == 400)
    check("training on another workspace's dataset → 404", TB.call("POST", f"/api/ml/models/{CTX['m']}/train", json={"dataset_id": CTX["churn_ds"]}).status_code == 404)

    # failures land on the version row, with a reason a person can act on, and don't hurt the next run
    wrong = TA.call("POST", "/api/ml/models", json={"name": "wrong-target", "task": "classification", "target": "nope"}).json()
    f1 = train(TA, wrong["id"], CTX["churn_ds"])
    check("a target column that doesn't exist → the version FAILS with 'no column named …' (no stack trace)", f1["status"] == "failed" and "no column named 'nope'" in f1["error"], str(f1))
    one = TA.call("POST", "/api/ml/models", json={"name": "one-class", "task": "classification", "target": "region"}).json()
    ds1 = TA.call("POST", "/api/ml/datasets", json={"name": "single", "csv": "a,b,region\n" + "\n".join(f"{i},{i % 3},emea" for i in range(40))}).json()
    check("a target with a single value → failed: 'only one value — nothing to learn'", "only one value" in (train(TA, one["id"], ds1["id"])["error"] or ""))
    badreg = TA.call("POST", "/api/ml/models", json={"name": "bad-reg", "task": "regression", "target": "plan"}).json()
    check("a non-numeric regression target → failed with the reason", "isn't numeric" in (train(TA, badreg["id"], CTX["churn_ds"])["error"] or ""))
    check("…and training still works right after those failures", train(TA, CTX["m"], CTX["churn_ds"], algorithm="logistic")["status"] == "ready")

    pm = TA.call("POST", "/api/ml/models", json={"name": "pricing-model", "task": "regression", "target": "price"}).json()
    CTX["pm"] = pm["id"]
    pv = train(TA, pm["id"], CTX["price_ds"])
    noise_floor = 0.7979 * 15_000  # E|N(0, 15000)|: the best any model can do on this data, because the noise is irreducible
    check("a regression model trains and reaches the data's NOISE FLOOR: R² ≥ 0.8 (the injected noise caps it near 0.86), MAE within 15% of the irreducible 11,970, far below the predict-the-mean baseline",
          pv["status"] == "ready" and pv["metrics"]["r2"] >= 0.8 and pv["metrics"]["mae"] <= 1.15 * noise_floor and pv["metrics"]["mae"] < 0.5 * pv["metrics"]["baseline_mae"] and pv["metrics"]["primary"]["name"] == "r2", str(pv["metrics"]))
    check("a dataset that versions were trained on can't be deleted (they must stay reproducible) → 409", TA.call("DELETE", f"/api/ml/datasets/{CTX['churn_ds']}").status_code == 409)


# ===========================================================================
@section("3. The registry: stages, one production version, a quality gate, rollback")
def sec_registry():
    vs, mid = CTX["versions"], CTX["m"]
    check("no version is in production yet, so predicting is refused (409) with what to do", predict(TA, mid, {"tenure": 5}).status_code == 409 and "promote" in predict(TA, mid, {"tenure": 5}).text)
    gate = TA.call("POST", "/api/ml/models", json={"name": "gated", "task": "regression", "target": "price", "min_score": 0.999999}).json()
    gv = train(TA, gate["id"], CTX["price_ds"])
    r = TA.call("POST", f"/api/ml/versions/{gv['id']}/promote", json={"stage": "production"})
    check("QUALITY GATE: a model with min_score 0.999999 refuses to promote a version below it (409), naming the metric and the threshold", r.status_code == 409 and "Quality gate" in r.text and "r2" in r.text, r.text)
    check("…but the same version may go to STAGING (the gate protects production)", TA.call("POST", f"/api/ml/versions/{gv['id']}/promote", json={"stage": "staging"}).status_code == 200)
    check("the gate-passing churn model promotes: v2 → staging, v2 → production", TA.call("POST", f"/api/ml/versions/{vs[2]}/promote", json={"stage": "staging"}).status_code == 200
          and TA.call("POST", f"/api/ml/versions/{vs[2]}/promote", json={"stage": "production"}).json()["stage"] == "production")
    TA.call("POST", f"/api/ml/versions/{vs[3]}/promote", json={"stage": "production"})
    stages = {x["version"]: x["stage"] for x in TA.call("GET", f"/api/ml/models/{mid}").json()["versions"]}
    check("promoting v3 to production ARCHIVES v2 — there is exactly one production version", stages[3] == "production" and stages[2] == "archived" and list(stages.values()).count("production") == 1, str(stages))
    check("the model list reports the production version", next(m for m in TA.call("GET", "/api/ml/models").json() if m["id"] == mid)["production_version"] == 3)
    TA.call("POST", f"/api/ml/versions/{vs[2]}/promote", json={"stage": "production"})
    stages = {x["version"]: x["stage"] for x in TA.call("GET", f"/api/ml/models/{mid}").json()["versions"]}
    check("ROLLBACK: promoting the older v2 again swaps them back (v2 production, v3 archived)", stages[2] == "production" and stages[3] == "archived")
    wrong_id = next(m["id"] for m in TA.call("GET", "/api/ml/models").json() if m["name"] == "wrong-target")
    failed = TA.call("GET", f"/api/ml/models/{wrong_id}").json()["versions"][0]
    TA.call("POST", f"/api/ml/versions/{vs[3]}/promote", json={"stage": "staging"})
    stages = {x["version"]: x["stage"] for x in TA.call("GET", f"/api/ml/models/{mid}").json()["versions"]}
    check("a version can sit in STAGING while another serves production, without disturbing it", stages[3] == "staging" and stages[2] == "production")
    check("a failed version can't be promoted (409); bad stage names → 400", TA.call("POST", f"/api/ml/versions/{failed['id']}/promote", json={"stage": "production"}).status_code == 409
          and TA.call("POST", f"/api/ml/versions/{vs[1]}/promote", json={"stage": "yolo"}).status_code == 400)
    check("a standard user can't promote (403)", api("POST", f"/api/ml/versions/{vs[1]}/promote", CTX["standard"], json={"stage": "production"}).status_code == 403)
    check("a model with a version in production can't be deleted (409)", TA.call("DELETE", f"/api/ml/models/{mid}").status_code == 409)
    actions = {e["action"] for e in TA.call("GET", "/api/audit-log").json()}
    check("dataset, model, training and promotion events are audited", {"ml.dataset_created", "ml.model_created", "ml.training_started", "ml.version_promoted"} <= actions)


# ===========================================================================
@section("4. Serving: validated inputs, logged predictions, sensible answers")
def sec_serving():
    mid = CTX["m"]
    hi = {"tenure": 2, "monthly_spend": 60, "support_tickets": 5, "plan": "basic", "region": "emea"}
    lo = {"tenure": 90, "monthly_spend": 120, "support_tickets": 0, "plan": "enterprise", "region": "amer"}
    r = predict(TA, mid, [hi, lo])
    body = r.json()
    check("a batch prediction returns a label, a probability and per-class probabilities for each row, plus the version and stage", r.status_code == 200 and body["version"] == 2 and body["stage"] == "production"
          and len(body["predictions"]) == 2 and all(abs(sum(p["probabilities"].values()) - 1) < 1e-6 and p["label"] in ("yes", "no") for p in body["predictions"]), r.text[:300])
    check("it is SENSIBLE: a 2-month, 5-ticket, basic-plan customer is predicted to churn; a 90-month, ticket-free enterprise customer is not", body["predictions"][0]["label"] == "yes" and body["predictions"][0]["probability"] > 0.7
          and body["predictions"][1]["label"] == "no" and body["predictions"][1]["probability"] > 0.7, str(body["predictions"]))
    check("it is deterministic: the same input scores the same", predict(TA, mid, hi).json()["predictions"][0]["probabilities"] == body["predictions"][0]["probabilities"])
    first_id = body["predictions"][0]["prediction_id"]
    logged = v.db_all("select version_id, inputs, output from ml_predictions where id = ?", (first_id,))[0]
    check("every prediction is logged with the version, the inputs as the model saw them, and the output", logged[0] == CTX["versions"][2] and json.loads(logged[1])["tenure"] == 2.0 and json.loads(logged[2])["label"] == "yes")
    w = predict(TA, mid, {"tenure": 10, "plan": "basic", "customer_id": "c1", "favourite_colour": "teal"}).json()["warnings"]
    check("missing features (spend, tickets, region) are imputed — and the caller is TOLD; extra columns are ignored — and the caller is told", any("imputed" in x and "monthly_spend" in x for x in w) and any("Ignored" in x and "favourite_colour" in x for x in w), str(w))
    check("a non-numeric value in a numeric feature is treated as missing, with a warning", any("non-numeric" in x for x in predict(TA, mid, {"tenure": "lots", "plan": "pro"}).json()["warnings"]))
    check("a category never seen in training ('platinum') doesn't crash the model", predict(TA, mid, {"tenure": 12, "monthly_spend": 50, "support_tickets": 1, "plan": "platinum", "region": "emea"}).status_code == 200)
    check("input validation: no rows → 400; over 1,000 rows → 413; a non-object row → 400", predict(TA, mid, []).status_code == 400 and predict(TA, mid, [hi] * 1001).status_code == 413 and predict(TA, mid, [5]).status_code == 400)
    check("the staging stage serves the staged version (3), an explicit version can be addressed (4), and one that doesn't exist → 404",
          predict(TA, mid, hi, stage="staging").json()["version"] == 3 and predict(TA, mid, hi, version=4).json()["version"] == 4 and predict(TA, mid, hi, version=99).status_code == 404)
    check("a standard user CAN score (business users shouldn't need a manager to use a model)", predict(TA, mid, hi, token=CTX["standard"]).status_code == 200)
    price_version = TA.call("GET", f"/api/ml/models/{CTX['pm']}").json()["versions"][0]["id"]
    TA.call("POST", f"/api/ml/versions/{price_version}/promote", json={"stage": "production"})
    value = predict(TA, CTX["pm"], {"sqft": 2000, "rooms": 4, "age": 10, "city": "beta"}).json()["predictions"][0]["value"]
    truth = 50 * 2000 + 10_000 * 4 - 800 * 10 + 30_000
    check("a regression model's prediction is close to the generating formula (within 5%)", abs(value - truth) / truth < 0.05, f"{value:.0f} vs {truth}")


# ===========================================================================
@section("5. Monitoring: drift and degradation are DETECTED, not just computed")
def sec_monitor():
    mid = CTX["m"]
    mon = lambda **p: TA.call("GET", f"/api/ml/models/{mid}/monitor", params=p).json()
    # use a version nothing has been scored with yet, so counts are exact
    nv = train(TA, mid, CTX["churn_ds"], algorithm="logistic")
    TA.call("POST", f"/api/ml/versions/{nv['id']}/promote", json={"stage": "production"})
    CTX["mon_version"] = nv
    check("before any traffic: drift and performance both say 'insufficient_data' (it won't invent a verdict from nothing)", mon()["drift"]["status"] == "insufficient_data" and mon()["performance"]["status"] == "insufficient_data")

    d = churn(300, 77)
    rows = churn_rows(d)
    ids = [p["prediction_id"] for p in predict(TA, mid, rows).json()["predictions"]]
    m1 = mon(window=300)
    check("300 in-distribution rows: drift is 'ok' and EVERY feature's PSI is under the 0.10 'stable' line", m1["drift"]["status"] == "ok" and all(f["psi"] < 0.10 for f in m1["drift"]["features"].values()), json.dumps(m1["drift"])[:400])
    check("…and with no ground truth yet, performance honestly says 'insufficient_data' (labelled_rows 0)", m1["performance"]["status"] == "insufficient_data" and m1["performance"]["labelled_rows"] == 0)
    for pid, truth in zip(ids, d["churned"]):
        TA.call("POST", f"/api/ml/predictions/{pid}/outcome", json={"actual": "yes" if truth else "no"})
    m2 = mon(window=300)
    perf = m2["performance"]
    check("once the TRUE outcomes arrive, live accuracy is computed and sits near the holdout (no degradation)", perf["status"] == "ok" and abs(perf["accuracy_drop"]) < 0.10 and perf["labelled_rows"] == 300, json.dumps(perf)[:300])

    d2 = churn(100, 78)
    ids2 = [p["prediction_id"] for p in predict(TA, mid, churn_rows(d2)).json()["predictions"]]
    for pid, truth in zip(ids2, d2["churned"]):
        TA.call("POST", f"/api/ml/predictions/{pid}/outcome", json={"actual": "no" if truth else "yes"})  # the world stopped behaving like the training data
    m3 = mon(window=100)
    check("DEGRADATION: when outcomes contradict the predictions the monitor flags it ('degraded', large accuracy drop) and raises an alert",
          m3["performance"]["status"] == "degraded" and m3["performance"]["accuracy_drop"] > 0.3 and any("performance" in a for a in m3["alerts"]), json.dumps(m3["performance"])[:300])

    ds = churn(300, 79, shift=True)
    predict(TA, mid, churn_rows(ds))
    m4 = mon(window=300)
    dr = m4["drift"]
    check("DRIFT: a shifted population (tenure +60 months, everyone on the enterprise plan, 3× the tickets) → status 'alert'", dr["status"] == "alert", json.dumps(dr)[:400])
    check("…naming the features that moved: tenure, plan, and the ticket count", {"tenure", "plan", "support_tickets"} <= set(dr["drifted_features"]), str(dr["drifted_features"]))
    check("…while features that did NOT shift (region, spend) stay quiet", dr["features"]["region"]["status"] == "ok" and dr["features"]["monthly_spend"]["status"] == "ok", str({k: v["psi"] for k, v in dr["features"].items()}))
    check("…and the model's OUTPUTS drifted too (everyone is now predicted to stay), with human-readable alerts",
          dr["prediction_drift"]["status"] == "alert" and any("tenure" in a for a in m4["alerts"]) and any("predictions have shifted" in a for a in m4["alerts"]), str(m4["alerts"]))
    check("the monitor window is honoured (the last 300 rows, or the last 50)", mon(window=300)["window"] == 300 and mon(window=50)["window"] == 50)
    check("outcomes can't be recorded by a standard user (403) or on another workspace's prediction (404)",
          api("POST", f"/api/ml/predictions/{ids[0]}/outcome", CTX["standard"], json={"actual": "yes"}).status_code == 403 and TB.call("POST", f"/api/ml/predictions/{ids[0]}/outcome", json={"actual": "yes"}).status_code == 404)


# ===========================================================================
@section("6. A tampered model artifact can never execute code; workspaces are isolated")
def sec_security():
    mid = CTX["m"]
    clean = train(TA, mid, CTX["churn_ds"], algorithm="logistic")
    TA.call("POST", f"/api/ml/versions/{clean['id']}/promote", json={"stage": "staging"})
    check("control: a freshly trained version scores normally from staging", predict(TA, mid, {"tenure": 5, "plan": "basic"}, stage="staging").status_code == 200)

    victim = train(TA, mid, CTX["churn_ds"], algorithm="logistic")
    blob = bytearray(db_one("select artifact from ml_model_versions where id = ?", (victim["id"],)))
    blob[len(blob) // 2] ^= 0x01
    db_exec("update ml_model_versions set artifact = ? where id = ?", (bytes(blob), victim["id"]))
    r = predict(TA, mid, {"tenure": 5, "plan": "basic"}, version=victim["version"])
    check("ONE FLIPPED BYTE in a stored artifact: loading is refused (409 'failed its integrity check')", r.status_code == 409 and "integrity check" in r.text, f"{r.status_code} {r.text[:200]}")

    class Evil:
        def __reduce__(self):
            return (os.system, (f"touch {MARKER}",))

    evil = train(TA, mid, CTX["churn_ds"], algorithm="logistic")
    db_exec("update ml_model_versions set artifact = ? where id = ?", (pickle.dumps(Evil()), evil["id"]))
    r = predict(TA, mid, {"tenure": 5, "plan": "basic"}, version=evil["version"])
    time.sleep(0.5)
    check("CODE EXECUTION ATTEMPT: a malicious pickle (whose __reduce__ runs `touch <marker>`) swapped in with the old signature is REFUSED…", r.status_code == 409 and "integrity check" in r.text)
    check("…and the marker file was never created — the signature is verified BEFORE anything is unpickled", not MARKER.exists(), f"marker exists: {MARKER.exists()}")
    db_exec("update ml_model_versions set artifact = (select artifact from ml_model_versions where id = ?), artifact_sig = (select artifact_sig from ml_model_versions where id = ?) where id = ?", (clean["id"], clean["id"], evil["id"]))
    check("a signature is bound to its tenant AND version id: copying a perfectly valid artifact + signature onto a different version fails too",
          predict(TA, mid, {"tenure": 5, "plan": "basic"}, version=evil["version"]).status_code == 409)

    b_model = TB.call("POST", "/api/ml/models", json={"name": "churn-model", "task": "classification", "target": "churned"})
    check("workspace B can create a model with the same NAME (names are per workspace)", b_model.status_code == 200)
    check("workspace B can't read, predict, monitor, promote, train or delete A's model/version (404 each)",
          all(TB.call(m, p, **kw).status_code == 404 for m, p, kw in (("GET", f"/api/ml/models/{mid}", {}), ("POST", f"/api/ml/models/{mid}/predict", {"json": {"inputs": {"tenure": 1}}}), ("GET", f"/api/ml/models/{mid}/monitor", {}),
                                                                      ("POST", f"/api/ml/versions/{clean['id']}/promote", {"json": {"stage": "production"}}), ("GET", f"/api/ml/versions/{clean['id']}", {}), ("DELETE", f"/api/ml/models/{mid}", {}))))
    check("…and B's model list contains only B's", [m["name"] for m in TB.call("GET", "/api/ml/models").json()] == ["churn-model"] and TB.call("GET", "/api/ml/models").json()[0]["id"] != mid)


# ===========================================================================
@section("7. Forecasting for dashboards: trend, seasonality, widening intervals")
def sec_forecast():
    rng = np.random.default_rng(3)
    truth = lambda i: 1000 + 40 * i + 150 * math.sin(2 * math.pi * (i % 12) / 12)
    pts = [{"date": f"{2022 + i // 12}-{i % 12 + 1:02d}-15", "value": truth(i) + float(rng.normal(0, 20))} for i in range(30)]
    r = TA.call("POST", "/api/ml/forecast", json={"points": pts, "horizon": 6})
    f = r.json()
    check("a 30-month series with trend and yearly seasonality: the method used is 'linear trend + monthly seasonality', fitted well (R² > 0.9)", r.status_code == 200 and "seasonality" in f["method"] and f["r2"] > 0.9, r.text[:200])
    check("the recovered trend is close to the true +40 per month", 34 <= f["slope_per_month"] <= 46, str(f["slope_per_month"]))
    mape = np.mean([abs(p["forecast"] - truth(30 + i)) / truth(30 + i) for i, p in enumerate(f["forecast"])])
    check("the six forecast months are within 8% of the true (noise-free) values on average", mape < 0.08, f"MAPE {mape:.3f}")
    widths = [p["high_95"] - p["low_95"] for p in f["forecast"]]
    check("prediction intervals WIDEN with the horizon (uncertainty grows), the 95% band contains the 80% band, and each forecast sits inside",
          all(b >= a for a, b in zip(widths, widths[1:])) and all(p["low_95"] <= p["low_80"] <= p["forecast"] <= p["high_80"] <= p["high_95"] for p in f["forecast"]))
    short = [{"date": f"2023-{m:02d}-01", "value": 100 + 10 * m} for m in range(1, 9)]
    check("with under 24 months it falls back to a plain linear trend", "seasonality" not in TA.call("POST", "/api/ml/forecast", json={"points": short, "horizon": 3}).json()["method"])
    split = short[:3] + [{"date": "2023-04-02", "value": 70}, {"date": "2023-04-20", "value": 70}] + short[4:]
    merged = short[:3] + [{"date": "2023-04-02", "value": 140}] + short[4:]
    check("several points in one month are summed into that month before fitting", TA.call("POST", "/api/ml/forecast", json={"points": split, "horizon": 2}).json()["forecast"] == TA.call("POST", "/api/ml/forecast", json={"points": merged, "horizon": 2}).json()["forecast"])
    check("validation: under 6 months → 400; an unparseable date → 400; horizon 0 or 25 → 400",
          TA.call("POST", "/api/ml/forecast", json={"points": short[:4], "horizon": 3}).status_code == 400 and TA.call("POST", "/api/ml/forecast", json={"points": [{"date": "soon", "value": 1}] * 8, "horizon": 3}).status_code == 400
          and TA.call("POST", "/api/ml/forecast", json={"points": short, "horizon": 0}).status_code == 400 and TA.call("POST", "/api/ml/forecast", json={"points": short, "horizon": 25}).status_code == 400)
    check("a standard user can forecast (it's a dashboard feature); a workspace with the flag off cannot", api("POST", "/api/ml/forecast", CTX["standard"], json={"points": short, "horizon": 2}).status_code == 200 and TF.call("POST", "/api/ml/forecast", json={"points": short, "horizon": 2}).status_code == 403)


def setup():
    global TA, TB, TF
    v.start_redis()
    v.start_slate(env={"ML_WORKERS": "2"})
    TA = Tenant("Verify ML A", f"admin-a-{secrets.token_hex(3)}@example.com")
    TB = Tenant("Verify ML B", f"admin-b-{secrets.token_hex(3)}@example.com")
    TF = Tenant("Verify ML Off", f"admin-f-{secrets.token_hex(3)}@example.com")
    for t in (TA, TB):
        t.set_flag("ml_lifecycle", True)


if __name__ == "__main__":
    code = v.run("ML lifecycle: train, register, serve, monitor", setup)
    if MARKER.exists():
        MARKER.unlink()
    sys.exit(code)
