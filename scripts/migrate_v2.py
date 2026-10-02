#!/usr/bin/env python3
"""One-off migration from the v1 device-classifier pipeline to v2. See
`docs/36-ai-pipeline.md`'s "Migration" section for the full story; this script performs steps
2-4 there (step 1, taking the backup, and step 5, deleting `/opt/librefeed/faces/` on the
device once this has run, are the operator's own job before and after running this).

Standalone on purpose: no `homeassistant` import, no event loop. `store.py`'s `_SyncStore` and
`identity.py` are loaded straight from `<config>/custom_components/kibble/` -- the exact files
Home Assistant itself runs for this config entry -- by registering a throwaway `kibble` package
pointing at that directory (the same trick `tests/conftest.py` uses for the stubbed test suite),
so their own `from . import identity` / `from .const import DOMAIN` relative imports resolve
without ever executing `__init__.py` (which chains into `coordinator.py`, and does need a real
Home Assistant runtime). Needs `numpy` and `Pillow` importable -- both already installed
wherever Home Assistant itself runs this integration, since `identity.py` depends on them too.

Usage::

    python3 migrate_v2.py --backup /path/to/opt.tar --config /config --entry <entry_id> [--dry-run]

Every write is idempotent, keyed by a uid derived from the source filename: re-running against
the same backup and config root finds everything already present and changes nothing. `--dry-run`
never opens the destination store at all -- every "already present" check is a plain filesystem
existence test against exactly the path a real run would have written, so a dry run cannot
change anything on disk, not even create an empty database file.

Steps:

1. Training -- `librefeed/faces/{Kitty,Pancake,not_a_cat}/*.jpg` (+ the matching `.emb` when one
   exists) become `training` rows, `source='import'`. `librefeed/faces/other/*` (the v1 "skip"
   bucket) is never imported. `Kitty` and `Pancake` are registered in the `cats` roster;
   `not_a_cat` is a training label only, never a roster entry. These are face crops with no
   body crop, same as every sample identity.py's own tuning data has ever had.
2. Pending -- `librefeed/faces/pending/*.jpg` (199 on the real backup) each become one
   `kind="import"` event with a single face-only sample, dated from the leading unix timestamp
   in the filename. Classified with the model just trained in step 1 so a confident guess is
   recorded the same way live ingest would record one (mirrors `ingest.py`'s
   `IdentityEngine.async_classify_event` verbatim, just synchronous -- there is no event loop
   here to run the real one on); anything the model can't confidently name falls out to
   `kibble/review`, same as any other event. `import` events never appear on `kibble/timeline`
   (`store.HIDDEN_TIMELINE_KINDS`).
3. Legacy evidence -- `<config>/.storage/kibble/evidence/<entry_id>/events/*.jpg` move into
   `media/<date>/<name>` (date from each name's own leading unix timestamp, bucketed with
   `store.py`'s own `_utc_date` so it lines up with whatever date folder live ingest would use
   for the same asset name). `.storage/kibble/` is removed once everything under it has been
   accounted for -- only when not a dry run.

A file that cannot be turned into a usable feature (undecodable JPEG, or -- for pending crops
and legacy evidence -- no leading timestamp to date it with) is skipped and counted, never a
hard failure: one bad file in a 300+ image gallery must not abort the whole migration.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shutil
import sys
import tarfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FACES_PREFIX = "librefeed/faces"
TRAINING_BUCKETS = ("Kitty", "Pancake", "not_a_cat")
CAT_ROSTER = ("Kitty", "Pancake")  # not_a_cat is a training label only, never a roster cat
PENDING_BUCKET = "pending"
LEGACY_EVIDENCE_REL = Path(".storage") / "kibble"

_LEADING_INT = re.compile(r"^(\d+)")


# --- loading store.py/identity.py from the deployed integration, no homeassistant needed ------


def _load_kibble_modules(config_dir: Path) -> tuple[Any, Any]:
    """Imports `store` and `identity` from `<config_dir>/custom_components/kibble/` by
    registering a throwaway `kibble` package pointing at that directory -- see this module's
    docstring for why `__init__.py` must never execute here."""
    kibble_dir = config_dir / "custom_components" / "kibble"
    if not (kibble_dir / "store.py").is_file():
        raise SystemExit(f"no custom_components/kibble found under {config_dir}")
    if "kibble" not in sys.modules:
        stub = types.ModuleType("kibble")
        stub.__path__ = [str(kibble_dir)]
        sys.modules["kibble"] = stub
    store = importlib.import_module("kibble.store")
    identity = importlib.import_module("kibble.identity")
    return store, identity


def _leading_timestamp(name: str) -> int | None:
    match = _LEADING_INT.match(name)
    return int(match.group(1)) if match else None


# --- reading the backup tar ---------------------------------------------------------------


def _scan_jpg_bucket(
    members: dict[str, tarfile.TarInfo], tar: tarfile.TarFile, bucket: str
) -> list[tuple[str, bytes, bytes | None]]:
    """`[(stem, jpeg, emb_or_None)]` for every `.jpg` directly under
    `librefeed/faces/<bucket>/`, oldest-name-first (stable, reproducible run-to-run order)."""
    prefix = f"{FACES_PREFIX}/{bucket}/"
    out: list[tuple[str, bytes, bytes | None]] = []
    for name in sorted(members):
        if not (name.startswith(prefix) and name.endswith(".jpg")):
            continue
        jpeg = tar.extractfile(members[name]).read()
        emb_member = members.get(name[: -len(".jpg")] + ".emb")
        emb = tar.extractfile(emb_member).read() if emb_member is not None else None
        out.append((Path(name).stem, jpeg, emb))
    return out


# --- counting -----------------------------------------------------------------------------


@dataclass
class StepCounts:
    found: int = 0
    unreadable: int = 0
    imported: int = 0
    already_present: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "found": self.found,
            "unreadable": self.unreadable,
            "imported": self.imported,
            "already_present": self.already_present,
        }


# No sqlite connection is ever opened for a dry run, not even read-only: every "already
# present" check below is a plain filesystem existence test against exactly the path
# `write_training`/`write_media` would produce, so a dry run cannot touch the destination
# store at all -- not a new directory, not an empty database file, not even a WAL/SHM
# journal file's mtime.

# --- step 1: training -----------------------------------------------------------------------


@dataclass
class TrainingPlan:
    per_cat: dict[str, StepCounts] = field(default_factory=dict)
    to_write: list[tuple[str, str, bytes, Any]] = field(default_factory=list)  # (cat, uid, jpeg, feat)
    pairs: list[tuple[str, Any]] = field(default_factory=list)  # (cat, Features), every readable one


def _plan_training(
    identity_mod: Any, store_mod: Any, tar: tarfile.TarFile, members: dict[str, tarfile.TarInfo], training_root: Path
) -> TrainingPlan:
    plan = TrainingPlan()
    for cat in TRAINING_BUCKETS:
        counts = StepCounts()
        for stem, jpeg, emb in _scan_jpg_bucket(members, tar, cat):
            counts.found += 1
            feat = identity_mod.features_from(None, jpeg, emb)
            if feat.face_feat is None:
                counts.unreadable += 1
                continue
            plan.pairs.append((cat, feat))
            uid = f"import-{store_mod.slugify_cat(cat)}-{stem}"
            if (training_root / store_mod.slugify_cat(cat) / f"{uid}-face.jpg").is_file():
                counts.already_present += 1
                continue
            plan.to_write.append((cat, uid, jpeg, feat))
            counts.imported += 1
        plan.per_cat[cat] = counts
    return plan


def _apply_training(sync: Any, plan: TrainingPlan) -> None:
    for cat in CAT_ROSTER:
        sync.add_cat(cat)
    for cat, uid, jpeg, feat in plan.to_write:
        sync.add_import_training(cat=cat, uid=uid, body=None, face=jpeg, features=feat)


# --- step 2: pending -------------------------------------------------------------------------


@dataclass
class PendingItem:
    stem: str
    ts: int
    jpeg: bytes
    feat: Any
    event_uid: str


@dataclass
class PendingPlan:
    counts: StepCounts = field(default_factory=StepCounts)
    to_write: list[PendingItem] = field(default_factory=list)
    classification: dict[str, int] = field(default_factory=lambda: {"auto": 0, "unknown": 0, "not_a_cat": 0})


def _classify(identity_mod: Any, model: Any, feat: Any) -> tuple[str | None, str, float | None, list]:
    """Mirrors `ingest.py`'s `IdentityEngine.async_classify_event` verbatim, synchronously --
    there is no event loop here to run the real one on."""
    verdict = model.classify([feat])
    if verdict.label is None:
        cat, status = None, "unknown"
    elif verdict.label == identity_mod.NOT_A_CAT:
        cat, status = None, identity_mod.NOT_A_CAT
    else:
        cat, status = verdict.label, "auto"
    return cat, status, verdict.confidence, verdict.per_sample


def _plan_pending(
    store_mod: Any,
    identity_mod: Any,
    tar: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    entry_id: str,
    media_root: Path,
    model: Any,
) -> PendingPlan:
    plan = PendingPlan()
    for stem, jpeg, emb in _scan_jpg_bucket(members, tar, PENDING_BUCKET):
        plan.counts.found += 1
        feat = identity_mod.features_from(None, jpeg, emb)
        ts = _leading_timestamp(stem)
        if feat.face_feat is None or ts is None:
            plan.counts.unreadable += 1
            continue
        event_uid = f"{entry_id}-import-{stem}"
        _cat, status, _conf, _per_sample = _classify(identity_mod, model, feat)
        plan.classification[status] = plan.classification.get(status, 0) + 1
        if (media_root / store_mod._utc_date(ts) / f"{stem}.jpg").is_file():
            plan.counts.already_present += 1
            continue
        plan.to_write.append(PendingItem(stem=stem, ts=ts, jpeg=jpeg, feat=feat, event_uid=event_uid))
        plan.counts.imported += 1
    return plan


def _apply_pending(store_mod: Any, identity_mod: Any, sync: Any, model: Any, plan: PendingPlan) -> None:
    for item in plan.to_write:
        date = store_mod._utc_date(item.ts)
        media_id = sync.write_media(date, f"{item.stem}.jpg", item.jpeg)
        sample_uid = f"{item.event_uid}-s1"
        sync.upsert_event(
            uid=item.event_uid,
            device_event_id=item.ts,
            kind="import",
            start=item.ts,
            end=item.ts,
            open_=False,
            eat_start=None,
            scene=None,
            before=None,
            after=None,
        )
        sync.insert_sample(
            uid=sample_uid,
            event_uid=item.event_uid,
            t=item.ts,
            body=None,
            face=media_id,
            features=item.feat,
            box=None,
            score=None,
        )
        cat, status, confidence, per_sample = _classify(identity_mod, model, item.feat)
        sync.set_event_classification(
            item.event_uid,
            cat=cat,
            identity_status=status,
            confidence=confidence,
            per_sample_uids=[sample_uid],
            per_sample=per_sample,
        )


# --- step 3: legacy evidence -----------------------------------------------------------------


@dataclass
class LegacyItem:
    path: Path
    date: str


@dataclass
class LegacyPlan:
    counts: StepCounts = field(default_factory=StepCounts)
    to_write: list[LegacyItem] = field(default_factory=list)
    events_dir: Path | None = None
    storage_kibble_dir: Path | None = None


def _plan_legacy_evidence(store_mod: Any, config_dir: Path, entry_id: str, media_root: Path) -> LegacyPlan:
    plan = LegacyPlan()
    storage_kibble_dir = config_dir / LEGACY_EVIDENCE_REL
    plan.storage_kibble_dir = storage_kibble_dir if storage_kibble_dir.is_dir() else None
    events_dir = storage_kibble_dir / "evidence" / entry_id / "events"
    if not events_dir.is_dir():
        return plan
    plan.events_dir = events_dir
    for path in sorted(events_dir.glob("*.jpg")):
        plan.counts.found += 1
        ts = _leading_timestamp(path.name)
        if ts is None:
            plan.counts.unreadable += 1
            continue
        date = store_mod._utc_date(ts)
        if (media_root / date / path.name).is_file():
            plan.counts.already_present += 1
            continue
        plan.to_write.append(LegacyItem(path=path, date=date))
        plan.counts.imported += 1
    return plan


def _apply_legacy_evidence(sync: Any, plan: LegacyPlan, dry_run: bool) -> bool:
    for item in plan.to_write:
        sync.write_media(item.date, item.path.name, item.path.read_bytes())
    removed = False
    if not dry_run and plan.storage_kibble_dir is not None and plan.storage_kibble_dir.is_dir():
        shutil.rmtree(plan.storage_kibble_dir)
        removed = True
    return removed


# --- main -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--backup", required=True, type=Path, help="path to the v1 opt.tar backup")
    parser.add_argument("--config", required=True, type=Path, help="Home Assistant config root, e.g. /config")
    parser.add_argument("--entry", required=True, help="the kibble config entry id")
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args(argv)

    if not args.backup.is_file():
        parser.error(f"backup tar not found: {args.backup}")
    config_dir = args.config.resolve()

    store_mod, identity_mod = _load_kibble_modules(config_dir)

    root = store_mod.entry_root(config_dir, args.entry)
    training_root = root / "training"
    media_root = root / "media"

    with tarfile.open(args.backup) as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}

        training_plan = _plan_training(identity_mod, store_mod, tar, members, training_root)
        model = identity_mod.Model(training_plan.pairs)

        pending_plan = _plan_pending(store_mod, identity_mod, tar, members, args.entry, media_root, model)

    legacy_plan = _plan_legacy_evidence(store_mod, config_dir, args.entry, media_root)

    if not args.dry_run:
        sync = store_mod._SyncStore(root, args.entry)
        try:
            _apply_training(sync, training_plan)
            _apply_pending(store_mod, identity_mod, sync, model, pending_plan)
            storage_removed = _apply_legacy_evidence(sync, legacy_plan, dry_run=False)
        finally:
            sync.close()
    else:
        storage_removed = False

    report: dict[str, Any] = {
        "dry_run": args.dry_run,
        "entry": args.entry,
        "cats_registered": list(CAT_ROSTER),
        "training": {cat: training_plan.per_cat[cat].as_dict() for cat in TRAINING_BUCKETS},
        "pending": {**pending_plan.counts.as_dict(), "classification": pending_plan.classification},
        "legacy_evidence": {**legacy_plan.counts.as_dict(), "storage_kibble_removed": storage_removed},
    }

    _print_report(report)
    return 0


def _print_report(report: dict[str, Any]) -> None:
    import json

    mode = "DRY RUN (nothing written)" if report["dry_run"] else "APPLIED"
    print(f"=== kibble migrate_v2: {mode}, entry={report['entry']} ===")
    for cat, counts in report["training"].items():
        print(f"  training/{cat}: {counts}")
    print(f"  cats registered: {report['cats_registered']}")
    print(f"  pending: {report['pending']}")
    print(f"  legacy_evidence: {report['legacy_evidence']}")
    print("--- json ---")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
