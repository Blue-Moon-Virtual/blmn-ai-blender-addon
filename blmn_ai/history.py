"""Local render history stored as JSON in the output folder.

Each entry records prompt, model, paths and a timestamp so the N-panel can show
recent results. The user's full history also lives in their blmn.ai web library
(results are persisted server-side).
"""
import json
import os
import time

from . import utils

HISTORY_FILE = "blmn_history.json"


def _history_path(output_dir):
    return os.path.join(output_dir, HISTORY_FILE)


def load(output_dir):
    path = _history_path(output_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as exc:
        utils.log("History load failed:", exc)
        return []


def add(output_dir, prompt, model_label, capture_path, result_path):
    entries = load(output_dir)
    entries.insert(0, {
        "time": time.time(),
        "label": _label_from_prompt(prompt, model_label),
        "prompt": prompt,
        "model": model_label,
        "capture_path": capture_path,
        "result_path": result_path,
    })

    try:
        with open(_history_path(output_dir), "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
    except OSError as exc:
        utils.log("History save failed:", exc)
    return entries


def _label_from_prompt(prompt, model_label):
    text = (prompt or "").strip().replace("\n", " ")
    if not text:
        return model_label or "Render"
    return text[:42] + ("…" if len(text) > 42 else "")
