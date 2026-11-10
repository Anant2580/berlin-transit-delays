-- Migration 008: results of the delay model (written by the Airflow DAG weekly_delay_model).
-- Safe to run more than once. Applied by deploy/update.sh.

-- One row per training run
CREATE TABLE IF NOT EXISTS ml_model_runs (
    run_id          SERIAL       PRIMARY KEY,
    trained_at      TIMESTAMPTZ  NOT NULL,
    train_from      TIMESTAMPTZ  NOT NULL,
    test_from       TIMESTAMPTZ  NOT NULL,    -- the test week is never seen during training
    test_to         TIMESTAMPTZ  NOT NULL,
    n_train         INTEGER      NOT NULL,
    n_test          INTEGER      NOT NULL,
    late_rate_test  NUMERIC(6, 4) NOT NULL,   -- share of test departures that were > 3 min late
    best_iteration  INTEGER      NOT NULL     -- number of trees after early stopping
);

-- Test-week metrics for the model and the baselines it is compared with
CREATE TABLE IF NOT EXISTS ml_model_metrics (
    run_id           INTEGER  NOT NULL REFERENCES ml_model_runs ON DELETE CASCADE,
    model            TEXT     NOT NULL,       -- base_rate, history_line_hour, lightgbm_schedule, lightgbm_full
    roc_auc          NUMERIC(6, 4),
    pr_auc           NUMERIC(6, 4),
    brier            NUMERIC(8, 5),
    precision_top10  NUMERIC(6, 4),           -- flag the riskiest 10%: share of them that were late
    recall_top10     NUMERIC(6, 4),           -- ... and share of all late departures caught
    PRIMARY KEY (run_id, model)
);

-- SHAP feature importance of the full model (mean absolute SHAP value on the test week)
CREATE TABLE IF NOT EXISTS ml_feature_importance (
    run_id         INTEGER  NOT NULL REFERENCES ml_model_runs ON DELETE CASCADE,
    rank           INTEGER  NOT NULL,
    feature        TEXT     NOT NULL,
    label          TEXT     NOT NULL,
    mean_abs_shap  NUMERIC(10, 5) NOT NULL,
    PRIMARY KEY (run_id, feature)
);

-- The read-only dashboard user may read the results (if that user exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard') THEN
        GRANT SELECT ON ml_model_runs, ml_model_metrics, ml_feature_importance TO dashboard;
    END IF;
END $$;
