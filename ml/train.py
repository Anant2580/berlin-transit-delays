"""
Train and evaluate the delay model: will a departure be more than 3 minutes late?

Honest evaluation with a time split (no shuffling, the model never sees the future):
  train       everything before the last two weeks
  validation  the second-to-last week (only used to decide when to stop adding trees)
  test        the last full week, never touched during training

The model is compared with two baselines, so the numbers mean something:
  base rate   predict the overall share of late departures for everyone
  history     the late share of the same line at the same hour, from the training weeks

Explanations: LightGBM computes exact TreeSHAP values itself (pred_contrib=True), so the
production code needs no extra library. Mean |SHAP| per feature = how much it moves predictions.

Usage:
  python -m ml.train --csv path/to/departures.csv.gz --out ml/output     # from a CSV export
  python -m ml.train --out /opt/airflow/models --write-db                 # from the database
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from ml.features import (
    ALL_FEATURES,
    FEATURE_LABELS,
    SCHEDULE_FEATURES,
    add_lag_features,
    load_from_db,
    prepare,
    to_model_frame,
)

PARAMS = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 200,     # at least 200 departures per leaf: no rules learned from a handful of trips
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "cat_smooth": 50,            # rare lines are pulled towards the average instead of over-fitted
    "verbose": -1,
    "seed": 42,
}
TOP_SHARE = 0.10                 # "if we flag the riskiest 10% of departures ..."
SHAP_SAMPLE = 50_000             # test rows used for the explanation (exact, but no need for all)


def time_split(df: pd.DataFrame, test_days: int = 7, val_days: int = 7):
    """Split by planned time at Berlin midnights: train | validation | test (the last full days)."""
    last_local = df["planned"].max().tz_convert("Europe/Berlin")
    end = last_local.normalize()                         # midnight starting the (incomplete) last day
    # Calendar days (DateOffset), not 24-hour blocks: across a daylight-saving change a "day" has
    # 23 or 25 hours, and the split must still fall on Berlin midnights
    test_start = end - pd.DateOffset(days=test_days)
    val_start = test_start - pd.DateOffset(days=val_days)
    train = df[df["planned"] < val_start]
    val = df[(df["planned"] >= val_start) & (df["planned"] < test_start)]
    test = df[(df["planned"] >= test_start) & (df["planned"] < end)]
    return train, val, test


def evaluate(y_true, p) -> dict:
    """Ranking quality (ROC-AUC, PR-AUC), probability quality (Brier) and a practical view (top 10%)."""
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    k = max(1, int(len(p) * TOP_SHARE))
    top = np.argsort(-p)[:k]                             # the departures the model finds riskiest
    return {
        "roc_auc": round(float(roc_auc_score(y_true, p)), 4),
        "pr_auc": round(float(average_precision_score(y_true, p)), 4),
        "brier": round(float(brier_score_loss(y_true, p)), 5),
        "precision_top10": round(float(y_true[top].mean()), 4),             # share of flagged ones that were late
        "recall_top10": round(float(y_true[top].sum() / max(1, y_true.sum())), 4),  # share of all late ones caught
    }


def history_baseline(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    """Late share of the same line at the same hour in the training data (falls back to line, then overall)."""
    line_hour = train.groupby(["line_name", "hour"]).late.agg(["mean", "count"])
    line_hour = line_hour[line_hour["count"] >= 20]["mean"]
    line = train.groupby("line_name").late.mean()
    keys = pd.MultiIndex.from_arrays([test["line_name"], test["hour"]])
    p = pd.Series(line_hour.reindex(keys).to_numpy(), index=test.index)
    p = p.fillna(test["line_name"].map(line)).fillna(train["late"].mean())
    return p.to_numpy()


def fit(train_X, train_y, val_X, val_y, features) -> lgb.Booster:
    """Gradient-boosted trees; stop adding trees when the validation week stops improving."""
    return lgb.train(
        PARAMS,
        lgb.Dataset(train_X[features], train_y),
        num_boost_round=2000,
        valid_sets=[lgb.Dataset(val_X[features], val_y)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )


def shap_importance(model: lgb.Booster, X: pd.DataFrame) -> pd.DataFrame:
    """Mean absolute SHAP value per feature (the last column of pred_contrib is the bias term)."""
    sample = X.sample(min(len(X), SHAP_SAMPLE), random_state=42)
    contrib = model.predict(sample[ALL_FEATURES], pred_contrib=True, num_iteration=model.best_iteration)
    mean_abs = np.abs(contrib[:, :-1]).mean(axis=0)
    imp = pd.DataFrame({"feature": ALL_FEATURES, "mean_abs_shap": mean_abs})
    imp["label"] = imp["feature"].map(FEATURE_LABELS)
    imp = imp.sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)
    imp["rank"] = imp.index + 1
    return imp


def run(raw: pd.DataFrame, out_dir: Path) -> dict:
    """The whole pipeline: features, split, baselines, two models, evaluation, SHAP, saved files."""
    df = add_lag_features(prepare(raw))
    train, val, test = time_split(df)
    if min(len(train), len(val), len(test)) == 0:
        raise ValueError("Not enough data for a train / validation / test split (needs about 3 weeks).")

    train_X, categories = to_model_frame(train)
    val_X, _ = to_model_frame(val, categories)
    test_X, _ = to_model_frame(test, categories)

    results = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "train_from": str(train["planned"].min()), "test_from": str(test["planned"].min()),
        "test_to": str(test["planned"].max()),
        "n_train": len(train), "n_val": len(val), "n_test": len(test),
        "late_rate_train": round(float(train["late"].mean()), 4),
        "late_rate_test": round(float(test["late"].mean()), 4),
        "models": {},
    }
    m = results["models"]
    m["base_rate"] = evaluate(test["late"], np.full(len(test), train["late"].mean()))
    m["history_line_hour"] = evaluate(test["late"], history_baseline(train, test))

    schedule_model = fit(train_X, train["late"], val_X, val["late"], SCHEDULE_FEATURES)
    m["lightgbm_schedule"] = evaluate(
        test["late"], schedule_model.predict(test_X[SCHEDULE_FEATURES], num_iteration=schedule_model.best_iteration))

    model = fit(train_X, train["late"], val_X, val["late"], ALL_FEATURES)
    m["lightgbm_full"] = evaluate(
        test["late"], model.predict(test_X[ALL_FEATURES], num_iteration=model.best_iteration))
    results["best_iteration"] = model.best_iteration

    importance = shap_importance(model, test_X)
    results["importance"] = importance[["rank", "feature", "label", "mean_abs_shap"]].round(5).to_dict("records")

    # Save everything needed to reuse the model and to show the results
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "delay_model.txt"), num_iteration=model.best_iteration)
    (out_dir / "categories.json").write_text(json.dumps(categories))
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    return results


def write_to_db(conn, results: dict) -> int:
    """Store one training run: metrics per model and the SHAP feature importance (read by the dashboard)."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ml_model_runs (trained_at, train_from, test_from, test_to, n_train, n_test,
                                          late_rate_test, best_iteration)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id""",
            (results["trained_at"], results["train_from"], results["test_from"], results["test_to"],
             results["n_train"], results["n_test"], results["late_rate_test"], results["best_iteration"]),
        )
        run_id = cur.fetchone()[0]
        cur.executemany(
            """INSERT INTO ml_model_metrics (run_id, model, roc_auc, pr_auc, brier, precision_top10, recall_top10)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(run_id, name, v["roc_auc"], v["pr_auc"], v["brier"], v["precision_top10"], v["recall_top10"])
             for name, v in results["models"].items()],
        )
        cur.executemany(
            """INSERT INTO ml_feature_importance (run_id, rank, feature, label, mean_abs_shap)
               VALUES (%s, %s, %s, %s, %s)""",
            [(run_id, r["rank"], r["feature"], r["label"], r["mean_abs_shap"]) for r in results["importance"]],
        )
    conn.commit()
    return run_id


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="CSV export instead of the database")
    parser.add_argument("--weeks", type=int, default=8, help="how many weeks of history to use (database only)")
    parser.add_argument("--out", default="ml/output", help="folder for the model and results")
    parser.add_argument("--write-db", action="store_true", help="store metrics and importance in PostgreSQL")
    args = parser.parse_args()

    conn = None
    if args.csv:
        raw = pd.read_csv(args.csv)
    else:
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        raw = load_from_db(conn, datetime.now(timezone.utc) - timedelta(weeks=args.weeks))

    results = run(raw, Path(args.out))
    for name, metrics in results["models"].items():
        print(f"{name:20s} {metrics}")
    print("Top features:", ", ".join(r["label"] for r in results["importance"][:5]))

    if args.write_db:
        if conn is None:
            import psycopg
            conn = psycopg.connect(os.environ["DATABASE_URL"])
        print("Saved as run", write_to_db(conn, results))


if __name__ == "__main__":
    main()
