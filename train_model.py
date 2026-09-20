"""HydroWatch — train the flood classifier.

Two-stage training:
  1. A random-forest *teacher* learns the four hydrological regimes from the
     simulated sensor streams (high accuracy, 300 trees — stays on Linux).
  2. A tiny random-forest *student* (24 shallow trees) is trained two ways:
       - *distilled*: on features + teacher probabilities (richer, but needs
         the teacher at inference — Linux-side only), and
       - *standalone*: on the 8 raw features only (this is the variant that
         gets quantized and exported to the UNO Q's MCU).

Evaluation uses independent scenario draws (never rows from the training
scenarios), the simulation analogue of a time-based split.
"""

from __future__ import annotations

import json
import os

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report, f1_score, confusion_matrix
from joblib import dump

import sensor_sim

MODEL_DIR = "model"
TRAIN_SCENARIOS = 300
TEST_SCENARIOS = 80
SEG = 64


def build_split():
    # Independent RNG streams: test scenarios are fresh draws, not rows of
    # the training scenarios (avoids augmentation leakage).
    Xtr, ytr = sensor_sim.make_dataset(scenarios_per_class=TRAIN_SCENARIOS)
    sensor_sim.RNG = np.random.default_rng(777_001)
    Xte, yte = sensor_sim.make_dataset(scenarios_per_class=TEST_SCENARIOS)
    return (Xtr, ytr), (Xte, yte)


def main() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)
    (Xtr, ytr), (Xte, yte) = build_split()
    print(f"train: {Xtr.shape[0]} rows | test: {Xte.shape[0]} rows "
          f"| features: {Xtr.shape[1]}")

    # ---- Teacher -----------------------------------------------------
    teacher = RandomForestClassifier(
        n_estimators=300, max_depth=None, min_samples_leaf=2,
        class_weight="balanced", n_jobs=-1, random_state=42,
    )
    teacher.fit(Xtr, ytr)
    teacher_pred = teacher.predict(Xte)
    teacher_acc = accuracy_score(yte, teacher_pred)
    print("\n[teacher] test accuracy:", f"{teacher_acc:.4f}")
    print(classification_report(yte, teacher_pred,
                                target_names=sensor_sim.CLASS_NAMES, digits=3))

    # ---- Students ----------------------------------------------------
    ptr = teacher.predict_proba(Xtr)
    pte = teacher.predict_proba(Xte)

    student_distilled = RandomForestClassifier(
        n_estimators=24, max_depth=10, min_samples_leaf=4,
        class_weight="balanced", n_jobs=-1, random_state=7,
    )
    student_distilled.fit(np.hstack([Xtr, ptr]), ytr)
    pred_d = student_distilled.predict(np.hstack([Xte, pte]))
    f1_d = f1_score(yte, pred_d, average="macro")

    student_standalone = RandomForestClassifier(
        n_estimators=24, max_depth=10, min_samples_leaf=4,
        class_weight="balanced", n_jobs=-1, random_state=7,
    )
    student_standalone.fit(Xtr, ytr)
    pred_s = student_standalone.predict(Xte)
    f1_s = f1_score(yte, pred_s, average="macro")

    print(f"[student distilled ]  macro-F1: {f1_d:.4f}  acc: {accuracy_score(yte, pred_d):.4f}")
    print(f"[student standalone]  macro-F1: {f1_s:.4f}  acc: {accuracy_score(yte, pred_s):.4f}")

    chosen_pred, chosen_name = ((pred_d, "distilled") if f1_d > f1_s
                                else (pred_s, "standalone"))
    print(f"\n[best variant: {chosen_name}]")
    print(classification_report(yte, chosen_pred,
                                target_names=sensor_sim.CLASS_NAMES, digits=3))
    print("confusion matrix (rows=true, cols=pred):")
    print(confusion_matrix(yte, chosen_pred))

    dump(teacher, os.path.join(MODEL_DIR, "teacher.joblib"))
    dump(student_distilled, os.path.join(MODEL_DIR, "student_distilled.joblib"))
    dump(student_standalone, os.path.join(MODEL_DIR, "student_standalone.joblib"))

    metrics = {
        "best_variant": chosen_name,
        "teacher_test_accuracy": float(teacher_acc),
        "student_distilled_macro_f1": float(f1_d),
        "student_standalone_macro_f1": float(f1_s),
        "train_rows": int(Xtr.shape[0]),
        "test_rows": int(Xte.shape[0]),
        "feature_order": sensor_sim.FEATURE_ORDER,
        "class_names": sensor_sim.CLASS_NAMES,
    }
    with open(os.path.join(MODEL_DIR, "metrics.json"), "w") as fh:
        json.dump(metrics, fh, indent=2)
    print("saved model/teacher.joblib, student_distilled.joblib, "
          "student_standalone.joblib, metrics.json")


if __name__ == "__main__":
    main()
