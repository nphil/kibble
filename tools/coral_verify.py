"""Compares the histogram recognizer (`identity.Model`) against the Coral centroid recognizer
(`coral_identity.CoralModel`) on a local copy of a live Kibble entry's own storage root, and
reports the go/no-go table docs/41-coral-recognition.md's "Calibration" section is built from.

Also the tool that produced `coral_identity.DECISION_TOP_THRESHOLD`/`DECISION_MARGIN`: rerun this
whenever training data has grown meaningfully (more not-a-cat body photos, a newly enrolled cat,
a lot more auto-learned samples) to see whether those two numbers should move.

Usage
-----
1. Get a LOCAL copy of the entry's storage root (never edits it -- opens `kibble.db` through the
   real `_SyncStore`, which runs its own idempotent migration on open, same as production):

     ssh <ha-host> 'sudo -n docker cp homeassistant:/config/kibble/<entry_id> -' \\
       | tar -x -C /path/to/local/copy

   (or an equivalent rsync/scp -- anything that reproduces `kibble.db`, `media/`, `training/`
   under one local directory).

2. Have a reachable CoralHub and, if it has a token configured, the token in a file:

     python3 tools/coral_verify.py --root /path/to/local/copy \\
       --coralhub-url http://192.168.1.69:8720 --coralhub-token-file /path/to/token

Ground truth (mirrors the 2026-09-25 benchmark's own definition, coral-bench.md section 1):
`training` rows with `source in (label, import)`, plus `samples` rows whose own `review` is a
real label (set, and not `skip`) or whose event is `reviewed=1` with `identity_status` not
`unknown`. Crop trustworthiness uses the DEPLOYED `crop_geometry.is_legacy_crop_trustworthy`
exactly (kept as-is, not the benchmark's own separate/stricter "clean" bucketing) -- see that
function's own docstring for why a box-less sample is trusted, not excluded.

Fold assignment is a custom greedy stratified-group k-fold (this repo's own dependency group has
no scikit-learn): groups (one per event, or one per standalone `import` row) are assigned,
largest first, to whichever fold currently has the fewest rows of that group's majority label,
tie-broken by total fold weight. Never sklearn-identical, but the property that matters --
one group's photos never split across train/test, and folds are reasonably label-balanced --
holds by construction.

Coral embeddings are fetched fresh from the real CoralHub server every run (this tool has no
dependency on `store.py`'s own embedding cache, which is production's concern, not an
evaluation harness's) -- every image is embedded exactly once per run and cached in memory only
for the run's own duration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KIBBLE_DIR = _REPO_ROOT / "custom_components" / "kibble"
if "kibble" not in sys.modules:
    import types

    _stub = types.ModuleType("kibble")
    _stub.__path__ = [str(_KIBBLE_DIR)]
    sys.modules["kibble"] = _stub
from kibble import crop_geometry, identity  # noqa: E402
from kibble.coral_identity import CoralFeatures, CoralModel  # noqa: E402

K_FOLDS = 5
FOLD_SEED = 20260926
EMBED_BATCH_MAX = 32
NOT_A_CAT = identity.NOT_A_CAT
CLASSES = ["Kitty", "Pancake", NOT_A_CAT]


# --- ground truth: reuses the real, deployed store/crop_geometry logic, never a re-derivation ----


def _build_ground_truth(db_path: Path) -> list[dict[str, Any]]:
    """One dict per (row, crop_kind) with `group_key`/`label`/`mode`/`jpeg` -- reads
    already-computed `body_feat`/`face_feat`/`face_emb` BLOBs directly (byte-for-byte what the
    deployed `identity.Model` already has cached, not a re-derivation) for the baseline, and
    raw JPEG bytes for the Coral arm's own embedding calls."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def crop_trustworthy(box: tuple[float, ...] | None, t: int) -> bool:
        return crop_geometry.is_legacy_crop_trustworthy(box, t)

    def training_row_crop_trustworthy(training_uid: str) -> bool:
        sample_uid = training_uid.removesuffix("-train")
        row = conn.execute(
            "SELECT t, box_x1, box_y1, box_x2, box_y2 FROM samples WHERE uid=?", (sample_uid,)
        ).fetchone()
        if row is None:
            return True
        box = (row["box_x1"], row["box_y1"], row["box_x2"], row["box_y2"]) if row["box_x1"] is not None else None
        return crop_trustworthy(box, row["t"])

    rows: list[dict[str, Any]] = []

    for r in conn.execute(
        "SELECT uid, cat, source, body, face, body_feat, face_feat, face_emb, mode "
        "FROM training WHERE source IN ('label','import')"
    ):
        if r["source"] == "label":
            if not training_row_crop_trustworthy(r["uid"]):
                continue
            sample_uid = r["uid"].removesuffix("-train")
            srow = conn.execute("SELECT event_uid FROM samples WHERE uid=?", (sample_uid,)).fetchone()
            group_key = srow["event_uid"] if srow is not None else r["uid"]
        else:  # import
            group_key = f"import:{r['uid']}"
        rows.append({
            "row_uid": r["uid"], "group_key": group_key, "label": r["cat"], "mode": r["mode"],
            "body_feat": identity.unpack(r["body_feat"]), "face_feat": identity.unpack(r["face_feat"]),
            "face_emb": identity.unpack(r["face_emb"]),
            "body": r["body"], "face": r["face"],
        })

    for r in conn.execute(
        """
        SELECT s.uid AS uid, s.event_uid AS event_uid, s.t AS t, s.body AS body, s.face AS face,
               s.review AS review, s.box_x1 AS box_x1, s.box_y1 AS box_y1, s.box_x2 AS box_x2,
               s.box_y2 AS box_y2, s.body_feat AS body_feat, s.face_feat AS face_feat,
               s.face_emb AS face_emb, s.mode AS mode,
               e.reviewed AS reviewed, e.cat AS event_cat, e.identity_status AS event_identity_status
        FROM samples s JOIN events e ON e.uid = s.event_uid
        WHERE (s.review IS NOT NULL AND s.review != 'skip')
           OR (e.reviewed = 1 AND e.identity_status != 'unknown')
        """
    ):
        if r["review"] == "skip":
            continue
        if r["review"] is not None:
            label = r["review"]
        elif r["event_identity_status"] == NOT_A_CAT:
            label = NOT_A_CAT
        elif r["event_identity_status"] == "reviewed" and r["event_cat"]:
            label = r["event_cat"]
        else:
            continue
        box = (r["box_x1"], r["box_y1"], r["box_x2"], r["box_y2"]) if r["box_x1"] is not None else None
        if not crop_trustworthy(box, r["t"]):
            continue
        rows.append({
            "row_uid": r["uid"], "group_key": r["event_uid"], "label": label, "mode": r["mode"],
            "body_feat": identity.unpack(r["body_feat"]), "face_feat": identity.unpack(r["face_feat"]),
            "face_emb": identity.unpack(r["face_emb"]),
            "body": r["body"], "face": r["face"],
        })

    conn.close()
    return rows


def _resolve_asset(root: Path, asset_id: str | None) -> bytes | None:
    if not asset_id:
        return None
    relative = asset_id if asset_id.startswith(("training/", "avatars/")) else f"media/{asset_id}"
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


# --- fold assignment: greedy stratified group k-fold (no sklearn dependency) --------------------


def _assign_folds(rows: list[dict[str, Any]], k: int, seed: int) -> None:
    group_labels: dict[str, Counter] = {}
    group_weight: Counter = Counter()
    for r in rows:
        group_labels.setdefault(r["group_key"], Counter())[r["label"]] += 1
        group_weight[r["group_key"]] += 1
    majority = {g: c.most_common(1)[0][0] for g, c in group_labels.items()}

    keys = sorted(majority.keys(), key=lambda g: -group_weight[g])
    rng = random.Random(seed)
    rng.shuffle(keys)
    keys.sort(key=lambda g: -group_weight[g])  # stable re-sort keeps the shuffle as a tie-break only

    fold_label_counts = [Counter() for _ in range(k)]
    fold_weight = [0] * k
    fold_of: dict[str, int] = {}
    for key in keys:
        label = majority[key]
        best = min(range(k), key=lambda f: (fold_label_counts[f][label], fold_weight[f], f))
        fold_of[key] = best
        fold_label_counts[best][label] += 1
        fold_weight[best] += group_weight[key]

    for r in rows:
        r["fold"] = fold_of[r["group_key"]]


# --- CoralHub embedding (fresh every run; no dependency on store.py's own cache) -----------------


async def _embed_all(
    base_url: str, token: str, rows: list[dict[str, Any]], root: Path, crop_kind: str, model_id: str
) -> dict[str, np.ndarray]:
    import aiohttp

    targets = []
    for r in rows:
        jpeg = _resolve_asset(root, r[crop_kind])
        if jpeg is not None:
            targets.append((r["row_uid"], jpeg))
    out: dict[str, np.ndarray] = {}
    headers = {"X-Client": "kibble"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(targets), EMBED_BATCH_MAX):
            batch = targets[i : i + EMBED_BATCH_MAX]
            payload = {"model": model_id, "images": [base64.b64encode(j).decode() for _, j in batch]}
            async with session.post(
                f"{base_url.rstrip('/')}/api/v1/embed", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            for (row_uid, _), vec in zip(batch, data["embeddings"], strict=True):
                out[row_uid] = np.asarray(vec, dtype=np.float32)
    return out


# --- evaluation: baseline (identity.Model) and Coral (CoralModel), event-grouped 5-fold CV -------


def _raw_prediction(modality_log_probs: list[np.ndarray], classes: list[str]) -> dict[str, Any] | None:
    if not modality_log_probs or not classes:
        return None
    fused = identity.fuse_log_probs(modality_log_probs)
    order = np.argsort(fused)[::-1]
    top_idx = int(order[0])
    top_p = float(fused[top_idx])
    second_p = float(fused[order[1]]) if len(order) > 1 else 0.0
    return {"label": classes[top_idx], "confidence": top_p, "margin": top_p - second_p}


def _run_baseline_fused(rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    preds = []
    for test_fold in range(k):
        train = [r for r in rows if r["fold"] != test_fold]
        test = [r for r in rows if r["fold"] == test_fold]
        if not test:
            continue
        pairs = [
            (r["label"], identity.Features(face_emb=r["face_emb"], body_feat=r["body_feat"],
                                            face_feat=r["face_feat"], mode=r["mode"]))
            for r in train
        ]
        model = identity.Model(pairs)
        for r in test:
            feat = identity.Features(face_emb=r["face_emb"], body_feat=r["body_feat"],
                                      face_feat=r["face_feat"], mode=r["mode"])
            res = _raw_prediction(model._modality_log_probs(feat), model.classes)
            preds.append(_pred_row(r, res, test_fold))
    return preds


def _run_coral_fused(rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    preds = []
    for test_fold in range(k):
        train = [r for r in rows if r["fold"] != test_fold]
        test = [r for r in rows if r["fold"] == test_fold]
        if not test:
            continue
        pairs = [
            (r["label"], CoralFeatures(body_emb=r.get("body_emb"), face_emb=r.get("face_emb_coral"), mode=r["mode"]))
            for r in train
        ]
        model = CoralModel(pairs)
        for r in test:
            feat = CoralFeatures(body_emb=r.get("body_emb"), face_emb=r.get("face_emb_coral"), mode=r["mode"])
            res = _raw_prediction(model._modality_log_probs(feat), model.classes)
            preds.append(_pred_row(r, res, test_fold))
    return preds


def _pred_row(r: dict[str, Any], res: dict[str, Any] | None, fold: int) -> dict[str, Any]:
    return {
        "row_uid": r["row_uid"], "label_true": r["label"], "mode": r["mode"], "fold": fold,
        "label_pred": res["label"] if res else None,
        "confidence": res["confidence"] if res else None,
        "margin": res["margin"] if res else None,
    }


# --- reporting -------------------------------------------------------------------------------


def _kp_accuracy(preds: list[dict[str, Any]]) -> tuple[float | None, int]:
    rel = [r for r in preds if r["label_true"] in ("Kitty", "Pancake")]
    if not rel:
        return None, 0
    correct = sum(1 for r in rel if r["label_pred"] == r["label_true"])
    return correct / len(rel), len(rel)


def _not_a_cat_pr(preds: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in preds if r["label_true"] == NOT_A_CAT and r["label_pred"] == NOT_A_CAT)
    fp = sum(1 for r in preds if r["label_true"] != NOT_A_CAT and r["label_pred"] == NOT_A_CAT)
    fn = sum(1 for r in preds if r["label_true"] == NOT_A_CAT and r["label_pred"] != NOT_A_CAT)
    return {
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "n": tp + fn,
    }


def _joint_gate_sweep(preds: list[dict[str, Any]], target: float = 0.95) -> dict[str, Any] | None:
    """Coverage-maximising (threshold, margin) pair clearing `target` precision -- the joint
    version of the benchmark's own `threshold_for_precision`, needed because production's gate
    checks BOTH `top_p` and `margin`, not confidence alone. When `target` is unreachable at any
    pair, reports the best precision actually achievable (at the strictest scored threshold/
    margin) instead, `reachable=False` -- transparency over a bare "no", matching the
    benchmark's own `threshold_for_precision` fallback."""
    scored = [r for r in preds if r["confidence"] is not None and r["label_pred"] is not None]
    if not scored:
        return None
    total = len(preds)
    best = None
    fallback = None
    for t in sorted({r["confidence"] for r in scored}):
        for m in sorted({round(r["margin"], 4) for r in scored}):
            answered = [r for r in scored if r["confidence"] >= t and r["margin"] >= m]
            if len(answered) < 5:
                continue
            correct = sum(1 for r in answered if r["label_pred"] == r["label_true"])
            precision = correct / len(answered)
            coverage = len(answered) / total
            if precision >= target and (best is None or coverage > best["coverage"]):
                best = {"threshold": t, "margin": m, "precision": precision, "coverage": coverage, "reachable": True}
            if fallback is None or precision > fallback["precision"]:
                fallback = {"threshold": t, "margin": m, "precision": precision, "coverage": coverage, "reachable": False}
    return best if best is not None else fallback


def _split_by_mode(preds: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {"day": [r for r in preds if r["mode"] == "day"], "ir": [r for r in preds if r["mode"] == "ir"], "all": preds}


def _print_report(name: str, preds: list[dict[str, Any]]) -> None:
    print(f"\n=== {name} (n={len(preds)}) ===")
    for mode_name, subset in _split_by_mode(preds).items():
        acc, n = _kp_accuracy(subset)
        print(f"  Kitty-vs-Pancake accuracy [{mode_name:>3s}]: "
              f"{'n/a' if acc is None else f'{acc:.3f}'} (n={n})")
    nac = _not_a_cat_pr(preds)
    p = "n/a" if nac["precision"] is None else f"{nac['precision']:.3f}"
    r = "n/a" if nac["recall"] is None else f"{nac['recall']:.3f}"
    print(f"  not_a_cat precision/recall: {p} / {r}  (n={nac['n']})")
    gate = _joint_gate_sweep(preds)
    if gate is None:
        print("  95%-precision gate: no scored predictions at all")
    elif gate["reachable"]:
        print(f"  95%-precision gate: threshold={gate['threshold']:.4f} margin={gate['margin']:.4f} "
              f"precision={gate['precision']:.3f} coverage={gate['coverage']:.3f}")
    else:
        print(f"  95%-precision gate: UNREACHABLE at any threshold; best achievable is "
              f"precision={gate['precision']:.3f} at coverage={gate['coverage']:.3f} "
              f"(threshold={gate['threshold']:.4f} margin={gate['margin']:.4f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="local copy of the entry's storage root")
    parser.add_argument("--coralhub-url", required=True)
    parser.add_argument("--coralhub-token-file", type=Path, default=None)
    parser.add_argument("--body-model", default="mobilenet_v1_1.0_224_l2norm_quant_edgetpu")
    parser.add_argument("--face-model", default="mobilenet_v1_1.0_224_quant_embedding_extractor_edgetpu")
    args = parser.parse_args()

    token = args.coralhub_token_file.read_text().strip() if args.coralhub_token_file else ""

    t0 = time.time()
    rows = _build_ground_truth(args.root / "kibble.db")
    rows = [r for r in rows if r["body_feat"] is not None or r["face_feat"] is not None]
    print(f"ground truth: {len(rows)} rows  by label: {dict(Counter(r['label'] for r in rows))}"
          f"  ({time.time() - t0:.1f}s)")

    _assign_folds(rows, K_FOLDS, FOLD_SEED)

    t0 = time.time()
    body_embeddings = asyncio.run(
        _embed_all(args.coralhub_url, token, rows, args.root, "body", args.body_model)
    )
    face_embeddings = asyncio.run(
        _embed_all(args.coralhub_url, token, rows, args.root, "face", args.face_model)
    )
    for r in rows:
        r["body_emb"] = body_embeddings.get(r["row_uid"])
        r["face_emb_coral"] = face_embeddings.get(r["row_uid"])
    print(f"CoralHub embeddings: {len(body_embeddings)} body, {len(face_embeddings)} face "
          f"({time.time() - t0:.1f}s)")

    baseline_preds = _run_baseline_fused(rows, K_FOLDS)
    coral_preds = _run_coral_fused(rows, K_FOLDS)

    _print_report("baseline (identity.Model, fused)", baseline_preds)
    _print_report("coral (CoralModel, fused)", coral_preds)


if __name__ == "__main__":
    main()
