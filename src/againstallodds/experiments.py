"""Frozen chronological model comparison and independent forward forecasts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from againstallodds.analytics import metrics, upcoming
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.features import FEATURE_VERSION, feature_dataset
from againstallodds.nfl_data import utcnow
from againstallodds.research_store import ResearchStore, identity

BASELINE = "power-rating-v1"
MODELS = (BASELINE, "ridge-v1", "boosted-v1")
SPEC = {"version": "annual-comparison-v1", "warmup": 2015, "train_start": 2016,
        "validation": [2019, 2020, 2021, 2022, 2023], "test": [2024, 2025], "forecast": 2026,
        "primary": "margin_mae", "seed": 42, "bootstrap_samples": 2000,
        "ridge_alphas": [100, 10, 1], "boosted_leaves": [7, 15], "boosted_l2": [10, 1],
        "boosted_iterations": 200, "boosted_learning_rate": 0.05, "boosted_min_leaf": 30,
        "feature_version": FEATURE_VERSION}


def candidates(model):
    if model == "ridge-v1":
        return [{"alpha": alpha} for alpha in SPEC["ridge_alphas"]]
    return [{"max_leaf_nodes": leaf, "l2_regularization": l2} for leaf in SPEC["boosted_leaves"] for l2 in SPEC["boosted_l2"]]


def pipeline(model, settings):
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    if model == "ridge-v1":
        return Pipeline([("impute", imputer), ("scale", StandardScaler()), ("model", Ridge(**settings))])
    return Pipeline([("impute", imputer), ("model", HistGradientBoostingRegressor(loss="absolute_error", max_iter=200, learning_rate=0.05, min_samples_leaf=30, early_stopping=False, random_state=42, **settings))])


def matrix(rows, names):
    return np.asarray([[np.nan if r["features"][name] is None else r["features"][name] for name in names] for r in rows], dtype=float)


def fold_rows(rows, season):
    train = [r for r in rows if r["eligible"] and r["actual_margin"] is not None and 2016 <= r["season"] < season]
    test = [r for r in rows if r["eligible"] and r["actual_margin"] is not None and r["season"] == season]
    return train, test


def fit_model(model, settings, training, names):
    if not training:
        raise AgainstAllOddsError("No eligible training games for this experiment.")
    fitted = pipeline(model, settings)
    with threadpool_limits(limits=2):
        fitted.fit(matrix(training, names), [r["actual_margin"] for r in training])
    return fitted


def predicted_rows(rows, values, model, stage):
    return [{k: row[k] for k in ("game_id", "season", "week", "gameday", "home_team", "away_team", "actual_margin", "market_margin")} |
            {"model_id": model, "stage": stage, "predicted_margin": float(value), "baseline_margin": row["baseline_margin"]} for row, value in zip(rows, values, strict=True)]


def paired_interval(rows, repetitions=2000):
    if not rows:
        return None
    blocks = {}
    for row in rows:
        key = (row["season"], row["week"])
        difference = abs(row["actual_margin"] - row["predicted_margin"]) - abs(row["actual_margin"] - row["baseline_margin"])
        total, count = blocks.get(key, (0., 0))
        blocks[key] = total + difference, count + 1
    values = np.array(list(blocks.values()))
    rng = np.random.default_rng(42)
    indices = rng.integers(0, len(values), size=(repetitions, len(values)))
    samples = values[indices].sum(axis=1)
    diffs = samples[:, 0] / samples[:, 1]
    return {"delta_mae": float(values[:, 0].sum() / values[:, 1].sum()), "low": float(np.quantile(diffs, .025)), "high": float(np.quantile(diffs, .975)), "blocks": len(values)}


def summarize(rows):
    summary = []
    for stage in ("development", "test"):
        for model in MODELS:
            selected = [r for r in rows if r["stage"] == stage and r["model_id"] == model]
            summary.append({"stage": stage, "model_id": model, **metrics(selected), "vs_baseline": paired_interval(selected) if model != BASELINE else None})
    return summary


def compare_models(store, *, progress=None, now=None):
    now = now or utcnow()
    research = ResearchStore(store)
    stats = research.latest_stats()
    missing = sorted(set(range(2015, 2026)) - set(stats))
    if missing:
        raise AgainstAllOddsError(f"Import historical statistics first. Missing seasons: {missing}")
    fid, rows, manifest = feature_dataset(store, now)
    names = sorted(rows[0]["features"])
    specification = {**SPEC, "sklearn": sklearn.__version__, "feature_names": names}
    eid = identity({"features": fid, "specification": specification})
    cached = research.experiment(eid)
    if cached:
        if progress:
            progress("Using the saved experiment for these exact inputs.")
        return json.loads(cached["result"])
    for season in range(2016, 2026):
        if not any(r["season"] == season and r["eligible"] and r["actual_margin"] is not None for r in rows):
            raise AgainstAllOddsError(f"No eligible completed games in {season}; cannot run the specified experiment.")
    validation, chosen, all_rows, artifacts = {}, {}, [], []
    # Select only with 2019–2023 results. Test seasons are not touched in this loop.
    for model in MODELS[1:]:
        scores = []
        for settings in candidates(model):
            errors, predicted = [], []
            for season in SPEC["validation"]:
                if progress:
                    progress(f"{model}: validating {season}, settings {settings}")
                train, test = fold_rows(rows, season)
                fitted = fit_model(model, settings, train, names)
                with threadpool_limits(limits=2):
                    output = fitted.predict(matrix(test, names))
                errors.extend(abs(np.asarray([r["actual_margin"] for r in test]) - output).tolist())
                predicted.extend(predicted_rows(test, output, model, "development"))
            scores.append({"settings": settings, "mae": float(np.mean(errors)), "games": len(errors), "predictions": predicted})
        # Candidates are ordered by the specified complexity/regularization tie-break.
        best = min(range(len(scores)), key=lambda i: (round(scores[i]["mae"], 12), i))
        chosen[model] = scores[best]["settings"]
        all_rows.extend(scores[best]["predictions"])
        validation[model] = [{k: v for k, v in row.items() if k != "predictions"} for row in scores]
    for stage, seasons in (("development", SPEC["validation"]), ("test", SPEC["test"])):
        for season in seasons:
            _, test = fold_rows(rows, season)
            all_rows.extend(predicted_rows(test, [r["baseline_margin"] for r in test], BASELINE, stage))
    for model in MODELS[1:]:
        for season in [*SPEC["test"], SPEC["forecast"]]:
            if progress:
                progress(f"{model}: fitting frozen settings for {season}")
            train, test = fold_rows(rows, season)
            fitted = fit_model(model, chosen[model], train, names)
            artifacts.append({"model_id": model, "season": season, "pipeline": fitted, "settings": chosen[model]})
            if season in SPEC["test"]:
                with threadpool_limits(limits=2):
                    output = fitted.predict(matrix(test, names))
                all_rows.extend(predicted_rows(test, output, model, "test"))
    ridge = next(a["pipeline"] for a in artifacts if a["model_id"] == "ridge-v1" and a["season"] == 2026)
    coefficients = sorted([{"feature": name, "coefficient": float(value)} for name, value in zip(names, ridge.named_steps["model"].coef_, strict=True)], key=lambda r: abs(r["coefficient"]), reverse=True)
    coverage = []
    for season in range(2016, 2026):
        games = [r for r in rows if r["season"] == season and r["actual_margin"] is not None]
        coverage.append({"season": season, "total": len(games), "eligible": sum(r["eligible"] for r in games), "excluded": sum(not r["eligible"] for r in games)})
    result = {"experiment_id": eid, "feature_id": fid, "manifest": manifest, "settings": chosen,
              "validation": validation, "summary": summarize(all_rows), "coverage": coverage,
              "exclusions": [{"game_id": r["game_id"], "reason": r["exclusion"]} for r in rows if 2016 <= r["season"] <= 2025 and r["actual_margin"] is not None and not r["eligible"]],
              "rows": all_rows, "coefficients": coefficients,
              "seasons": [{"season": season, "model_id": model, **metrics([r for r in all_rows if r["season"] == season and r["model_id"] == model])} for season in [*SPEC["validation"], *SPEC["test"]] for model in MODELS]}
    research.publish(eid, fid, specification, result, artifacts, now)
    if progress:
        progress("Comparison saved. Generating upcoming forecasts for all three models.")
    predict_models(store, now=now, save=True)
    return result


def predict_models(store, *, now=None, save=False, model="all"):
    now = now or utcnow()
    forecasts = upcoming(store, now, save=save and model in {"all", BASELINE})
    baseline_rows = [{**r, "model_id": BASELINE, "available": True} for r in forecasts]
    if model == BASELINE:
        return baseline_rows
    output = baseline_rows if model == "all" else []
    research = ResearchStore(store)
    experiment = research.latest_experiment()
    if not experiment:
        for model_id in MODELS[1:] if model == "all" else [model]:
            output.extend({**r, "model_id": model_id, "predicted_margin": None, "available": False, "exclusion": "Run the model comparison to train this challenger."} for r in forecasts)
        return output
    _, feature_rows, manifest = feature_dataset(store, now, persist=save)
    features = {r["game_id"]: r for r in feature_rows}
    names = json.loads(experiment["specification"])["feature_names"]
    for season in sorted({r["season"] for r in forecasts}):
        artifacts = {a["model_id"]: a for a in research.artifacts(experiment["id"], season)}
        for model_id in MODELS[1:] if model == "all" else [model]:
            artifact = artifacts.get(model_id)
            usable, saved = [], []
            for row in [r for r in forecasts if r["season"] == season]:
                feature = features[row["game_id"]]
                reason = feature["exclusion"] if not feature["eligible"] else ""
                if not artifact:
                    reason = f"No frozen {season} model artifact."
                if reason:
                    output.append({**row, "model_id": model_id, "predicted_margin": None, "available": False, "exclusion": reason})
                else:
                    usable.append((row, feature))
            if not usable:
                continue
            fitted = research.load_model(artifact)
            with threadpool_limits(limits=2):
                values = fitted.predict(matrix([r[1] for r in usable], names))
            for (row, feature), value in zip(usable, values, strict=True):
                prediction = {**row, "predicted_margin": float(value), "model_id": model_id, "available": True, "artifact_id": artifact["id"], "feature_version": FEATURE_VERSION, "feature_manifest": manifest}
                output.append(prediction)
                saved.append(prediction)
            if save:
                config = {"model": model_id, "artifact_id": artifact["id"], "feature_version": FEATURE_VERSION, "manifest": manifest}
                store.save_predictions(manifest["schedule"], config, saved, now)
    return output
