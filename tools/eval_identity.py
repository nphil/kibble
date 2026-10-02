"""Calibrates and reports on the identity engine against real device data.

Extracts the face-crop galleries for Kitty, Pancake and not_a_cat (plus the ungrounded
`pending` queue) from the migration backup tar into a temp dir, builds `identity.Features` for
every sample from its face crop and `.emb` sidecar (no body crops exist in that backup -- this
can only calibrate the face-embedding and face-appearance modalities, not body appearance),
then reports leave-one-out accuracy, a confusion matrix and the unknown rate for three
conditions: face embedding alone, face appearance alone, and the two fused. It also reports how
the fused, fully-trained model would call every pending crop (no ground truth there, just the
resulting distribution).

Run: `uv run python tools/eval_identity.py` (or `python tools/eval_identity.py` inside the
repo's venv). Read-only: the backup tar and /data/home/backups are never written to.
"""

from __future__ import annotations

import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "custom_components" / "kibble"))
import identity  # noqa: E402

TAR_PATH = Path("/data/home/KibbleOS/backups/feeder-20260924-1807/opt.tar")
GROUND_TRUTH_CATS = ("Kitty", "Pancake", "not_a_cat")
PENDING = "pending"

Sample = tuple[str, bytes, bytes | None]  # (stem, jpg bytes, emb bytes or None)


def _extract_faces(tar_path: Path, dest: Path) -> None:
    with tarfile.open(tar_path) as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile()
            and any(f"librefeed/faces/{bucket}/" in m.name for bucket in (*GROUND_TRUTH_CATS, PENDING))
        ]
        tar.extractall(dest, members=members, filter="data")


def _load_samples(root: Path, bucket: str) -> list[Sample]:
    d = root / "librefeed" / "faces" / bucket
    out: list[Sample] = []
    for jpg in sorted(d.glob("*.jpg")):
        emb_path = jpg.with_suffix(".emb")
        emb = emb_path.read_bytes() if emb_path.exists() else None
        out.append((jpg.stem, jpg.read_bytes(), emb))
    return out


def _features(sample: Sample) -> identity.Features:
    _, jpg, emb = sample
    return identity.features_from(body_jpeg=None, face_jpeg=jpg, face_emb=emb)


def _ablate(feat: identity.Features, *, keep_emb: bool, keep_face_feat: bool) -> identity.Features:
    return identity.Features(
        face_emb=feat.face_emb if keep_emb else None,
        body_feat=None,
        face_feat=feat.face_feat if keep_face_feat else None,
        mode=feat.mode,
    )


def _loo_report(training: list[tuple[str, identity.Features]], classes: list[str]) -> dict:
    """Leave-one-out over `training` through the real, gated `Model.classify()`. Returns a
    `true-label -> Counter({predicted-or-"unknown": n})` confusion table plus accuracy / wrong
    / unknown rates per class and overall."""
    confusion: dict[str, Counter] = defaultdict(Counter)
    for i, (true_label, feat) in enumerate(training):
        reduced = training[:i] + training[i + 1 :]
        model = identity.Model(reduced)
        verdict = model.classify([feat])
        predicted = verdict.label if verdict.label is not None else "unknown"
        confusion[true_label][predicted] += 1

    per_class: dict[str, dict | None] = {}
    total_correct = total_wrong = total_unknown = total_n = 0
    for cls in classes:
        row = confusion.get(cls, Counter())
        n = sum(row.values())
        if n == 0:
            per_class[cls] = None
            continue
        correct = row.get(cls, 0)
        unknown = row.get("unknown", 0)
        wrong = n - correct - unknown
        per_class[cls] = {
            "n": n,
            "accuracy": correct / n,
            "wrong_rate": wrong / n,
            "unknown_rate": unknown / n,
        }
        total_correct += correct
        total_wrong += wrong
        total_unknown += unknown
        total_n += n

    overall = (
        {
            "n": total_n,
            "accuracy": total_correct / total_n,
            "wrong_rate": total_wrong / total_n,
            "unknown_rate": total_unknown / total_n,
        }
        if total_n
        else None
    )
    return {"confusion": confusion, "per_class": per_class, "overall": overall}


def _print_report(name: str, report: dict) -> None:
    print(f"\n=== {name} ===")
    overall = report["overall"]
    if overall is None:
        print("  no samples")
        return
    print(
        f"  overall: n={overall['n']} accuracy={overall['accuracy']:.3f} "
        f"wrong={overall['wrong_rate']:.3f} unknown={overall['unknown_rate']:.3f}"
    )
    for cls, stats in report["per_class"].items():
        if stats is None:
            print(f"  {cls}: no samples")
            continue
        print(
            f"  {cls}: n={stats['n']} accuracy={stats['accuracy']:.3f} "
            f"wrong={stats['wrong_rate']:.3f} unknown={stats['unknown_rate']:.3f}"
        )
    print("  confusion (true -> predicted counts):")
    for true_label, row in report["confusion"].items():
        pretty = ", ".join(f"{k}={v}" for k, v in sorted(row.items()))
        print(f"    {true_label}: {pretty}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kibble-eval-identity-") as tmp:
        dest = Path(tmp)
        print(f"extracting {TAR_PATH} -> {dest}")
        _extract_faces(TAR_PATH, dest)

        raw_samples: dict[str, list[Sample]] = {
            bucket: _load_samples(dest, bucket) for bucket in (*GROUND_TRUTH_CATS, PENDING)
        }
        for bucket, samples in raw_samples.items():
            with_emb = sum(1 for _, _, e in samples if e is not None)
            print(f"{bucket}: {len(samples)} crops, {with_emb} with .emb")

        training_full: list[tuple[str, identity.Features]] = [
            (cat, _features(sample)) for cat in GROUND_TRUTH_CATS for sample in raw_samples[cat]
        ]

        modes = Counter(feat.mode for _, feat in training_full)
        print(f"\nmode distribution across ground-truth crops: {dict(modes)}")
        undecodable = sum(1 for _, feat in training_full if feat.face_feat is None)
        no_emb = sum(1 for _, feat in training_full if feat.face_emb is None)
        print(f"undecodable face crops: {undecodable}/{len(training_full)}, missing/invalid .emb: {no_emb}/{len(training_full)}")

        classes = sorted(GROUND_TRUTH_CATS)

        emb_only = [(lbl, _ablate(f, keep_emb=True, keep_face_feat=False)) for lbl, f in training_full]
        _print_report("face embedding only", _loo_report(emb_only, classes))

        feat_only = [(lbl, _ablate(f, keep_emb=False, keep_face_feat=True)) for lbl, f in training_full]
        _print_report("face appearance only", _loo_report(feat_only, classes))

        _print_report("fused (embedding + appearance)", _loo_report(training_full, classes))

        # loo_accuracy() exactly as HaV2's Cats card will call it.
        fused_model = identity.Model(training_full)
        print("\nModel.loo_accuracy() (per class, public API):")
        for cls, acc in fused_model.loo_accuracy().items():
            print(f"  {cls}: {acc if acc is None else f'{acc:.3f}'}")

        # Pending queue: no ground truth, just the distribution the trained model would produce.
        pending_features = [_features(s) for s in raw_samples[PENDING]]
        counts: Counter = Counter()
        for feat in pending_features:
            verdict = fused_model.classify([feat])
            counts[verdict.label if verdict.label is not None else "unknown"] += 1
        total_pending = len(pending_features)
        print(f"\npending ({total_pending} crops) classified by the fused model:")
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {label}: {n} ({n / total_pending:.1%})")


if __name__ == "__main__":
    main()
