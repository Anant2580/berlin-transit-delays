"""
Charts for the README from a training run's results.json:
  docs/ml_feature_importance.png   what drives late departures (mean |SHAP| per feature)
  docs/ml_model_comparison.png     share of late departures caught when flagging the riskiest 10%

Usage: python -m ml.plots ml/output/results.json docs/
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                     # render to files, no screen needed
import matplotlib.pyplot as plt  # noqa: E402

INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
BLUE, BLUE_LIGHT = "#2563eb", "#bfdbfe"   # one hue: the main model dark, the comparisons light

MODEL_LABELS = {
    "base_rate": "Guess the average",
    "history_line_hour": "History: line × hour",
    "lightgbm_schedule": "Model: timetable only",
    "lightgbm_full": "Model: + recent delays",
}


def _style(ax, title, subtitle):
    ax.set_title(title, loc="left", fontsize=15, color=INK, fontweight="bold", pad=26)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=10.5, color=MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def feature_importance(results: dict, path: Path):
    imp = results["importance"][:10][::-1]          # top 10, largest at the top
    fig, ax = plt.subplots(figsize=(10, 6), dpi=160)
    values = [r["mean_abs_shap"] for r in imp]
    ax.barh([r["label"] for r in imp], values, color=BLUE, height=0.6)
    for y, v in enumerate(values):
        ax.text(v + max(values) * 0.01, y, f"{v:.2f}", va="center", fontsize=9, color=MUTED)
    ax.tick_params(axis="y", labelcolor=INK, labelsize=10.5)
    ax.set_xlabel("Mean |SHAP value| (log-odds): how strongly a feature moves the prediction", color=MUTED)
    _style(ax, "What makes a departure late?",
           f"SHAP explanation of the model on the test week ({results['n_test']:,} departures)")
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def model_comparison(results: dict, path: Path):
    names = ["base_rate", "history_line_hour", "lightgbm_schedule", "lightgbm_full"][::-1]
    recall = [results["models"][n]["recall_top10"] * 100 for n in names]
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=160)
    colors = [BLUE if n == "lightgbm_full" else BLUE_LIGHT for n in names]
    ax.barh([MODEL_LABELS[n] for n in names], recall, color=colors, height=0.6)
    for y, (n, v) in enumerate(zip(names, recall, strict=True)):
        auc = results["models"][n]["roc_auc"]
        ax.text(v + 0.8, y, f"{v:.0f}%   (ROC-AUC {auc:.2f})", va="center", fontsize=9.5, color=INK)
    ax.set_xlim(0, max(recall) * 1.35)
    ax.tick_params(axis="y", labelcolor=INK, labelsize=10.5)
    ax.set_xlabel("Share of all late departures caught (%)", color=MUTED)
    _style(ax, "Flag the riskiest 10% of departures: how many late ones do we catch?",
           f"Test week never seen in training · {results['late_rate_test'] * 100:.1f}% of departures were late")
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    results = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    feature_importance(results, out / "ml_feature_importance.png")
    model_comparison(results, out / "ml_model_comparison.png")
    print("Charts written to", out)
