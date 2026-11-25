"""SQLite-backed evidence archive, event journal, training set and retention policy for one
Kibble config entry -- the HA-storage half of docs/36-ai-pipeline.md's design.

Root: ``hass.config.path("kibble", entry_id)``::

    kibble.db                          SQLite (WAL); schema below
    media/<YYYY-MM-DD>/<asset>         archived evidence; deleted by retention
    training/<cat_slug>/<uid>-{body,face}.jpg   copies of labelled samples; never removed

Two layers on purpose:

- `_SyncStore` is plain `sqlite3` + `pathlib`, no Home Assistant import at all. It is the
  entire implementation, and is exactly what `scripts/migrate_v2.py` uses directly (that
  script has no event loop and no HA runtime to hand it).
- `KibbleStore` is the async facade `custom_components/kibble` actually uses: every call is
  proxied onto one dedicated single-worker executor thread (never HA's shared pool, and never
  more than one at a time), because a `sqlite3.Connection` may only ever be touched
  concurrently by one thread even with `check_same_thread=False`.

Schema version 1. `samples.box_*`/`score` are an addition over docs/36-ai-pipeline.md's first
draft (approved 2026-09-24): accurate thumbnail selection has to reject tiny/clipped boxes and
prefer a face, and that decision is re-made every time reclassification changes a sample's
`guess`, not just once at ingest -- so the raw detector geometry has to survive in the row, not
just get consumed and discarded. The thumbnail itself is never cached: it is a pure function of
an event's current samples, computed fresh on every read.
"""

from __future__ import annotations
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import autolearn, coral_identity, crop_geometry, identity, photo_queue, sessions
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 11

MEDIA_CAP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB guard, docs/36-ai-pipeline.md
MEDIA_CAP_TARGET_RATIO = 0.9  # purge down to 90% of the cap, not to the line
# Per-cat training-set caps: `TRAINING_CAP_PER_CAT` is where eviction *starts* -- pressure at
# this line is absorbed entirely by evicting `auto`-source rows (most redundant, then lowest
# confidence, then oldest), never a human-labelled/uploaded/imported one. `_HARD_CAP` is the
# ceiling those protected rows themselves can never cross: only once every evictable auto row
# is gone and the cat is *still* over the hard cap does eviction reach into them, oldest first.
# See `_select_eviction_candidates`'s docstring for the full algorithm.
TRAINING_CAP_PER_CAT = 400
TRAINING_HARD_CAP_PER_CAT = 600
# Whole-store training-set budget, independent of per-cat counts -- guards against many-cat
# growth the per-cat cap alone would not bound. Same soft/hard split and eviction order as the
# per-cat cap, just measured in bytes across every cat's pool at once (`_training_file_bytes`).
# At the measured ~100 KB/sample (640px max edge, JPEG q82 -- see `media_processing.py` and
# kibble/docs/36-ai-pipeline.md's storage-math note) 512 MiB is ~5000 samples: several times
# `TRAINING_CAP_PER_CAT` even for half a dozen enrolled cats, so in practice the per-cat cap
# binds first and this is the many-cat/oversized-sample backstop.
TRAINING_CAP_TOTAL_BYTES = 512 * 1024 * 1024
TRAINING_HARD_CAP_TOTAL_BYTES = 768 * 1024 * 1024
# Refuse a new upload/auto-learn write once the entry's own filesystem has less free space than
# this left -- checked against `shutil.disk_usage`, never inferred from the caps above (a
# neighbouring HA add-on or the recorder DB can fill the same disk independently of anything
# Kibble itself wrote).
FREE_DISK_FLOOR_BYTES = 500 * 1024 * 1024
# A custom avatar lives in its own directory, never as a reference into `media/`/`training/` --
# see `set_cat_avatar`'s docstring for why.
AVATAR_DIR = "avatars"

HIDDEN_TIMELINE_KINDS = ("import",)
LABEL_UNKNOWN = "unknown"
# `samples.review` reserved values -- anything else stored there is a cat name. `LABEL_SKIP`
# ("Don't use this photo") means "never trained, regardless of what the visit says"; `LABEL_BOTH`
# ("both cats are in this photo") is never trained either -- a crop of two animals teaches the
# classifier nothing about either -- but does record that both cats were there; `LABEL_FOLLOW`
# is never actually stored (a "follow" `kibble/sample/label` call clears the column back to
# `NULL`) -- it is the WS-facing spelling for "no override" so the card never has to send a
# literal `null`.
LABEL_SKIP = "skip"
LABEL_BOTH = "both"
LABEL_FOLLOW = "follow"

# Reclassification/thumb selection never trusts a sample whose per-sample guess is a
# not_a_cat verdict -- it is not evidence of what the cat looks like, so it must not become
# the picture representing the track either.
_NOT_A_CAT = identity.NOT_A_CAT

_UNSAFE_SLUG = re.compile(r"[^a-z0-9_-]+")


def slugify_cat(name: str) -> str:
    """A filesystem/asset-id-safe stand-in for a cat's display name: lowercase, `_`/`-` only,
    never empty (an all-punctuation name still needs a directory)."""
    slug = _UNSAFE_SLUG.sub("_", name.strip().lower()).strip("_-")
    return slug or "cat"


def entry_root(config_dir: str | os.PathLike[str], entry_id: str) -> Path:
    """The root directory one config entry's store lives under -- shared by `KibbleStore`
    (via `hass.config.path`) and `scripts/migrate_v2.py` (given the config dir directly)."""
    return Path(config_dir) / DOMAIN / entry_id


def _utc_date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _now() -> int:
    return int(time.time())


def resolve_asset_path(root: Path, asset_id: str) -> Path | None:
    """Maps a public asset id back to a file under `root`, or `None` if `asset_id` cannot
    possibly be one HA wrote (path traversal, absolute path, empty segment). `training/...`
    and `avatars/...` ids resolve directly under `root`; every other id is `media/<id>`."""
    if not asset_id or ".." in asset_id.split("/") or asset_id.startswith("/"):
        return None
    relative = asset_id if asset_id.startswith(("training/", f"{AVATAR_DIR}/")) else f"media/{asset_id}"
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _asset(entry_id: str, asset_id: str | None) -> dict[str, str] | None:
    if not asset_id:
        return None
    return {"id": asset_id, "url": f"/api/kibble/{entry_id}/media/{asset_id}"}


# --- Thumbnail selection: a pure function of one event's current samples ----------------------

_MIN_BOX_AREA_RATIO = 0.04
_EDGE_MARGIN = 0.02


@dataclass(frozen=True, slots=True)
class ThumbCandidate:
    """Exactly what thumbnail selection needs from one sample row -- box/score are the raw
    detector geometry (frame-relative 0..1), `guess` is that sample's own classifier verdict.
    `face` is the face-crop asset id (or `None`); it also serves as the displayed thumb when
    a sample has no body crop at all -- true of every migrated face-only import row, and of
    any live sample whose body write was refused by the spool guard."""

    uid: str
    t: int
    body: str | None
    has_face: bool
    score: float | None
    box: tuple[float, float, float, float] | None
    guess: str | None
    face: str | None = None


def _box_area(box: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _box_clipped(box: tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = box
    return (
        x1 <= _EDGE_MARGIN
        or y1 <= _EDGE_MARGIN
        or x2 >= 1.0 - _EDGE_MARGIN
        or y2 >= 1.0 - _EDGE_MARGIN
    )


def _box_usable(box: tuple[float, float, float, float] | None) -> bool:
    """No box at all (the device didn't supply one) can't be filtered on and passes; a
    present box must clear both the minimum-area and not-clipped-at-the-edge gates."""
    if box is None:
        return True
    return _box_area(box) >= _MIN_BOX_AREA_RATIO and not _box_clipped(box)


def _frame_box_rows(raw: str | None) -> list[tuple[int | None, tuple[float, float, float, float]]]:
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, list) or len(row) != 5:
            continue
        try:
            sid = int(row[0]) if row[0] is not None else None
            box = tuple(float(value) for value in row[1:])
        except (TypeError, ValueError, OverflowError):
            continue
        x1, y1, x2, y2 = box
        if not all(math.isfinite(value) for value in box) or not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            continue
        out.append((sid, box))
    return out


def pick_thumb(candidates: Sequence[ThumbCandidate]) -> ThumbCandidate | None:
    """The best crop to represent a track, or `None` if nothing qualifies: exclude a sample
    the classifier itself calls `not_a_cat` or with neither a body nor a face crop, exclude a
    box that is tiny or clipped at the frame edge, then prefer a sample with a face (a face
    confirms a cat), then the highest body-detector score. Use `thumb_asset_id` to turn the
    result into the asset id actually worth displaying -- a face-only candidate's `body` is
    `None`, so `.body` alone is never enough."""
    pool = [
        c
        for c in candidates
        if (c.body is not None or c.face is not None)
        and c.guess != _NOT_A_CAT
        and _box_usable(c.box)
    ]
    if not pool:
        return None
    with_face = [c for c in pool if c.has_face]
    ranked = with_face or pool
    return max(ranked, key=lambda c: (c.score if c.score is not None else -1.0, c.t))


def review_thumb(candidates: Sequence[ThumbCandidate]) -> ThumbCandidate | None:
    """The crop to show for an event awaiting human review. Same preference as `pick_thumb`,
    but never empty while any crop exists: the review queue is exactly where a doubtful sample
    (a not_a_cat guess, an awkward box) must still be visible so a person can judge it."""
    best = pick_thumb(candidates)
    if best is not None:
        return best
    pool = [c for c in candidates if c.body is not None or c.face is not None]
    if not pool:
        return None
    return max(pool, key=lambda c: (c.has_face, c.score if c.score is not None else -1.0, c.t))


def thumb_asset_id(candidate: ThumbCandidate | None) -> str | None:
    """The asset id `pick_thumb`'s choice should actually display: the body crop, or the face
    crop when there is no body (a face-only sample -- see `ThumbCandidate.face`)."""
    if candidate is None:
        return None
    return candidate.body or candidate.face


# --- Training-set eviction: a pure function of one cat's (or the whole store's) candidate rows -

_PROTECTED_TRAINING_SOURCES = ("label", "upload", "import")


@dataclass(frozen=True, slots=True)
class TrainingCandidate:
    """Exactly what eviction ranking needs from one `training` row."""

    uid: str
    source: str
    confidence: float | None
    created: int


def _nearest_distance_ranks(
    entries: Sequence[tuple[str, Any, str | None]], only: Collection[str] | None = None
) -> dict[str, float]:
    """Per-uid nearest-neighbour distance to every OTHER entry of the same mode in `entries`
    (`(uid, feature_vector_or_None, mode)` triples) -- smaller means more redundant.
    `float("inf")` for an entry with no comparable feature at all (nothing to judge redundancy
    by, so it is never preferred for eviction on that basis). `only` limits the ranking to
    those uids -- each still measured against the whole pool, so its value is what a full
    ranking would give -- because eviction only ever ranks its `auto` rows."""
    wanted = None if only is None else frozenset(only)
    out = {uid: float("inf") for uid, _, _ in entries if wanted is None or uid in wanted}
    pools: dict[tuple[str | None, tuple[int, ...]], list[int]] = {}
    for index, (_, feat, mode) in enumerate(entries):
        if feat is not None:
            pools.setdefault((mode, feat.shape), []).append(index)
    for members in pools.values():
        rows = [row for row, index in enumerate(members) if wanted is None or entries[index][0] in wanted]
        if not rows:
            continue
        distances = identity.nearest_other_distances([entries[index][1] for index in members], rows)
        for row, distance in zip(rows, distances, strict=True):
            out[entries[members[row]][0]] = distance
    return out


def _select_eviction_candidates(
    candidates: Sequence[TrainingCandidate],
    redundancy: dict[str, float],
    soft_cap: int,
    hard_cap: int,
) -> list[str]:
    """uids to evict from one training pool, worst-first. `auto`-source rows absorb pressure
    down to `soft_cap` alone -- ranked most redundant first, then lowest confidence, then
    oldest -- and only once every evictable `auto` row is gone does eviction reach past
    `soft_cap` at all. A protected row (`label`/`upload`/`import`) is evicted only if the pool
    is *still* over `hard_cap` after that, oldest protected first -- "never ... unless over a
    hard cap" from the design note, made literal. An unrecognised future `source` value is
    treated as evictable (not in `_PROTECTED_TRAINING_SOURCES`): the safe failure mode for a
    forgotten source is pressure that reaches it appropriately, not silent unbounded growth."""
    auto = [c for c in candidates if c.source not in _PROTECTED_TRAINING_SOURCES]
    protected = [c for c in candidates if c.source in _PROTECTED_TRAINING_SOURCES]
    auto_ranked = sorted(
        auto,
        key=lambda c: (
            redundancy.get(c.uid, float("inf")),
            c.confidence if c.confidence is not None else 0.0,
            c.created,
        ),
    )
    auto_excess = max(0, len(candidates) - soft_cap)
    evict_auto = auto_ranked[: min(auto_excess, len(auto_ranked))]
    remaining = len(candidates) - len(evict_auto)
    evict_protected: list[TrainingCandidate] = []
    if remaining > hard_cap:
        hard_excess = remaining - hard_cap
        evict_protected = sorted(protected, key=lambda c: c.created)[:hard_excess]
    return [c.uid for c in (*evict_auto, *evict_protected)]


def _sample_training_target(
    review: str | None, owner: sqlite3.Row, session: sqlite3.Row
) -> str | None:
    """What ONE photo should be trained as -- the single rule the training gallery follows
    (docs/36-ai-pipeline.md, "What trains"): a cat name, `"not_a_cat"`, or `None` for "not in
    the gallery". `owner` is the visit part the photo belongs to, `session` the top-level visit.

    - "Can't tell" on the visit (`owner` reviewed as unknown) trains nothing, whatever any
      single photo of it says -- the verdict is about the whole visit, so it wins.
    - A photo answered "Don't use" or "Both in this photo" is never trained.
    - A photo answered with a cat teaches that cat; answered "Not a cat" it teaches the
      "not a cat" class, exactly as the whole-visit label always did.
    - An unanswered photo follows its visit only when a person labelled the visit as a whole
      (the legacy visit-level answer). In a photo-driven visit (`from_photos`) it is display
      only: whoever answered a few photos did not vouch for the rest."""
    if owner["reviewed"] and owner["identity_status"] == LABEL_UNKNOWN:
        return None
    if review in (LABEL_SKIP, LABEL_BOTH, LABEL_UNKNOWN):
        return None
    if review is not None:
        return review
    if session["from_photos"] or not owner["reviewed"]:
        return None
    if owner["identity_status"] == "reviewed" and owner["cat"]:
        return owner["cat"]
    if owner["identity_status"] == _NOT_A_CAT:
        return _NOT_A_CAT
    return None


# --- Cursor encoding for timeline/review/training paging --------------------------------------


def _encode_cursor(start: int, uid: str) -> str:
    import base64

    raw = f"{start}\0{uid}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    import base64
    import binascii

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        start_str, uid = raw.split("\0", 1)
        return int(start_str), uid
    except (ValueError, binascii.Error, UnicodeDecodeError) as err:
        raise ValueError("invalid timeline cursor") from err


# --- Identity summary consumed by entities (binary_sensor/sensor/image) -----------------------


@dataclass(frozen=True, slots=True)
class CatStats:
    """One enrolled cat's identity-derived state -- everything the per-cat entities need,
    recomputed after every ingest pass or store mutation that could change it. `avatar`/
    `avatar_updated` back the per-cat `image` entity (`image.py`'s `KibbleCatAvatarImage`) the
    same way `last_detection_thumb` backs the device-wide one -- the effective avatar asset id
    (custom override if set, else the newest trained photo) and when it last changed.
    `learning_state` is `autolearn.learning_state`'s own three values -- see that function's
    docstring for the two consumers that both read it from here. `recognition_score`/
    `recognition_basis`/`training_samples`/`training_uploads` back `sensor.py`'s per-cat and
    overall recognition sensors -- `autolearn.recognition_score`'s own output, computed once
    here rather than by each sensor separately."""

    last_seen: int | None = None
    last_meal: int | None = None
    recent_meals: tuple[int, ...] = ()  # eat starts within the last 48h -- "today" is filtered live
    present: bool = False
    avatar: str | None = None
    avatar_updated: int | None = None
    learning_state: str = "learning"
    recognition_score: int = 0
    recognition_basis: str = "estimate"
    training_samples: int = 0
    training_uploads: int = 0


@dataclass(frozen=True, slots=True)
class DeviceIdentitySummary:
    """Device-wide identity state -- `sensor.*_last_seen_pet`/`image.*_last_detection` and the
    per-cat roster driving the dynamically-created per-cat entities."""

    last_seen_pet: str | None = None
    last_seen_pet_ts: int | None = None
    last_detection_thumb: dict[str, str] | None = None
    cats: dict[str, CatStats] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> DeviceIdentitySummary:
        return cls()


# --- The synchronous core ----------------------------------------------------------------------


class _SyncStore:
    """All actual SQLite/filesystem work. Every method runs to completion on whichever thread
    calls it; callers (`KibbleStore`, `scripts/migrate_v2.py`) are responsible for making sure
    that's always the same one thread per instance."""

    def __init__(self, root: Path, entry_id: str) -> None:
        self.root = root
        self.entry_id = entry_id
        self.media_root = root / "media"
        self.training_root = root / "training"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.training_root.mkdir(parents=True, exist_ok=True)
        # Set whenever the training gallery changes (`_bump_training_generation`); `KibbleStore`
        # reads and clears it after every job so the learning job hears about it at once.
        self._learning_dirty = False
        self._media_bytes_cache: tuple[float, int] | None = None
        self._review_gallery_cache: tuple[int, float, photo_queue.Gallery | None] | None = None
        self.conn = sqlite3.connect(root / "kibble.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # --- schema ----------------------------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                uid TEXT PRIMARY KEY,
                device_event_id INTEGER,
                kind TEXT NOT NULL,
                start INTEGER NOT NULL,
                end INTEGER,
                open INTEGER NOT NULL,
                eat_start INTEGER,
                scene TEXT,
                scene_k INTEGER,
                subjects TEXT,
                before TEXT,
                after TEXT,
                cat TEXT,
                identity_status TEXT,
                confidence REAL,
                reviewed INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL,
                parent_uid TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                clip_id TEXT,
                clip_start_ms INTEGER,
                clip_end_ms INTEGER,
                judge_present INTEGER,
                judge_cat TEXT,
                judge_confidence REAL,
                judge_multiple INTEGER,
                judge_reason TEXT,
                judge_model TEXT,
                judge_at INTEGER,
                judge_evidence TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
            CREATE INDEX IF NOT EXISTS idx_events_open ON events(open);
            CREATE INDEX IF NOT EXISTS idx_events_review ON events(identity_status, reviewed);
            CREATE INDEX IF NOT EXISTS idx_events_cat ON events(cat, kind, start);

            CREATE TABLE IF NOT EXISTS samples(
                uid TEXT PRIMARY KEY,
                event_uid TEXT NOT NULL,
                t INTEGER NOT NULL,
                body TEXT,
                face TEXT,
                face_emb BLOB,
                body_feat BLOB,
                face_feat BLOB,
                mode TEXT,
                guess TEXT,
                guess_confidence REAL,
                box_x1 REAL,
                box_y1 REAL,
                box_x2 REAL,
                box_y2 REAL,
                score REAL,
                review TEXT,
                review_src TEXT,
                sid INTEGER,
                frame_k INTEGER,
                is_primary INTEGER NOT NULL DEFAULT 1,
                bowl INTEGER,
                frame_boxes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_samples_event ON samples(event_uid);

            CREATE TABLE IF NOT EXISTS training(
                uid TEXT PRIMARY KEY,
                cat TEXT NOT NULL,
                source TEXT NOT NULL,
                created INTEGER NOT NULL,
                body TEXT,
                face TEXT,
                face_emb BLOB,
                body_feat BLOB,
                face_feat BLOB,
                mode TEXT,
                confidence REAL
            );
            CREATE INDEX IF NOT EXISTS idx_training_cat ON training(cat, created);

            CREATE TABLE IF NOT EXISTS feeds(
                uid TEXT PRIMARY KEY,
                device_feed_id TEXT,
                ts INTEGER NOT NULL,
                portions REAL,
                scheduled INTEGER,
                confirmed INTEGER,
                before TEXT,
                after TEXT,
                amount1 INTEGER,
                amount2 INTEGER,
                food1 TEXT,
                food2 TEXT,
                single INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_feeds_ts ON feeds(ts);

            CREATE TABLE IF NOT EXISTS cats(
                name TEXT PRIMARY KEY,
                color INTEGER NOT NULL,
                created INTEGER NOT NULL,
                avatar_asset TEXT,
                avatar_updated INTEGER,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

            CREATE TABLE IF NOT EXISTS review_outcomes(
                uid TEXT PRIMARY KEY,
                cat TEXT NOT NULL,
                ts INTEGER NOT NULL,
                correct INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_review_outcomes_cat ON review_outcomes(cat, ts);

            CREATE TABLE IF NOT EXISTS auto_learn_state(
                cat TEXT PRIMARY KEY,
                paused INTEGER NOT NULL,
                updated INTEGER NOT NULL
            );

            -- Schema version 8 (docs/41-coral-recognition.md): one cached Coral embedding per
            -- (training row OR sample row, crop kind, model id) -- a brand-new table needs no
            -- ALTER-TABLE dance, unlike every earlier schema bump above; `IF NOT EXISTS` alone
            -- makes it idempotent for both a fresh database and an upgraded one. `row_kind` is
            -- 'training' or 'sample'; `row_uid` is that table's own `uid`; `crop_kind` is
            -- 'body' or 'face'. An embedding is a pure function of one crop's own bytes and the
            -- model that produced it, so it is written at most once per key
            -- (`set_coral_embedding`'s own `ON CONFLICT ... DO NOTHING`) and survives a
            -- training row's cat being changed by a re-label (`_upsert_training_row` moves the
            -- FILE, never the row's `uid`, on a re-label -- see its own docstring).
            CREATE TABLE IF NOT EXISTS coral_embeddings(
                row_kind TEXT NOT NULL,
                row_uid TEXT NOT NULL,
                crop_kind TEXT NOT NULL,
                model_id TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created INTEGER NOT NULL,
                PRIMARY KEY (row_kind, row_uid, crop_kind, model_id)
            );
            CREATE INDEX IF NOT EXISTS idx_coral_embeddings_row ON coral_embeddings(row_kind, row_uid);

            -- Schema version 11 (docs/36-ai-pipeline.md, "Learning job"): the durable half of the
            -- coalesced learning job. One row. `gen` rises by one inside the same transaction as
            -- every change to the training gallery (`_upsert_training_row`, `_delete_training_row`);
            -- `done_gen` is the `gen` the last finished learning pass covered, so `gen > done_gen`
            -- survives a restart as "learning still owed". `answers`/`answers_done` count the
            -- human answers that changed the gallery and the ones a finished pass has used (only
            -- for the "N answers saved" figure). `last_result` is the last finished pass's summary
            -- (JSON), `counts_done` the per-cat training counts it ended with (so the next pass can
            -- say what was learned since), `last_error` the last failure text while it keeps
            -- retrying.
            CREATE TABLE IF NOT EXISTS learning(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                gen INTEGER NOT NULL DEFAULT 0,
                done_gen INTEGER NOT NULL DEFAULT 0,
                answers INTEGER NOT NULL DEFAULT 0,
                answers_done INTEGER NOT NULL DEFAULT 0,
                last_result TEXT,
                counts_done TEXT,
                last_error TEXT
            );
            INSERT OR IGNORE INTO learning(id) VALUES (1);
            -- Write-ahead intent for label writes: the visit whose training rows still have to be
            -- reconciled with its photo answers. Inserted in the SAME transaction as the answer,
            -- deleted once `reconcile_session_training` succeeded, so a crash between the two can
            -- never leave a "Can't tell"/"Don't use" photo in the gallery.
            CREATE TABLE IF NOT EXISTS learning_queue(session_uid TEXT PRIMARY KEY);
            """
        )
        # `feeds` predates `amount1`/`amount2`/`food1`/`food2`/`single` (schema version 1 --
        # docs/37-hopper-full.md's hopper-modes-and-food-names section): a fresh database gets
        # them straight from `CREATE TABLE` above, but an existing one needs each column added
        # in place. `ALTER TABLE ... ADD COLUMN` has no "IF NOT EXISTS" of its own, so `PRAGMA
        # table_info` is the idempotency check. `hopper`, the column these five replace, is
        # left alone: dropping a column is a full table rebuild for no benefit, since nothing
        # has read or written it since.
        feed_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(feeds)")}
        for column, decl in (
            ("amount1", "INTEGER"),
            ("amount2", "INTEGER"),
            ("food1", "TEXT"),
            ("food2", "TEXT"),
            ("single", "INTEGER"),
        ):
            if column not in feed_columns:
                self.conn.execute(f"ALTER TABLE feeds ADD COLUMN {column} {decl}")
        # `samples` predates `review` (schema version 3, per-photo label overrides): same
        # idempotency check, same reasoning -- a fresh database already has it from `CREATE
        # TABLE` above.
        sample_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(samples)")}
        for column, decl in (
            ("review", "TEXT"),
            ("review_src", "TEXT"),
            ("sid", "INTEGER"),
            ("frame_k", "INTEGER"),
            ("is_primary", "INTEGER NOT NULL DEFAULT 1"),
            ("bowl", "INTEGER"),
            ("frame_boxes", "TEXT"),
        ):
            if column not in sample_columns:
                self.conn.execute(f"ALTER TABLE samples ADD COLUMN {column} {decl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_event_sid ON samples(event_uid, sid)")
        # `cats` predates `avatar_asset`/`avatar_updated` (schema version 4, custom per-cat
        # avatar) and `training` predates `confidence` (same version, auto-learn quality
        # ranking): same idempotency check, same reasoning as `feeds`/`samples` above.
        cat_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(cats)")}
        for column, decl in (("avatar_asset", "TEXT"), ("avatar_updated", "INTEGER")):
            if column not in cat_columns:
                self.conn.execute(f"ALTER TABLE cats ADD COLUMN {column} {decl}")
        training_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(training)")}
        if "confidence" not in training_columns:
            self.conn.execute("ALTER TABLE training ADD COLUMN confidence REAL")
        # `events` predates `parent_uid`/`hidden` (schema version 5, session splitting --
        # docs/36-ai-pipeline.md): same idempotency check, same reasoning as `feeds`/`samples`/
        # `cats`/`training` above.
        event_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(events)")}
        for column, decl in (("parent_uid", "TEXT"), ("hidden", "INTEGER NOT NULL DEFAULT 0")):
            if column not in event_columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
        # Always safe, always cheap (IF NOT EXISTS): a fresh database already has `parent_uid`
        # from `CREATE TABLE` above and never hits the ALTER branch at all, so this is the one
        # place both a fresh and an upgraded database are guaranteed to have the column first.
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_uid)")
        # `events` predates `scene_k`/`subjects` (schema version 9, multi-cat tracking).
        for column, decl in (("scene_k", "INTEGER"), ("subjects", "TEXT")):
            if column not in event_columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_device_session "
            "ON events(device_event_id, parent_uid, open, updated DESC)"
        )
        # `events` predates `clip_id`/`clip_start_ms`/`clip_end_ms` (schema version 6, Scrypted
        # eating-clip playback -- docs/39-eating-clips.md): same idempotency check, same
        # reasoning as every column added above. Only ever written on a TOP-LEVEL event
        # (`parent_uid IS NULL`) -- a split child's own row never gets these directly; it
        # resolves through its parent instead (`_clip_fields` below), since the recorded clip
        # spans the whole shared session, not one cat's own slice of it.
        for column, decl in (
            ("clip_id", "TEXT"), ("clip_start_ms", "INTEGER"), ("clip_end_ms", "INTEGER")
        ):
            if column not in event_columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
        # `events` predates `judge_present`/`judge_cat`/`judge_confidence`/`judge_multiple`/
        # `judge_reason`/`judge_model`/`judge_at`/`judge_evidence`, and `cats` predates
        # `description` (schema version 7, second-opinion vision judge -- docs/40-vision-
        # judge.md): same idempotency check, same reasoning as every column added above. The
        # `judge_*` columns are a verbatim record of the last verdict for one event (Contract
        # 1, plus which model answered, when, and the evidence key it was judged against);
        # `description` is one enrolled cat's coat description, filled in by the judge itself
        # (Contract 3) the first time it runs, blank until then.
        for column, decl in (
            ("judge_present", "INTEGER"), ("judge_cat", "TEXT"), ("judge_confidence", "REAL"),
            ("judge_multiple", "INTEGER"), ("judge_reason", "TEXT"), ("judge_model", "TEXT"),
            ("judge_at", "INTEGER"), ("judge_evidence", "TEXT"),
        ):
            if column not in event_columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
        if "description" not in cat_columns:
            self.conn.execute("ALTER TABLE cats ADD COLUMN description TEXT")
        # `events` predates `from_photos`/`ate_confirmed` (schema version 11, one-photo review):
        # `from_photos` marks a visit whose identity was DERIVED from per-photo answers, so photos
        # nobody answered are display-only and never trained; `ate_confirmed` records that a human
        # settled "who ate" for a multi-cat meal (`kibble/session/ate`).
        for column, decl in (
            ("from_photos", "INTEGER NOT NULL DEFAULT 0"), ("ate_confirmed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in event_columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        try:
            old_version = int(row["value"]) if row is not None else 0
        except (TypeError, ValueError):
            old_version = 0
        if old_version < 10:
            self._migrate_v10()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        elif row["value"] != str(SCHEMA_VERSION):
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
            )
        self.conn.commit()

    def _migrate_v10(self) -> None:
        """Replace reviewed v1 per-track lanes with the session's reviewed cat set."""
        children = self.conn.execute(
            "SELECT * FROM events WHERE parent_uid IS NOT NULL ORDER BY rowid"
        ).fetchall()
        by_parent: dict[str, list[sqlite3.Row]] = {}
        for child in children:
            parent_uid = child["parent_uid"]
            suffix = child["uid"][len(parent_uid) :]
            if suffix.startswith("-sub") and suffix[4:].isdigit():
                by_parent.setdefault(parent_uid, []).append(child)

        for parent_uid, lanes in by_parent.items():
            parent = self.conn.execute("SELECT * FROM events WHERE uid=?", (parent_uid,)).fetchone()
            if parent is None:
                continue
            human_cats: dict[str, bool] = {}
            for lane in lanes:
                if not lane["reviewed"]:
                    continue
                if lane["identity_status"] == "reviewed" and lane["cat"]:
                    cat = lane["cat"]
                    human_cats[cat] = human_cats.get(cat, False) or lane["kind"] == "eat"
                    self.conn.execute(
                        "UPDATE samples SET review=?, review_src='subject' "
                        "WHERE event_uid=? AND (review IS NULL OR review_src='subject')",
                        (cat, lane["uid"]),
                    )
                elif lane["identity_status"] in (_NOT_A_CAT, LABEL_UNKNOWN) or lane["cat"] == _NOT_A_CAT:
                    review = LABEL_UNKNOWN if lane["identity_status"] == LABEL_UNKNOWN else _NOT_A_CAT
                    self.conn.execute(
                        "UPDATE samples SET review=?, review_src='photo' "
                        "WHERE event_uid=? AND review IS NULL",
                        (review, lane["uid"]),
                    )
            if human_cats:
                # v1 lanes took `kind` from per-subject bowl timing, which fragmented tracks got
                # wrong (session e2060: a 3.5 minute meal left both lanes as visits). A session the
                # feeder itself reported as an eat, answered with exactly one cat, is that cat's meal.
                if parent["kind"] == "eat" and len(human_cats) == 1:
                    human_cats = {cat: True for cat in human_cats}
                self.replan_session(
                    parent_uid,
                    {},
                    human_cats=[(cat, ate) for cat, ate in human_cats.items()],
                )
            else:
                self._unsplit_session(parent, lanes)
                self.replan_session(parent_uid, {})
        self.conn.commit()

    # --- media/training filesystem ----------------------------------------------------------

    def media_exists(self, date: str, filename: str) -> bool:
        return (self.media_root / date / filename).exists()

    def write_media(self, date: str, filename: str, data: bytes) -> str:
        """Atomically persists one asset under `media/<date>/<filename>`, returning its asset
        id. A no-op (no rewrite) if the file is already there -- `filename`s are unique per
        capture, so an existing file is always the same bytes."""
        path = self.media_root / date / filename
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return f"{date}/{filename}"

    def delete_media(self, asset_id: str) -> None:
        path = self.media_root / asset_id
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def write_training(self, cat: str, uid: str, kind: str, data: bytes) -> str:
        slug = slugify_cat(cat)
        filename = f"{uid}-{kind}.jpg"
        path = self.training_root / slug / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return f"training/{slug}/{filename}"

    def delete_training_files(self, cat: str, uid: str) -> None:
        slug = slugify_cat(cat)
        for kind in ("body", "face"):
            try:
                (self.training_root / slug / f"{uid}-{kind}.jpg").unlink()
            except FileNotFoundError:
                pass

    def _delete_training_row(self, uid: str, cat: str) -> None:
        """Removes one training row's files, cached Coral embeddings and DB row together --
        every full removal (eviction, `training_remove`, a label reverting a row to `skip`)
        goes through this one place so a future addition here never has to be remembered at
        each call site separately -- and advances the learning generation, so the learning job
        knows the model is out of date. Never called when a row's files are merely MOVING to a
        different cat's directory (`_upsert_training_row`'s own re-label path) -- that keeps
        the row and its cached embeddings, since an embedding is a property of the crop's own
        pixels, not of which cat it is currently labelled as."""
        self.delete_training_files(cat, uid)
        self.conn.execute("DELETE FROM coral_embeddings WHERE row_kind='training' AND row_uid=?", (uid,))
        self.conn.execute("DELETE FROM training WHERE uid=?", (uid,))
        self._bump_training_generation()

    def _media_bytes_by_date(self) -> list[tuple[str, int]]:
        """`[(date, bytes)]` for every day directory under `media/`, oldest first."""
        if not self.media_root.exists():
            return []
        out: list[tuple[str, int]] = []
        for day_dir in sorted(self.media_root.iterdir()):
            if not day_dir.is_dir():
                continue
            total = sum(f.stat().st_size for f in day_dir.glob("**/*") if f.is_file())
            out.append((day_dir.name, total))
        return out

    def _media_used_bytes(self) -> int:
        """Total bytes under `media/`, at most a minute old: stat-walking every file takes a
        second or more on a full store, and the only reader (`kibble/cats`, every time the cats
        screen opens) just shows the figure. Retention's own cap check does NOT use this."""
        cached = self._media_bytes_cache
        now = time.monotonic()
        if cached is None or now - cached[0] > 60:
            cached = (now, sum(size for _, size in self._media_bytes_by_date()))
            self._media_bytes_cache = cached
        return cached[1]

    # --- ingest: events/samples upserts ------------------------------------------------------

    def upsert_event(
        self,
        *,
        uid: str,
        device_event_id: int,
        kind: str,
        start: int,
        end: int | None,
        open_: bool,
        eat_start: int | None,
        scene: str | None,
        before: str | None,
        after: str | None,
        scene_k: int | None = None,
        subjects: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Insert-or-update the non-identity columns only. `cat`/`identity_status`/
        `confidence`/`reviewed` are never touched here -- only classification/review writes
        those, and a human review must survive every later poll of the same still-open (or
        re-seen) event. `kind` follows the device: an open track is first seen as a visit and
        only becomes an eat once the cat has stayed at the bowl long enough.

        `before`/`after` are bracket photos, fetched once and never revisited -- an
        already-durable value is kept even when `excluded` carries something else. `scene`,
        unlike them, is meant to be replaced: `librefeedd` keeps rewriting it under a NEW
        filename as a better sample supersedes the last one (docs/40-vision-judge.md, Contract
        4), so it is always set to whatever this call was given -- `ingest.py` has already
        decided, before calling this, whether the device's current scene name is genuinely new
        (and if so, fetched it) or unchanged (and passed the existing value straight back
        through)."""
        now = _now()
        subjects_json = (
            json.dumps([dict(subject) for subject in subjects], separators=(",", ":"))
            if subjects is not None else None
        )
        self.conn.execute(
            """
            INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start,
                                scene, scene_k, subjects, before, after, reviewed, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(uid) DO UPDATE SET
                kind=CASE WHEN events.reviewed=1 THEN events.kind ELSE excluded.kind END,
                end=excluded.end, open=excluded.open, eat_start=excluded.eat_start,
                scene=excluded.scene,
                scene_k=COALESCE(excluded.scene_k, events.scene_k),
                subjects=COALESCE(excluded.subjects, events.subjects),
                before=COALESCE(events.before, excluded.before),
                after=COALESCE(events.after, excluded.after),
                updated=excluded.updated
            """,
            (uid, device_event_id, kind, start, end, int(open_), eat_start, scene, scene_k, subjects_json, before, after, now),
        )
        self.conn.commit()

    def close_orphaned_open(self, live_open_uids: Collection[str]) -> int:
        """Closes every row still marked open whose session the feeder no longer reports as
        open. `live_open_uids` are the top-level uids of the tracks open in the feeder's
        CURRENT event list; anything else open here can never be closed by `upsert_event`,
        because its track is no longer in that list at all: the feeder restarted mid-track
        (an open track is not carried across a restart), or it aged out of the feeder's
        bounded log before a poll saw it closed. Left alone, such a row kept its cat "present"
        indefinitely (Kitty, 2026-11-22 to 11-23, 24 h). Ends at its last photo, else its start.
        Returns how many rows were closed."""
        rows = self.conn.execute("SELECT uid, parent_uid FROM events WHERE open=1").fetchall()
        orphans = [r["uid"] for r in rows if (r["parent_uid"] or r["uid"]) not in live_open_uids]
        now = _now()
        for uid in orphans:
            self.conn.execute(
                "UPDATE events SET open=0, updated=?, end=COALESCE("
                "(SELECT MAX(t) FROM samples WHERE event_uid=events.uid), end, start) WHERE uid=?",
                (now, uid),
            )
        if orphans:
            self.conn.commit()
        return len(orphans)

    def event_asset_fields(self, uid: str) -> dict[str, str | None] | None:
        row = self.conn.execute(
            "SELECT scene, before, after FROM events WHERE uid=?", (uid,)
        ).fetchone()
        return dict(row) if row is not None else None

    # --- eating clips (docs/39-eating-clips.md) ---------------------------------------------

    def set_event_clip(self, uid: str, *, clip_id: str, clip_start_ms: int, clip_end_ms: int) -> None:
        """Links one TOP-LEVEL event to the Scrypted clip that recorded it --
        `eating_clips.ClipLinker`'s own successful-lookup write. A no-op if `uid` no longer
        exists (the event aged out under retention while a retry was still pending)."""
        now = _now()
        self.conn.execute(
            "UPDATE events SET clip_id=?, clip_start_ms=?, clip_end_ms=?, updated=? WHERE uid=?",
            (clip_id, clip_start_ms, clip_end_ms, now, uid),
        )
        self.conn.commit()

    def clear_event_clip(self, uid: str) -> None:
        """Un-links a clip `views.KibbleClipView` discovered is gone (Scrypted's own quota
        cleanup, `docs/39-eating-clips.md`'s "findings" section) -- `uid` here is always the
        OWNING row (`resolve_event_clip`'s own `owner_uid`), never a split child that only
        ever resolved one through its parent."""
        now = _now()
        self.conn.execute(
            "UPDATE events SET clip_id=NULL, clip_start_ms=NULL, clip_end_ms=NULL, updated=? WHERE uid=?",
            (now, uid),
        )
        self.conn.commit()

    def resolve_event_clip(self, uid: str) -> dict[str, Any] | None:
        """The clip actually backing one event, resolved through its parent when `uid` is
        itself a split child (`docs/39-eating-clips.md`: "split children resolve the clip
        through their parent"). `None` for an unknown event, or one with no clip linked (yet,
        or ever) -- `views.KibbleClipView`'s own gate against streaming anything for an
        arbitrary id: nothing is ever proxied to Scrypted unless this resolves to a real,
        previously-linked clip."""
        row = self.conn.execute(
            "SELECT uid, clip_id, clip_start_ms, clip_end_ms, parent_uid FROM events WHERE uid=?", (uid,)
        ).fetchone()
        if row is None:
            return None
        if row["clip_id"] is None and row["parent_uid"] is not None:
            row = self.conn.execute(
                "SELECT uid, clip_id, clip_start_ms, clip_end_ms FROM events WHERE uid=?",
                (row["parent_uid"],),
            ).fetchone()
            if row is None:
                return None
        if row["clip_id"] is None:
            return None
        return {
            "owner_uid": row["uid"],
            "clip_id": row["clip_id"],
            "start_ms": row["clip_start_ms"],
            "end_ms": row["clip_end_ms"],
        }

    def events_needing_clip_link(self, cutoff: int) -> list[tuple[str, int, int]]:
        """Closed, unlinked top-level eat sessions since `cutoff` -- the restart-survival half
        of linking (`docs/39-eating-clips.md`): a retry schedule lives only in process memory,
        so a restart mid-arc must never silently strand an event with no clip forever. Bounded
        to `kind='eat'` (nothing else is ever linked) and `parent_uid IS NULL` (a split child
        never carries its own link -- see `resolve_event_clip`)."""
        rows = self.conn.execute(
            "SELECT uid, start, end FROM events "
            "WHERE kind='eat' AND open=0 AND clip_id IS NULL AND parent_uid IS NULL AND start>=?",
            (cutoff,),
        ).fetchall()
        return [(r["uid"], r["start"], r["end"]) for r in rows if r["end"] is not None]

    # --- vision judge (docs/40-vision-judge.md) -----------------------------------------------

    def events_needing_judge(self, cutoff: int, limit: int) -> list[str]:
        """Closed, unreviewed, unhidden `eat`/`visit` events since `cutoff`, newest first,
        capped at `limit` -- the startup backfill sweep's own candidate list
        (`judge.VisionJudge.async_backfill_eligible`). Coarse on purpose: the fine-grained
        eligibility gate (a visit needs a usable thumb and an unconfident identity; an event
        already judged against its current evidence is skipped) needs sample data this query
        does not fetch, and lives in `judge.py` instead, right next to the same crop-selection
        logic it must stay consistent with."""
        rows = self.conn.execute(
            "SELECT uid FROM events WHERE kind IN ('eat','visit') AND open=0 AND reviewed=0 "
            "AND hidden=0 AND start>=? ORDER BY start DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [r["uid"] for r in rows]

    def judge_event_context(self, uid: str) -> dict[str, Any] | None:
        """Everything `judge.VisionJudge` needs to decide eligibility and, once a verdict comes
        back, to apply it -- read fresh both before the (slow) model request and again right
        before writing, since a human review or a fresh local reclassification can land in
        between. `sample_count` is every sample the feeder kept for the event, trustworthy crop or
        not -- how much body evidence the device itself saw."""
        row = self.conn.execute(
            "SELECT kind, open, reviewed, hidden, scene, before, after, cat, identity_status, "
            "confidence, judge_evidence, "
            "(SELECT COUNT(*) FROM samples WHERE samples.event_uid = events.uid) AS sample_count "
            "FROM events WHERE uid=?",
            (uid,),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["kind"], "open": bool(row["open"]), "reviewed": bool(row["reviewed"]),
            "hidden": bool(row["hidden"]), "scene": row["scene"], "before": row["before"],
            "after": row["after"], "cat": row["cat"], "identity_status": row["identity_status"],
            "confidence": row["confidence"], "judge_evidence": row["judge_evidence"],
            "sample_count": row["sample_count"],
        }

    def event_sample_candidates(self, event_uid: str) -> list[ThumbCandidate]:
        """The same per-sample candidates `pick_thumb`/`review_thumb` rank thumbnails from --
        exposed for `judge.select_judge_crops`, which ranks the *judge's* own up-to-3-best-by-
        score crops from exactly the same rows (Contract 2), and for the visit-eligibility
        "would this show in the timeline" check (`pick_thumb` returning `None`)."""
        return self._thumb_candidates(event_uid)

    def apply_judge_verdict(
        self,
        uid: str,
        *,
        model: str,
        evidence: str,
        present: bool,
        cat: str,
        confidence: float,
        multiple: bool,
        reason: str,
        apply_identity: bool,
        new_cat: str | None = None,
        new_identity_status: str | None = None,
        new_confidence: float | None = None,
    ) -> bool:
        """Persists one verdict's `judge_*` columns (Contract 1, verbatim -- `cat` here is
        exactly the verdict's own "none"/"unknown"/enrolled-name string, never translated) and,
        only when the caller has already decided rule (a) or (b) applies (`apply_identity`),
        the live `cat`/`identity_status`/`confidence` fields too -- one statement, gated on
        `reviewed=0` so a human review racing this write always wins and is never overwritten.
        `apply_identity=False` is rule (c): record the verdict only. Returns whether the row
        was actually updated (`False` if `uid` was reviewed, or gone, in the meantime)."""
        now = _now()
        if apply_identity:
            cur = self.conn.execute(
                "UPDATE events SET judge_present=?, judge_cat=?, judge_confidence=?, "
                "judge_multiple=?, judge_reason=?, judge_model=?, judge_at=?, judge_evidence=?, "
                "cat=?, identity_status=?, confidence=?, updated=? WHERE uid=? AND reviewed=0",
                (
                    int(present), cat, confidence, int(multiple), reason, model, now, evidence,
                    new_cat, new_identity_status, new_confidence, now, uid,
                ),
            )
        else:
            cur = self.conn.execute(
                "UPDATE events SET judge_present=?, judge_cat=?, judge_confidence=?, "
                "judge_multiple=?, judge_reason=?, judge_model=?, judge_at=?, judge_evidence=?, "
                "updated=? WHERE uid=? AND reviewed=0",
                (int(present), cat, confidence, int(multiple), reason, model, now, evidence, now, uid),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def invalidate_scene_asset(self, uid: str, old_asset_id: str | None) -> None:
        """After `ingest.py` has already durably written a replacement scene (Contract 4):
        removes the superseded HA copy, but only if no other row -- another event's own scene/
        before/after, a sample's body/face, or a feed's before/after -- still references the
        same asset id, and clears this event's stored judge evidence so the changed scene makes
        it eligible for exactly one re-judge."""
        if old_asset_id and not self._asset_referenced(old_asset_id, exclude_event_uid=uid):
            self.delete_media(old_asset_id)
        self.conn.execute("UPDATE events SET judge_evidence=NULL WHERE uid=?", (uid,))
        self.conn.commit()

    def _asset_referenced(self, asset_id: str, *, exclude_event_uid: str | None = None) -> bool:
        event_sql = "SELECT 1 FROM events WHERE (scene=? OR before=? OR after=?)"
        args: list[Any] = [asset_id, asset_id, asset_id]
        if exclude_event_uid is not None:
            event_sql += " AND uid != ?"
            args.append(exclude_event_uid)
        if self.conn.execute(event_sql, args).fetchone() is not None:
            return True
        if self.conn.execute(
            "SELECT 1 FROM samples WHERE body=? OR face=?", (asset_id, asset_id)
        ).fetchone() is not None:
            return True
        return self.conn.execute(
            "SELECT 1 FROM feeds WHERE before=? OR after=?", (asset_id, asset_id)
        ).fetchone() is not None

    def cats_needing_description(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM cats WHERE description IS NULL OR description=''"
        ).fetchall()
        return [r["name"] for r in rows]

    def set_cat_description(self, cat: str, description: str) -> None:
        self.conn.execute("UPDATE cats SET description=? WHERE name=?", (description, cat))
        self.conn.commit()

    def cat_descriptions(self) -> dict[str, str | None]:
        rows = self.conn.execute("SELECT name, description FROM cats").fetchall()
        return {r["name"]: r["description"] for r in rows}

    def training_crop_for_mode(self, cat: str, mode: str) -> dict[str, Any] | None:
        """The newest training crop of `cat` in `mode` (`identity.MODE_DAY`/`MODE_IR`) with an
        actual body image, or `None` -- `judge.VisionJudge.ensure_descriptions`'s "prefer one
        day and one IR" crop selection (Contract 3), one mode at a time."""
        row = self.conn.execute(
            "SELECT uid, body FROM training WHERE cat=? AND mode=? AND body IS NOT NULL "
            "ORDER BY created DESC LIMIT 1",
            (cat, mode),
        ).fetchone()
        return dict(row) if row is not None else None

    def judge_diagnostics(self) -> dict[str, Any]:
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE judge_at IS NOT NULL"
        ).fetchone()["n"]
        present_true = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE judge_at IS NOT NULL AND judge_present=1"
        ).fetchone()["n"]
        present_false = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE judge_at IS NOT NULL AND judge_present=0"
        ).fetchone()["n"]
        identity_fixed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE judge_at IS NOT NULL AND judge_cat IS NOT NULL AND judge_cat=cat"
        ).fetchone()["n"]
        return {
            "total_judged": total,
            "cat_present_true": present_true,
            "cat_present_false": present_false,
            "identity_fixed": identity_fixed,
        }

    def existing_sample_uids(self, event_uid: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT uid FROM samples WHERE event_uid=?", (event_uid,)
        ).fetchall()
        return {r["uid"] for r in rows}

    def session_uid_for_sample(self, sample_uid: str) -> str | None:
        row = self.conn.execute(
            "SELECT COALESCE(events.parent_uid, events.uid) AS session_uid "
            "FROM samples JOIN events ON events.uid=samples.event_uid WHERE samples.uid=?",
            (sample_uid,),
        ).fetchone()
        return row["session_uid"] if row is not None else None

    def insert_sample(
        self,
        *,
        uid: str,
        event_uid: str,
        t: int,
        body: str | None,
        face: str | None,
        features: identity.Features,
        box: tuple[float, float, float, float] | None,
        score: float | None,
        sid: int | None = None,
        frame_k: int | None = None,
        is_primary: bool = True,
        bowl: bool | None = None,
        frame_boxes: Sequence[Sequence[int | float | None]] | None = None,
    ) -> None:
        box_x1, box_y1, box_x2, box_y2 = box if box is not None else (None, None, None, None)
        frame_boxes_json = (
            json.dumps(frame_boxes, separators=(",", ":")) if frame_boxes is not None else None
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO samples(
                uid, event_uid, t, body, face, face_emb, body_feat, face_feat, mode,
                box_x1, box_y1, box_x2, box_y2, score, sid, frame_k, is_primary, bowl, frame_boxes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid, event_uid, t, body, face,
                identity.pack(features.face_emb), identity.pack(features.body_feat),
                identity.pack(features.face_feat), features.mode,
                box_x1, box_y1, box_x2, box_y2, score, sid, frame_k, int(is_primary),
                None if bowl is None else int(bowl), frame_boxes_json,
            ),
        )
        self.conn.commit()

    def features_for_event(self, event_uid: str) -> list[identity.Features]:
        rows = self.conn.execute(
            "SELECT face_emb, body_feat, face_feat, mode FROM samples WHERE event_uid=? ORDER BY t",
            (event_uid,),
        ).fetchall()
        return [
            identity.Features(
                face_emb=identity.unpack(r["face_emb"]),
                body_feat=identity.unpack(r["body_feat"]),
                face_feat=identity.unpack(r["face_feat"]),
                mode=r["mode"],
            )
            for r in rows
        ]

    def _family_uids(self, uid: str) -> list[str]:
        """`uid` plus every existing child part of this session. Shared by session detail,
        identity scoring, and replanning so every family-wide sample query uses the exact same
        membership."""
        children = self.conn.execute("SELECT uid FROM events WHERE parent_uid=?", (uid,)).fetchall()
        return [uid, *(c["uid"] for c in children)]

    def vision_cats_for_event(self, device_event_id: int) -> dict[int, str | None]:
        """docs/42-multi-cat.md's own `kibble/vision/last` enrichment: `{sid: cat}` for every
        sid-tagged sample of the NEWEST OPEN top-level session whose `device_event_id` matches
        the feeder's own live `event_id`, across that session's WHOLE family. A sid whose
        owning event's identity is not yet `auto`/`reviewed` (or a sid with no matching sample
        at all) maps to `None` -- "no confident name yet", never an error. `{}` when no such
        open session has been ingested at all (a poll landing ahead of ingest, or a stale/
        already-closed `event_id`)."""
        session = self.conn.execute(
            "SELECT uid FROM events WHERE device_event_id=? AND parent_uid IS NULL AND open=1 "
            "ORDER BY start DESC LIMIT 1",
            (device_event_id,),
        ).fetchone()
        if session is None:
            return {}
        family_uids = self._family_uids(session["uid"])
        placeholder = ",".join("?" for _ in family_uids)
        rows = self.conn.execute(
            f"SELECT s.sid AS sid, e.cat AS cat, e.identity_status AS identity_status "
            f"FROM samples s JOIN events e ON e.uid = s.event_uid "
            f"WHERE s.event_uid IN ({placeholder}) AND s.sid IS NOT NULL",
            family_uids,
        ).fetchall()
        return {r["sid"]: (r["cat"] if r["identity_status"] in ("auto", "reviewed") else None) for r in rows}

    def features_by_sid_for_family(self, uid: str) -> dict[int, list[identity.Features]]:
        """Features for each subject id across the full session family, used by the identity
        engine's per-subject session scoring. Sid-less photos are not included in a subject
        group; the session planner keeps them as individual photos instead."""
        family_uids = self._family_uids(uid)
        placeholder = ",".join("?" for _ in family_uids)
        rows = self.conn.execute(
            f"SELECT sid, face_emb, body_feat, face_feat, mode FROM samples "
            f"WHERE event_uid IN ({placeholder}) AND sid IS NOT NULL ORDER BY t",
            family_uids,
        ).fetchall()
        out: dict[int, list[identity.Features]] = {}
        for r in rows:
            out.setdefault(r["sid"], []).append(
                identity.Features(
                    face_emb=identity.unpack(r["face_emb"]), body_feat=identity.unpack(r["body_feat"]),
                    face_feat=identity.unpack(r["face_feat"]), mode=r["mode"],
                )
            )
        return out

    def sample_uids_ordered(self, event_uid: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT uid FROM samples WHERE event_uid=? ORDER BY t", (event_uid,)
        ).fetchall()
        return [r["uid"] for r in rows]

    def set_event_classification(
        self,
        uid: str,
        *,
        cat: str | None,
        identity_status: str | None,
        confidence: float | None,
        per_sample_uids: Sequence[str],
        per_sample: Sequence[tuple[str | None, float | None]],
    ) -> bool:
        """Applies a fresh `Verdict` to an unreviewed event -- never called for a reviewed
        one; see `store.py`'s callers. Writes only what actually differs: a relearn re-asks the
        model about every unreviewed event and most answers do not change, so the unchanged
        ones cost no write, no commit and no `updated` bump. Returns `True` when the event's
        NAME changed (a new cat, or none) -- the number the learning summary reports."""
        row = self.conn.execute(
            "SELECT cat, identity_status, confidence, reviewed FROM events WHERE uid=?", (uid,)
        ).fetchone()
        if row is None:
            return False
        changed = renamed = False
        if not row["reviewed"] and (row["cat"], row["identity_status"], row["confidence"]) != (
            cat, identity_status, confidence
        ):
            self.conn.execute(
                "UPDATE events SET cat=?, identity_status=?, confidence=?, updated=? "
                "WHERE uid=? AND reviewed=0",
                (cat, identity_status, confidence, _now(), uid),
            )
            changed = True
            renamed = row["cat"] != cat
        current = {
            r["uid"]: (r["guess"], r["guess_confidence"])
            for r in self.conn.execute(
                "SELECT uid, guess, guess_confidence FROM samples WHERE event_uid=?", (uid,)
            )
        }
        for sample_uid, (guess, guess_confidence) in zip(per_sample_uids, per_sample, strict=False):
            if current.get(sample_uid) != (guess, guess_confidence):
                self.conn.execute(
                    "UPDATE samples SET guess=?, guess_confidence=? WHERE uid=?",
                    (guess, guess_confidence, sample_uid),
                )
                changed = True
        if changed:
            self.conn.commit()
        return renamed

    def upsert_feed(
        self,
        *,
        uid: str,
        device_feed_id: str,
        ts: int,
        portions: float | None,
        scheduled: bool,
        confirmed: bool,
        before: str | None,
        after: str | None,
        amount1: int | None,
        amount2: int | None,
        food1: str | None,
        food2: str | None,
        single: bool | None,
    ) -> None:
        """`amount1`/`amount2`/`food1`/`food2`/`single` are the per-feed facts recorded at
        ingest (docs/37-hopper-full.md, "historical accuracy"): set once, on this row's first
        INSERT, from the device record and the coordinator's state at that moment, and never
        touched again on a later re-upsert of the same `uid` -- a divider flip or a food
        rename afterward must not rewrite what already happened. `portions`/`scheduled`/
        `confirmed`/`before`/`after` keep updating on every upsert exactly as before this table
        gained the five new columns."""
        self.conn.execute(
            """
            INSERT INTO feeds(uid, device_feed_id, ts, portions, scheduled, confirmed, before,
                               after, amount1, amount2, food1, food2, single)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                portions=excluded.portions,
                scheduled=excluded.scheduled, confirmed=excluded.confirmed,
                before=COALESCE(feeds.before, excluded.before),
                after=COALESCE(feeds.after, excluded.after)
            """,
            (
                uid, device_feed_id, ts, portions, int(scheduled), int(confirmed), before, after,
                amount1, amount2, food1, food2, None if single is None else int(single),
            ),
        )
        self.conn.commit()

    def feed_asset_fields(self, uid: str) -> dict[str, str | None] | None:
        row = self.conn.execute("SELECT before, after FROM feeds WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row is not None else None

    # --- bowl-fill estimate learning (bowl_fill.py) -----------------------------------------

    def get_bowl_fill_learning(self, bucket: str) -> tuple[float, int] | None:
        """The learned `(fill_per_portion, samples)` for one hopper bucket
        (`"hopper1"`/`"hopper2"`), or `None` if that bucket has never learned a real sample --
        stored in the generic `meta` table rather than a dedicated table, since it is exactly
        the same shape of small, keyed, rarely-written value `schema_version` already lives in
        there as. A row whose value fails to parse (a hand-edited or corrupted database) is
        treated the same as no row at all, never as a crash."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"bowl_fill_per_portion:{bucket}",)
        ).fetchone()
        if row is None:
            return None
        try:
            fpp_text, samples_text = row["value"].split(",", 1)
            return (float(fpp_text), int(samples_text))
        except (ValueError, IndexError):
            return None

    def set_bowl_fill_learning(self, bucket: str, fill_per_portion: float, samples: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"bowl_fill_per_portion:{bucket}", f"{fill_per_portion!r},{samples}"),
        )
        self.conn.commit()

    # --- reclassification candidates --------------------------------------------------------

    def events_for_reclassify(self, cutoff: int) -> list[str]:
        """The unreviewed TOP-LEVEL events inside retention. A split session's child parts are
        left out on purpose: they are rebuilt from their parent, and re-planning from a child
        sees a family of one, which un-splits the session -- the next pass would split it again
        and every relearn would churn the same parts."""
        rows = self.conn.execute(
            "SELECT uid FROM events WHERE reviewed=0 AND parent_uid IS NULL AND start>=?", (cutoff,)
        ).fetchall()
        return [r["uid"] for r in rows]

    def all_training_features(self) -> list[tuple[str, identity.Features]]:
        """Every `training` row's own features, keyed by cat -- the identity engine's gallery
        (`identity.Model`). A `label`/`auto` row whose SOURCE sample (`training.uid` minus its
        trailing `-train`) turns out to have an untrustworthy legacy crop is skipped here --
        never deleted, never re-evaluated by `async_maybe_auto_learn`'s own (creation-time,
        auto-only) gate, just left out of the classifier's own feature space on every rebuild.
        `import`/`upload` rows have no such source sample to distrust and are always kept."""
        rows = self.conn.execute(
            "SELECT uid, cat, source, face_emb, body_feat, face_feat, mode FROM training"
        ).fetchall()
        out = []
        for r in rows:
            if r["source"] in ("label", "auto") and not self._training_row_crop_trustworthy(r["uid"]):
                continue
            out.append((
                r["cat"],
                identity.Features(
                    face_emb=identity.unpack(r["face_emb"]),
                    body_feat=identity.unpack(r["body_feat"]),
                    face_feat=identity.unpack(r["face_feat"]),
                    mode=r["mode"],
                ),
            ))
        return out

    def _training_row_crop_trustworthy(self, training_uid: str) -> bool:
        """Whether the device sample a `label`/`auto` training row was copied from (its own
        uid, `training_uid` minus the trailing `-train`) had a trustworthy crop. `True` (kept)
        when that sample has since aged out of retention and is simply gone: routine purge
        proves nothing about crop quality, so an unprovable row is never punished for it."""
        sample_uid = training_uid.removesuffix("-train")
        row = self.conn.execute(
            "SELECT t, box_x1, box_y1, box_x2, box_y2 FROM samples WHERE uid=?", (sample_uid,)
        ).fetchone()
        if row is None:
            return True
        box = None
        if row["box_x1"] is not None:
            box = (row["box_x1"], row["box_y1"], row["box_x2"], row["box_y2"])
        return crop_geometry.is_legacy_crop_trustworthy(box, row["t"])

    # --- Coral embedding cache (schema v8, docs/41-coral-recognition.md) ---------------------

    def coral_embedding(self, row_kind: str, row_uid: str, crop_kind: str, model_id: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT embedding FROM coral_embeddings WHERE row_kind=? AND row_uid=? AND crop_kind=? AND model_id=?",
            (row_kind, row_uid, crop_kind, model_id),
        ).fetchone()
        return row["embedding"] if row is not None else None

    def set_coral_embedding(
        self, row_kind: str, row_uid: str, crop_kind: str, model_id: str, embedding: bytes
    ) -> None:
        self.set_coral_embeddings(row_kind, [(row_uid, crop_kind, model_id, embedding)])

    def set_coral_embeddings(
        self, row_kind: str, items: Sequence[tuple[str, str, str, bytes]]
    ) -> None:
        """Idempotent: "each image is embedded exactly once" (docs/41-coral-recognition.md) --
        an embedding is a pure function of one crop's own bytes and the model that produced
        it, so a second write for the same key is always the identical vector and is silently
        dropped rather than overwritten. `items` are `(row_uid, crop_kind, model_id,
        embedding)`; a whole batch is ONE transaction, because each commit waits for the disk
        and a batch used to cost one per vector."""
        now = _now()
        self.conn.executemany(
            "INSERT INTO coral_embeddings(row_kind, row_uid, crop_kind, model_id, embedding, created) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(row_kind, row_uid, crop_kind, model_id) DO NOTHING",
            [(row_kind, uid, crop, model, blob, now) for uid, crop, model, blob in items],
        )
        self.conn.commit()

    def _read_asset(self, asset_id: str | None) -> bytes | None:
        """Reads any asset this entry owns, `media/...` or `training/...` alike -- the same
        root-relative resolution `resolve_asset_path` (the HTTP-view-facing lookup) uses,
        reused here for the Coral backfill's own crop reads."""
        if not asset_id:
            return None
        path = resolve_asset_path(self.root, asset_id)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def training_rows_needing_coral(self, body_model: str, face_model: str, limit: int) -> list[dict[str, Any]]:
        """Up to `limit` TRUSTWORTHY training rows (same source-crop-trustworthy filter as
        `all_training_features`) still missing a cached Coral embedding for the currently
        configured models, newest first, each carrying whichever crop's bytes still need
        embedding (`None` when that crop is absent or already cached) -- `CoralRecognizer`'s
        rebuild/backfill "ensure embedded" pass reads this to know what to embed next."""
        rows = self.conn.execute(
            "SELECT uid, source, body, face FROM training ORDER BY created DESC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            if len(out) >= limit:
                break
            if r["source"] in ("label", "auto") and not self._training_row_crop_trustworthy(r["uid"]):
                continue
            need_body = (
                r["body"] is not None and self.coral_embedding("training", r["uid"], "body", body_model) is None
            )
            need_face = (
                r["face"] is not None and self.coral_embedding("training", r["uid"], "face", face_model) is None
            )
            if not need_body and not need_face:
                continue
            out.append({
                "uid": r["uid"],
                "body_bytes": self._read_asset(r["body"]) if need_body else None,
                "face_bytes": self._read_asset(r["face"]) if need_face else None,
            })
        return out

    def samples_needing_coral(self, body_model: str, face_model: str, limit: int) -> list[dict[str, Any]]:
        """Up to `limit` samples of a still-reclassifiable (unreviewed) event, newest first,
        still missing a cached Coral embedding -- a REVIEWED event is frozen and never
        reclassified (`events_for_reclassify`'s own `WHERE reviewed=0`), so embedding its
        samples would serve no operational purpose. Part of the same backfill pass as
        `training_rows_needing_coral`; classify-time catch-up for one specific event instead
        uses `samples_needing_coral_for_event`."""
        rows = self.conn.execute(
            "SELECT s.uid AS uid, s.body AS body, s.face AS face FROM samples s "
            "JOIN events e ON e.uid = s.event_uid WHERE e.reviewed=0 ORDER BY s.t DESC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            if len(out) >= limit:
                break
            need_body = (
                r["body"] is not None and self.coral_embedding("sample", r["uid"], "body", body_model) is None
            )
            need_face = (
                r["face"] is not None and self.coral_embedding("sample", r["uid"], "face", face_model) is None
            )
            if not need_body and not need_face:
                continue
            out.append({
                "uid": r["uid"],
                "body_bytes": self._read_asset(r["body"]) if need_body else None,
                "face_bytes": self._read_asset(r["face"]) if need_face else None,
            })
        return out

    def samples_needing_coral_for_event(
        self, event_uid: str, body_model: str, face_model: str
    ) -> list[dict[str, Any]]:
        """Every sample of `event_uid` still missing a cached Coral embedding, unconditionally
        (the caller already chose to classify this exact event) -- `CoralRecognizer.
        async_classify_event`'s own classify-time catch-up."""
        rows = self.conn.execute(
            "SELECT uid, body, face FROM samples WHERE event_uid=? ORDER BY t", (event_uid,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            need_body = (
                r["body"] is not None and self.coral_embedding("sample", r["uid"], "body", body_model) is None
            )
            need_face = (
                r["face"] is not None and self.coral_embedding("sample", r["uid"], "face", face_model) is None
            )
            if not need_body and not need_face:
                continue
            out.append({
                "uid": r["uid"],
                "body_bytes": self._read_asset(r["body"]) if need_body else None,
                "face_bytes": self._read_asset(r["face"]) if need_face else None,
            })
        return out

    def all_training_coral_features(
        self, body_model: str, face_model: str
    ) -> list[tuple[str, coral_identity.CoralFeatures]]:
        """Mirrors `all_training_features` (same source-crop-trustworthy filter) but reads
        cached Coral embeddings instead of the histogram BLOBs -- `CoralRecognizer.
        async_rebuild`'s own gallery. A row with neither crop embedded yet contributes
        nothing (skipped, not a zero vector) -- exactly `all_training_features`'s own
        `identity.Features` shape, where a missing modality is `None`, never a fabricated
        value."""
        rows = self.conn.execute("SELECT uid, cat, source, body, face, mode FROM training").fetchall()
        out: list[tuple[str, coral_identity.CoralFeatures]] = []
        for r in rows:
            if r["source"] in ("label", "auto") and not self._training_row_crop_trustworthy(r["uid"]):
                continue
            body_blob = self.coral_embedding("training", r["uid"], "body", body_model) if r["body"] else None
            face_blob = self.coral_embedding("training", r["uid"], "face", face_model) if r["face"] else None
            if body_blob is None and face_blob is None:
                continue
            out.append((
                r["cat"],
                coral_identity.CoralFeatures(
                    body_emb=identity.unpack(body_blob), face_emb=identity.unpack(face_blob), mode=r["mode"]
                ),
            ))
        return out

    def coral_features_for_event(
        self, event_uid: str, body_model: str, face_model: str
    ) -> list[coral_identity.CoralFeatures]:
        """One `CoralFeatures` per sample of `event_uid`, in capture order -- mirrors
        `features_for_event`'s own shape, for `CoralRecognizer.async_classify_event`."""
        rows = self.conn.execute(
            "SELECT uid, body, face, mode FROM samples WHERE event_uid=? ORDER BY t", (event_uid,)
        ).fetchall()
        out: list[coral_identity.CoralFeatures] = []
        for r in rows:
            body_blob = self.coral_embedding("sample", r["uid"], "body", body_model) if r["body"] else None
            face_blob = self.coral_embedding("sample", r["uid"], "face", face_model) if r["face"] else None
            out.append(coral_identity.CoralFeatures(
                body_emb=identity.unpack(body_blob), face_emb=identity.unpack(face_blob), mode=r["mode"]
            ))
        return out

    def coral_features_by_sid_for_family(
        self, uid: str, body_model: str, face_model: str
    ) -> dict[int, list[coral_identity.CoralFeatures]]:
        """Coral-backed mirror of `features_by_sid_for_family` for per-subject session scoring."""
        family_uids = self._family_uids(uid)
        placeholder = ",".join("?" for _ in family_uids)
        rows = self.conn.execute(
            f"SELECT uid, sid, body, face, mode FROM samples "
            f"WHERE event_uid IN ({placeholder}) AND sid IS NOT NULL ORDER BY t",
            family_uids,
        ).fetchall()
        out: dict[int, list[coral_identity.CoralFeatures]] = {}
        for r in rows:
            body_blob = self.coral_embedding("sample", r["uid"], "body", body_model) if r["body"] else None
            face_blob = self.coral_embedding("sample", r["uid"], "face", face_model) if r["face"] else None
            out.setdefault(r["sid"], []).append(coral_identity.CoralFeatures(
                body_emb=identity.unpack(body_blob), face_emb=identity.unpack(face_blob), mode=r["mode"]
            ))
        return out

    # --- thumbnail / row shaping -------------------------------------------------------------

    def _thumb_candidates(self, event_uid: str) -> list[ThumbCandidate]:
        """Every TRUSTWORTHY sample of `event_uid`, in capture order -- the single source of
        truth `pick_thumb`/`review_thumb` (thumbnail selection), `event_detail` (the review
        sheet's own per-photo list) and `judge.select_judge_crops` all share. A legacy sample
        whose crop `crop_geometry.is_legacy_crop_trustworthy` flags as unreliable is left out
        entirely: never offered as a thumbnail, never shown to a human for labelling, never
        sent to the vision judge -- the underlying `samples` row and its files are untouched."""
        rows = self.conn.execute(
            "SELECT uid, t, body, face, score, box_x1, box_y1, box_x2, box_y2, guess "
            "FROM samples WHERE event_uid=? ORDER BY t",
            (event_uid,),
        ).fetchall()
        out = []
        for r in rows:
            box = None
            if r["box_x1"] is not None:
                box = (r["box_x1"], r["box_y1"], r["box_x2"], r["box_y2"])
            if not crop_geometry.is_legacy_crop_trustworthy(box, r["t"]):
                continue
            out.append(
                ThumbCandidate(
                    uid=r["uid"], t=r["t"], body=r["body"], has_face=r["face"] is not None,
                    score=r["score"], box=box, guess=r["guess"], face=r["face"],
                )
            )
        return out

    def _clip_payload(self, row: sqlite3.Row) -> dict[str, int] | None:
        """The `clip` field of one event's timeline/detail payload: this row's own clip if
        directly linked, else -- for a split child -- its parent's, since the recorded clip
        spans the shared session, not one cat's own slice of it (`docs/39-eating-clips.md`:
        "split children resolve the clip through their parent"). Only ever `{start_ms,
        end_ms}`; `clip_id` (the Scrypted videoId) never leaves the store -- the card only
        ever needs its own event uid to ask `views.KibbleClipView` for the stream."""
        clip_start_ms = row["clip_start_ms"]
        clip_end_ms = row["clip_end_ms"]
        if clip_start_ms is None and row["parent_uid"] is not None:
            parent = self.conn.execute(
                "SELECT clip_start_ms, clip_end_ms FROM events WHERE uid=?", (row["parent_uid"],)
            ).fetchone()
            if parent is not None:
                clip_start_ms, clip_end_ms = parent["clip_start_ms"], parent["clip_end_ms"]
        if clip_start_ms is None or clip_end_ms is None:
            return None
        return {"start_ms": clip_start_ms, "end_ms": clip_end_ms}

    def _event_timeline_dict(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """One `events` row -> a `TimelineEvent` dict, or `None` if a "visit" has no usable
        thumb (it stays in the store for `kibble/review/photos`, just never on the timeline)."""
        candidates = self._thumb_candidates(row["uid"])
        thumb = pick_thumb(candidates)
        if row["kind"] == "visit" and thumb is None:
            return None
        thumb_asset = _asset(self.entry_id, thumb_asset_id(thumb))
        if row["kind"] == "eat" and thumb_asset is None:
            thumb_asset = _asset(self.entry_id, row["scene"])
        identity_status = row["identity_status"]
        return {
            "uid": row["uid"],
            "kind": row["kind"],
            "start": row["start"],
            "end": None if row["open"] else row["end"],
            "open": bool(row["open"]),
            "cat": row["cat"],
            "identity": identity_status,
            "confidence": row["confidence"] if identity_status == "auto" else None,
            "thumb": thumb_asset,
            "scene": _asset(self.entry_id, row["scene"]),
            "before": _asset(self.entry_id, row["before"]),
            "after": _asset(self.entry_id, row["after"]),
            "sample_count": len(candidates),
            "clip": self._clip_payload(row),
            "session": row["parent_uid"],
        }

    def _feed_timeline_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """The stored, frozen-at-ingest facts for one feed row -- `websocket.py`'s `_feed_view`
        renders these into the timeline's public `feed` shape (per-hopper `sides`, the current-
        mode fallback for a `single` recorded `None`); this dict is deliberately the raw
        storage-level shape, not that public one."""
        return {
            "uid": row["uid"],
            "kind": "feed",
            "start": row["ts"],
            "end": row["ts"],
            "open": False,
            "cat": None,
            "identity": None,
            "confidence": None,
            "thumb": None,
            "scene": None,
            "before": _asset(self.entry_id, row["before"]),
            "after": _asset(self.entry_id, row["after"]),
            "sample_count": 0,
            "feed": {
                "portions": row["portions"],
                "scheduled": bool(row["scheduled"]),
                "confirmed": bool(row["confirmed"]),
                "amount1": row["amount1"],
                "amount2": row["amount2"],
                "food1": row["food1"],
                "food2": row["food2"],
                "single": None if row["single"] is None else bool(row["single"]),
            },
        }

    # --- kibble/timeline ---------------------------------------------------------------------

    def timeline_page(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        bound = _decode_cursor(cursor) if cursor else None
        kinds_placeholder = ",".join("?" for _ in HIDDEN_TIMELINE_KINDS)
        # Over-fetch generously: a visit with no usable thumb is dropped after the fact, and a
        # short run of unusable visits must not starve the page below `limit`.
        fetch_n = max(limit * 3, limit + 20)
        items: list[dict[str, Any]] = []
        next_bound = bound
        for _round in range(6):  # bounded: never loops forever even if the table is all-unusable
            event_rows = self._page_rows(
                "events",
                "start",
                f"identity_status IS NOT 'not_a_cat' AND kind NOT IN ({kinds_placeholder}) AND hidden=0",
                tuple(HIDDEN_TIMELINE_KINDS),
                next_bound,
                fetch_n,
            )
            feed_rows = self._page_rows("feeds", "ts", "1=1", (), next_bound, fetch_n)
            merged = (
                [(r["start"], r["uid"], "event", r) for r in event_rows]
                + [(r["ts"], r["uid"], "feed", r) for r in feed_rows]
            )
            merged.sort(key=lambda t: (-t[0], t[1]))
            exhausted = len(event_rows) < fetch_n and len(feed_rows) < fetch_n
            for start, uid, src, row in merged:
                item = self._event_timeline_dict(row) if src == "event" else self._feed_timeline_dict(row)
                if item is not None:
                    items.append(item)
                    next_bound = (start, uid)
                if len(items) > limit:
                    break
            if len(items) > limit or exhausted:
                break
        has_more = len(items) > limit
        page = items[:limit]
        cursor_out = _encode_cursor(page[-1]["start"], page[-1]["uid"]) if has_more and page else None
        return {"items": page, "cursor": cursor_out, "has_more": has_more}

    def _page_rows(
        self,
        table: str,
        order_col: str,
        where: str,
        where_args: tuple[Any, ...],
        bound: tuple[int, str] | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        clauses = [where] if where else []
        args = list(where_args)
        if bound is not None:
            clauses.append(f"({order_col} < ? OR ({order_col} = ? AND uid > ?))")
            args.extend([bound[0], bound[0], bound[1]])
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order_col} DESC, uid ASC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def event_detail(self, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
        if row is None:
            return None
        session_row = self._session_row(row)
        children = self.conn.execute(
            "SELECT * FROM events WHERE parent_uid=? ORDER BY start, uid", (session_row["uid"],)
        ).fetchall()
        family_uids = self._family_uids(session_row["uid"])
        placeholders = ",".join("?" for _ in family_uids)
        sample_rows = self.conn.execute(
            f"SELECT uid FROM samples WHERE event_uid IN ({placeholders}) ORDER BY t, uid",
            family_uids,
        ).fetchall()
        samples = [self._sample_dict(sample["uid"]) for sample in sample_rows]
        cats = self._session_cats(session_row, children)

        event = self._bare_event_fields(session_row)
        event["sample_count"] = len(sample_rows)
        event["kind"] = "eat" if session_row["kind"] == "eat" or any(cat["ate"] for cat in cats) else session_row["kind"]
        thumb_candidates = [
            candidate
            for part_uid in family_uids
            for candidate in self._thumb_candidates(part_uid)
        ]
        event["thumb"] = _asset(self.entry_id, thumb_asset_id(pick_thumb(thumb_candidates)))
        if event["kind"] == "eat" and event["thumb"] is None:
            event["thumb"] = event["scene"]
        if len(cats) == 1:
            event["cat"] = cats[0]["cat"]
            event["identity"] = "reviewed" if cats[0]["confirmed"] else "auto"
            event["confidence"] = cats[0]["confidence"]
        elif len(cats) > 1:
            event["cat"] = None
            event["identity"] = "reviewed" if all(cat["confirmed"] for cat in cats) else "auto"
            event["confidence"] = None
        event["needs_ate"] = self._needs_ate(session_row, children, cats)
        return {
            "event": event,
            "samples": samples,
            "scene_subjects": self._scene_subjects(session_row),
            "cats": cats,
            "multiple_cats": bool(session_row["judge_multiple"]),
        }

    def _session_row(self, row: sqlite3.Row) -> sqlite3.Row:
        """Resolve a part uid to the top-level session row that owns its shared media."""
        if row["parent_uid"] is None:
            return row
        parent = self.conn.execute("SELECT * FROM events WHERE uid=?", (row["parent_uid"],)).fetchone()
        return parent if parent is not None else row

    def _session_cats(
        self, session_row: sqlite3.Row, children: Sequence[sqlite3.Row]
    ) -> list[dict[str, Any]]:
        parts = children or [session_row]
        by_cat: dict[str, dict[str, Any]] = {}
        for part in parts:
            cat = part["cat"]
            if not cat or cat in (_NOT_A_CAT, LABEL_UNKNOWN, LABEL_SKIP):
                continue
            photos = self.conn.execute(
                "SELECT COUNT(*) FROM samples WHERE event_uid=?", (part["uid"],)
            ).fetchone()[0]
            current = by_cat.get(cat)
            if current is None:
                by_cat[cat] = {
                    "cat": cat,
                    "ate": part["kind"] == "eat",
                    "confirmed": bool(part["reviewed"] and part["identity_status"] == "reviewed"),
                    "confidence": part["confidence"] if part["identity_status"] == "auto" else None,
                    "photos": photos,
                    "start": part["start"],
                }
            else:
                current["ate"] = current["ate"] or part["kind"] == "eat"
                current["confirmed"] = current["confirmed"] or bool(
                    part["reviewed"] and part["identity_status"] == "reviewed"
                )
                if current["confidence"] is None:
                    current["confidence"] = part["confidence"]
                current["photos"] += photos
                current["start"] = min(current["start"], part["start"])
        cats = list(by_cat.values())
        cats.sort(key=lambda item: (not item["ate"], item["start"], item["cat"]))
        return [
            {key: cat[key] for key in ("cat", "ate", "confirmed", "confidence", "photos")}
            for cat in cats
        ]

    def _scene_subject_label(
        self,
        photo_review: str | None,
        subject_review: str | None,
        owner: sqlite3.Row | None,
    ) -> tuple[str | None, bool]:
        if photo_review not in (None, LABEL_SKIP, LABEL_UNKNOWN, LABEL_BOTH):
            return photo_review, True
        if subject_review not in (None, LABEL_SKIP, LABEL_UNKNOWN, LABEL_BOTH):
            return subject_review, True
        if owner is None:
            return None, False
        if owner["identity_status"] == _NOT_A_CAT:
            return _NOT_A_CAT, bool(owner["reviewed"])
        if owner["cat"]:
            return owner["cat"], bool(owner["reviewed"])
        return None, bool(owner["reviewed"])

    def _scene_subjects(self, session_row: sqlite3.Row) -> list[dict[str, Any]]:
        """Map the shared scene's boxes to photo, subject, or part labels."""
        scene_k = session_row["scene_k"]
        if scene_k is None:
            return []
        family_uids = self._family_uids(session_row["uid"])
        placeholders = ",".join("?" for _ in family_uids)
        rows = self.conn.execute(
            f"SELECT uid, sid, box_x1, box_y1, box_x2, box_y2, event_uid, review, review_src, frame_boxes "
            f"FROM samples WHERE event_uid IN ({placeholders}) AND frame_k=?",
            (*family_uids, scene_k),
        ).fetchall()
        subject_reviews: dict[int, str] = {}
        for review_row in self.conn.execute(
            f"SELECT sid, review FROM samples WHERE event_uid IN ({placeholders}) "
            "AND sid IS NOT NULL AND review_src='subject' AND review IS NOT NULL",
            family_uids,
        ):
            subject_reviews[review_row["sid"]] = review_row["review"]
        owners: dict[str, sqlite3.Row | None] = {}
        subjects: dict[int | str, dict[str, Any]] = {}
        frame_boxes_raw: str | None = None
        for sample in rows:
            if frame_boxes_raw is None and sample["frame_boxes"]:
                frame_boxes_raw = sample["frame_boxes"]
            if sample["box_x1"] is None:
                continue
            if sample["event_uid"] not in owners:
                owners[sample["event_uid"]] = self.conn.execute(
                    "SELECT cat, identity_status, reviewed FROM events WHERE uid=?", (sample["event_uid"],)
                ).fetchone()
            photo_review = sample["review"] if sample["review_src"] != "subject" else None
            label, reviewed = self._scene_subject_label(
                photo_review, subject_reviews.get(sample["sid"]), owners[sample["event_uid"]]
            )
            key: int | str = sample["sid"] if sample["sid"] is not None else f"row:{sample['uid']}"
            subjects[key] = {
                "box": [sample["box_x1"], sample["box_y1"], sample["box_x2"], sample["box_y2"]],
                "sid": sample["sid"], "label": label, "reviewed": reviewed,
            }
        for sid, box in _frame_box_rows(frame_boxes_raw):
            key = sid if sid is not None else f"box:{box}"
            # `frame_boxes` repeats every photo's own box too; a sid-less copy of an animal that
            # already has its photo row must not come back as a second, unnamed "Who?" box.
            if key in subjects or any(
                all(abs(a - b) < 1e-3 for a, b in zip(existing["box"], box)) for existing in subjects.values()
            ):
                continue
            label = subject_reviews.get(sid) if sid is not None else None
            subjects[key] = {
                "box": list(box), "sid": sid, "label": label,
                "reviewed": label is not None and label not in (LABEL_SKIP, LABEL_UNKNOWN),
            }
        return sorted(subjects.values(), key=lambda item: item["box"][0] + item["box"][2])

    def _bare_event_fields(self, row: sqlite3.Row) -> dict[str, Any]:
        identity_status = row["identity_status"]
        return {
            "uid": row["uid"], "kind": row["kind"], "start": row["start"],
            "end": None if row["open"] else row["end"],
            "open": bool(row["open"]), "cat": row["cat"], "identity": identity_status,
            "confidence": row["confidence"] if identity_status == "auto" else None,
            "scene": _asset(self.entry_id, row["scene"]), "before": _asset(self.entry_id, row["before"]),
            "after": _asset(self.entry_id, row["after"]),
            "clip": self._clip_payload(row), "session": row["parent_uid"],
        }

    def _sample_dict(self, uid: str) -> dict[str, Any]:
        """One sample's `kibble/event`/`kibble/sample/label` shape: `review` is the raw
        column (`None` = follow, or `"skip"`/`"both"`/a cat name/`"not_a_cat"`); `label` is
        what it actually, currently teaches -- `"skip"`/`"both"` for those answers (never in
        `training` to look up), else the `training` row's own cat if one exists (`"not_a_cat"`
        included), else `None` (nothing saved yet, still just a `guess`)."""
        r = self.conn.execute("SELECT * FROM samples WHERE uid=?", (uid,)).fetchone()
        owner = self.conn.execute("SELECT cat FROM events WHERE uid=?", (r["event_uid"],)).fetchone()
        review = r["review"]
        if review in (LABEL_SKIP, LABEL_BOTH):
            label = review
        else:
            training_row = self.conn.execute(
                "SELECT cat FROM training WHERE uid=?", (f"{uid}-train",)
            ).fetchone()
            label = training_row["cat"] if training_row is not None else None
        box = None
        coords = (r["box_x1"], r["box_y1"], r["box_x2"], r["box_y2"])
        if all(value is not None for value in coords):
            candidate = tuple(float(value) for value in coords)
            x1, y1, x2, y2 = candidate
            if all(math.isfinite(value) for value in candidate) and 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1:
                box = candidate
        crop = None
        if box is not None:
            try:
                if crop_geometry.is_legacy_crop_trustworthy(box, int(r["t"])):
                    x1, y1, x2, y2 = box
                    rect = crop_geometry.crop_rect_square(
                        x1 * crop_geometry.VISION_FRAME_W, y1 * crop_geometry.VISION_FRAME_H,
                        x2 * crop_geometry.VISION_FRAME_W, y2 * crop_geometry.VISION_FRAME_H,
                        frame_w=crop_geometry.VISION_FRAME_W, frame_h=crop_geometry.VISION_FRAME_H,
                    )
                    if rect[2] > rect[0] and rect[3] > rect[1]:
                        crop = (
                            rect[0] / crop_geometry.VISION_FRAME_W,
                            rect[1] / crop_geometry.VISION_FRAME_H,
                            rect[2] / crop_geometry.VISION_FRAME_W,
                            rect[3] / crop_geometry.VISION_FRAME_H,
                        )
            except (OverflowError, ValueError):
                crop = None
        peers = [
            list(peer_box) for peer_sid, peer_box in _frame_box_rows(r["frame_boxes"])
            if peer_sid != r["sid"] or peer_box != box
        ]
        return {
            "uid": r["uid"],
            "cat": owner["cat"] if owner is not None else None,
            "t": r["t"],
            "body": _asset(self.entry_id, r["body"]),
            "face": _asset(self.entry_id, r["face"]),
            "guess": r["guess"],
            "guess_confidence": r["guess_confidence"],
            "review": review,
            "sid": r["sid"],
            "box": box,
            "crop": crop,
            "peers": peers,
            "label": label,
        }

    # --- kibble/label ------------------------------------------------------------------------

    def _session_and_parts(self, uid: str) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
        row = self.conn.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
        if row is None:
            return None
        session_row = self._session_row(row)
        children = self.conn.execute(
            "SELECT * FROM events WHERE parent_uid=? ORDER BY start, uid", (session_row["uid"],)
        ).fetchall()
        return session_row, list(children)

    def label_events(self, uids: Sequence[str], label: str) -> list[dict[str, Any]]:
        """The whole-visit quick-pick (`kibble/label`): applies `label` to each whole visit,
        collapsing its parts, and returns every visit's fresh `event`.

        Idempotent: a visit that already carries exactly this verdict is left alone -- no
        write, no learning -- so a retry after a lost response costs nothing. A real change is
        durable on return (the visit's `learning_queue` row commits with it) and the training
        gallery is reconciled to it here, or by the learning job if that fails: "Can't tell"
        therefore empties the gallery of every photo of the visit (rows, files and cached
        embeddings) instead of leaving them behind. Nothing here waits for a model rebuild."""
        if label not in (LABEL_UNKNOWN, _NOT_A_CAT):
            known = {row["name"] for row in self.conn.execute("SELECT name FROM cats")}
            if label not in known:
                raise ValueError(f"unknown cat: {label}")
        identity_status = LABEL_UNKNOWN if label == LABEL_UNKNOWN else _NOT_A_CAT if label == _NOT_A_CAT else "reviewed"
        cat = label if identity_status == "reviewed" else None
        session_uids: list[str] = []
        for uid in uids:
            found = self._session_and_parts(uid)
            if found is not None and found[0]["uid"] not in session_uids:
                session_uids.append(found[0]["uid"])
        touched_cats: set[str] = set()
        changed: list[str] = []
        generation = self._training_generation()
        now = _now()
        for session_uid in session_uids:
            found = self._session_and_parts(session_uid)
            if found is None:
                continue
            parent, children = found
            if (
                not children and parent["reviewed"] and not parent["from_photos"]
                and parent["identity_status"] == identity_status and parent["cat"] == cat
            ):
                continue
            self._queue_reconcile(session_uid)
            for part in [parent, *children]:
                if part["identity_status"] == "auto" and part["cat"]:
                    self._record_review_outcome(part["uid"], part["cat"], correct=part["cat"] == cat)
                    touched_cats.add(part["cat"])
            ate = parent["kind"] == "eat" or any(child["kind"] == "eat" for child in children)
            self._unsplit_session(
                parent,
                children,
                identity=(cat, identity_status, None, 1),
                kind="eat" if ate else "visit",
            )
            self.conn.execute("UPDATE events SET updated=?, from_photos=0 WHERE uid=?", (now, session_uid))
            changed.append(session_uid)
        for touched in touched_cats:
            self._refresh_auto_learn_state(touched)
        self.conn.commit()
        for session_uid in changed:
            self._reconcile_now(session_uid)
        self._count_answers(generation, len(changed))
        events: list[dict[str, Any]] = []
        for session_uid in session_uids:
            detail = self.event_detail(session_uid)
            if detail is not None:
                events.append(detail["event"])
        return events

    def set_session_cats(
        self, uid: str, cats: Sequence[tuple[str, bool]]
    ) -> dict[str, Any] | None:
        known = {row["name"] for row in self.conn.execute("SELECT name FROM cats")}
        normalized: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for cat, ate in cats:
            if not isinstance(cat, str) or cat not in known:
                raise ValueError(f"unknown cat: {cat}")
            if cat in seen:
                raise ValueError(f"duplicate cat: {cat}")
            if type(ate) is not bool:
                raise ValueError("ate must be a boolean")
            seen.add(cat)
            normalized.append((cat, ate))
        if not normalized:
            raise ValueError("cats must contain at least one cat")
        found = self._session_and_parts(uid)
        if found is None:
            return None
        parent, children = found
        current = self._session_cats(parent, children)
        if (
            not parent["from_photos"]
            and all(item["confirmed"] for item in current)
            and sorted((item["cat"], item["ate"]) for item in current) == sorted(normalized)
        ):
            return self.event_detail(parent["uid"])
        generation = self._training_generation()
        self._queue_reconcile(parent["uid"])
        self.conn.execute("UPDATE events SET from_photos=0, ate_confirmed=1 WHERE uid=?", (parent["uid"],))
        self.conn.commit()
        if len(normalized) == 1:
            cat, ate = normalized[0]
            self._unsplit_session(
                parent,
                children,
                identity=(cat, "reviewed", None, 1),
                kind="eat" if ate else "visit",
            )
        else:
            self.replan_session(parent["uid"], {}, human_cats=normalized)
        self._reconcile_now(parent["uid"])
        self._count_answers(generation)
        return self.event_detail(parent["uid"])

    def set_session_subject(
        self,
        uid: str,
        sid: int,
        label: str,
        scores: Mapping[int, sessions.IdentityScores] | None = None,
    ) -> dict[str, Any] | None:
        if type(sid) is not int or sid < 0:
            raise ValueError("sid must be a non-negative integer")
        if label not in (LABEL_FOLLOW, _NOT_A_CAT):
            known = {row["name"] for row in self.conn.execute("SELECT name FROM cats")}
            if label not in known:
                raise ValueError(f"unknown cat: {label}")
        found = self._session_and_parts(uid)
        if found is None:
            return None
        parent, children = found
        family_uids = [parent["uid"], *(child["uid"] for child in children)]
        placeholders = ",".join("?" for _ in family_uids)
        sample = self.conn.execute(
            f"SELECT uid FROM samples WHERE event_uid IN ({placeholders}) AND sid=? LIMIT 1",
            (*family_uids, sid),
        ).fetchone()
        if sample is None:
            raise ValueError(f"unknown subject: {sid}")
        if label == LABEL_FOLLOW:
            pending = self.conn.execute(
                f"SELECT COUNT(*) FROM samples WHERE event_uid IN ({placeholders}) "
                "AND sid=? AND review_src='subject'",
                (*family_uids, sid),
            ).fetchone()[0]
        else:
            pending = self.conn.execute(
                f"SELECT COUNT(*) FROM samples WHERE event_uid IN ({placeholders}) AND sid=? "
                "AND (review IS NULL OR review_src='subject') "
                "AND (review IS NOT ? OR review_src IS NOT 'subject')",
                (*family_uids, sid, label),
            ).fetchone()[0]
        if not pending:
            return self.event_detail(parent["uid"])
        generation = self._training_generation()
        self._queue_reconcile(parent["uid"])
        if label == LABEL_FOLLOW:
            self.conn.execute(
                f"UPDATE samples SET review=NULL, review_src=NULL WHERE event_uid IN ({placeholders}) "
                "AND sid=? AND review_src='subject'",
                (*family_uids, sid),
            )
        else:
            self.conn.execute(
                f"UPDATE samples SET review=?, review_src='subject' WHERE event_uid IN ({placeholders}) "
                "AND sid=? AND (review IS NULL OR review_src='subject')",
                (label, *family_uids, sid),
            )
        self.conn.commit()
        if self._is_explicit_visit(parent, children):
            human_cats = self._persisted_human_cats(parent, children)
            if label not in (LABEL_FOLLOW, _NOT_A_CAT):
                current = self._session_cats(parent, children)
                if label not in {cat["cat"] for cat in current}:
                    human_cats = [(cat["cat"], cat["ate"]) for cat in current]
                    ate = parent["kind"] == "eat" or any(cat["ate"] for cat in current)
                    human_cats.append((label, ate))
            self.replan_session(parent["uid"], scores, human_cats=human_cats)
        else:
            self._derive_visit_from_photos(parent, children, scores)
        self._reconcile_now(parent["uid"])
        self._count_answers(generation)
        return self.event_detail(parent["uid"])

    # --- photo answers -> visit identity (docs/36-ai-pipeline.md, "One photo per screen") ---------

    def _cat_names(self) -> list[str]:
        return [row["name"] for row in self.conn.execute("SELECT name FROM cats ORDER BY color")]

    def _is_explicit_visit(self, parent: sqlite3.Row, children: Sequence[sqlite3.Row]) -> bool:
        """True for a visit a person labelled as a whole (the legacy `kibble/label` and
        `kibble/session/label` answers): it keeps that label, and a photo answer can only add a
        cat to it. Everything else -- unreviewed, photo-driven, or "Can't tell" -- gets its
        identity from its photo answers (`_derive_visit_from_photos`)."""
        if parent["from_photos"]:
            return False
        if self._persisted_human_cats(parent, children):
            return True
        return bool(parent["reviewed"]) and parent["identity_status"] == _NOT_A_CAT

    def _apply_photo_answer(
        self,
        parent: sqlite3.Row,
        children: Sequence[sqlite3.Row],
        label: str,
        scores: Mapping[int, sessions.IdentityScores] | None,
    ) -> None:
        if not self._is_explicit_visit(parent, children):
            self._derive_visit_from_photos(parent, children, scores)
            return
        human_cats = self._persisted_human_cats(parent, children)
        current = self._session_cats(parent, children)
        present = {item["cat"] for item in current}
        known = self._cat_names()
        added = known if label == LABEL_BOTH else [label] if label in known else []
        missing = [cat for cat in added if cat not in present]
        if missing:
            ate = parent["kind"] == "eat" or any(item["ate"] for item in current)
            human_cats = [(item["cat"], item["ate"]) for item in current]
            human_cats.extend((cat, ate) for cat in missing)
        self.replan_session(parent["uid"], scores, human_cats=human_cats)

    def _answered_samples(self, family_uids: Sequence[str]) -> list[sqlite3.Row]:
        """The photos of one visit that carry an answer (from any source), in time order --
        what a photo-driven visit's identity is derived from."""
        placeholders = ",".join("?" for _ in family_uids)
        return self.conn.execute(
            f"SELECT uid, t, sid, review, review_src FROM samples "
            f"WHERE event_uid IN ({placeholders}) AND review IS NOT NULL ORDER BY t, uid",
            list(family_uids),
        ).fetchall()

    def _derive_visit_from_photos(
        self,
        parent: sqlite3.Row,
        children: Sequence[sqlite3.Row],
        scores: Mapping[int, sessions.IdentityScores] | None,
    ) -> None:
        """Sets a visit's identity from ALL its photo answers -- nobody is ever asked about the
        visit itself. The cats are the ones named by at least one photo (`"both"` names every
        enrolled cat), the visit is `not_a_cat` when photos were answered but none names a cat
        and one says so, and "can't tell" when only "Don't use" answers exist. Undoing every
        answer hands the visit back to the engine (`reviewed=0`)."""
        family_uids = [parent["uid"], *(child["uid"] for child in children)]
        answers = self._answered_samples(family_uids)
        enrolled = self._cat_names()
        named: list[str] = []
        not_a_cat = False
        for answer in answers:
            review = answer["review"]
            if review == LABEL_BOTH:
                named.extend(cat for cat in enrolled if cat not in named)
            elif review == _NOT_A_CAT:
                not_a_cat = True
            elif review in enrolled and review not in named:
                named.append(review)
        eat_visit = parent["kind"] == "eat" or any(child["kind"] == "eat" for child in children)
        kind = "eat" if eat_visit else "visit"
        if not answers:
            if parent["from_photos"]:
                self.conn.execute(
                    "UPDATE events SET from_photos=0, ate_confirmed=0 WHERE uid=?", (parent["uid"],)
                )
                self._unsplit_session(
                    parent, children, identity=(None, LABEL_UNKNOWN, None, 0), kind=kind
                )
            return
        self.conn.execute("UPDATE events SET from_photos=1 WHERE uid=?", (parent["uid"],))
        if named:
            flags = self._derived_ate_flags(parent, children, named, answers, eat_visit)
            self.replan_session(parent["uid"], scores, human_cats=flags)
            return
        status = _NOT_A_CAT if not_a_cat else LABEL_UNKNOWN
        self._unsplit_session(parent, children, identity=(None, status, None, 1), kind=kind)

    def _derived_ate_flags(
        self,
        parent: sqlite3.Row,
        children: Sequence[sqlite3.Row],
        named: Sequence[str],
        answers: Sequence[sqlite3.Row],
        eat_visit: bool,
    ) -> list[tuple[str, bool]]:
        """Who ate, for a photo-driven visit: one cat, or a person's own "who ate" answer, keeps
        what the visit already says; with two cats the feeder's per-subject data decides when it
        can (`_feeder_ate`); failing both, what the visit said before, else "everyone ate"."""
        persisted = {child["cat"]: child["kind"] == "eat" for child in children if child["cat"]}
        if len(named) > 1 and not parent["ate_confirmed"]:
            fed = self._feeder_ate(parent, named, answers)
            if fed is not None:
                return [(cat, fed[cat]) for cat in named]
        return [(cat, persisted.get(cat, eat_visit)) for cat in named]

    def _feeder_ate(
        self, parent: sqlite3.Row, named: Sequence[str], answers: Sequence[sqlite3.Row]
    ) -> dict[str, bool] | None:
        """Which of `named` ate according to the feeder's own per-animal data, or `None` when it
        cannot say. Each cat's animals are the subjects (`sid`) of the photos answered as that
        cat; an animal ate when the feeder saw it at the bowl. Decided only when every cat has
        at least one animal with data and at least one of them ate -- a visit where the data
        says nobody ate is exactly the case worth one question."""
        spans = self._subject_spans(parent)
        if not spans:
            return None
        subjects: dict[str, set[int]] = {cat: set() for cat in named}
        for answer in answers:
            review = answer["review"]
            if review in subjects and answer["sid"] is not None:
                subjects[review].add(answer["sid"])
        verdict: dict[str, bool] = {}
        for cat, sids in subjects.items():
            known = [spans[sid] for sid in sids if sid in spans]
            if not known:
                return None
            verdict[cat] = any(span.eat_start is not None for span in known)
        return verdict if any(verdict.values()) else None

    def _needs_ate(
        self, parent: sqlite3.Row, children: Sequence[sqlite3.Row], cats: Sequence[dict[str, Any]]
    ) -> bool:
        """True when a photo-driven visit names two cats, a meal happened, and neither the
        feeder's data nor a person has said which of them ate -- the one thing the card may
        still ask, as a single extra screen (`set_session_ate`)."""
        if not parent["from_photos"] or parent["ate_confirmed"] or len(cats) < 2:
            return False
        if not any(cat["ate"] for cat in cats):
            return False
        answers = self._answered_samples(self._family_uids(parent["uid"]))
        return self._feeder_ate(parent, [cat["cat"] for cat in cats], answers) is None

    def _visit_summary(self, parent: sqlite3.Row, children: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """The visit's identity as the photo screen shows it after an answer."""
        cats = self._session_cats(parent, children)
        kind = "eat" if parent["kind"] == "eat" or any(cat["ate"] for cat in cats) else parent["kind"]
        return {
            "uid": parent["uid"],
            "kind": kind,
            "cats": [{"cat": cat["cat"], "ate": cat["ate"]} for cat in cats],
            "needs_ate": self._needs_ate(parent, children, cats),
        }

    def _label_sample_result(self, sample_uid: str) -> dict[str, Any]:
        sample = self._sample_dict(sample_uid)
        owner = self.conn.execute("SELECT event_uid FROM samples WHERE uid=?", (sample_uid,)).fetchone()
        found = self._session_and_parts(owner["event_uid"])
        return {"sample": sample, "visit": self._visit_summary(*found) if found is not None else None}

    def set_session_ate(self, uid: str, ate_cats: Sequence[str]) -> dict[str, Any] | None:
        """Records WHO ATE for a visit that names two cats and a meal -- the one thing the feeder
        could not tell (`needs_ate`). `None` for an unknown visit; `ValueError` for an empty
        list, a duplicate, or a cat the visit does not name. Idempotent. Never trains anything:
        who ate says nothing about what a cat looks like."""
        if not ate_cats:
            raise ValueError("at least one cat must have eaten")
        if len(set(ate_cats)) != len(ate_cats):
            raise ValueError("duplicate cat")
        found = self._session_and_parts(uid)
        if found is None:
            return None
        parent, children = found
        cats = self._session_cats(parent, children)
        names = [cat["cat"] for cat in cats]
        for cat in ate_cats:
            if cat not in names:
                raise ValueError(f"not in this visit: {cat}")
        if parent["ate_confirmed"] and all(cat["ate"] == (cat["cat"] in ate_cats) for cat in cats):
            return {"visit": self._visit_summary(parent, children)}
        self._queue_reconcile(parent["uid"])
        self.conn.execute("UPDATE events SET ate_confirmed=1 WHERE uid=?", (parent["uid"],))
        self.conn.commit()
        flags = [(name, name in ate_cats) for name in names]
        if len(flags) == 1:
            cat, ate = flags[0]
            self._unsplit_session(
                parent, children, identity=(cat, "reviewed", None, 1), kind="eat" if ate else "visit"
            )
        else:
            self.replan_session(parent["uid"], {}, human_cats=flags)
        self._reconcile_now(parent["uid"])
        found = self._session_and_parts(uid)
        assert found is not None
        return {"visit": self._visit_summary(*found)}

    # --- the write-ahead queue behind every label (docs/36-ai-pipeline.md, "Learning") ------------

    def _queue_reconcile(self, session_uid: str) -> None:
        """Records that this visit's training photos must be reconciled with its answers. Called
        BEFORE the answer is committed, so the intent and the answer reach the disk together;
        `reconcile_session_training` deletes the row once the gallery matches."""
        self.conn.execute("INSERT OR IGNORE INTO learning_queue(session_uid) VALUES (?)", (session_uid,))

    def _reconcile_now(self, session_uid: str) -> None:
        """The inline half of a label write: brings the visit's training photos in line right
        away, so "Can't tell" and "Don't use" take effect at once. Failing is survivable -- the
        answer is already saved and the visit is still queued, so the learning job reconciles it
        on its next pass -- and must never fail the label itself."""
        try:
            self.reconcile_session_training(session_uid)
        except Exception:  # noqa: BLE001 -- the answer is saved; the learning job finishes this
            self.conn.rollback()
            self._learning_dirty = True  # wakes the learning job, which reconciles the queue
            _LOGGER.exception(
                "Reconciling the training photos of visit %s failed; the learning job will retry", session_uid
            )

    def reconcile_session_training(self, uid: str) -> bool:
        """Makes the training gallery match one visit's current answers, photo by photo
        (`_sample_training_target`): rows are added, moved to another cat, or removed together
        with their files and cached embeddings. Idempotent, and cheap when nothing differs.
        Returns whether the gallery changed; deletes the visit's `learning_queue` row last."""
        found = self._session_and_parts(uid)
        if found is None:
            self.conn.execute("DELETE FROM learning_queue WHERE session_uid=?", (uid,))
            self.conn.commit()
            return False
        parent, children = found
        owners = {part["uid"]: part for part in (parent, *children)}
        placeholders = ",".join("?" for _ in owners)
        rows = self.conn.execute(
            f"SELECT uid, event_uid, review, body, face, face_emb, body_feat, face_feat, mode "
            f"FROM samples WHERE event_uid IN ({placeholders})",
            list(owners),
        ).fetchall()
        changed = False
        for sample in rows:
            target = _sample_training_target(sample["review"], owners[sample["event_uid"]], parent)
            if self._reconcile_sample_training(sample, target):
                changed = True
        self.conn.execute("DELETE FROM learning_queue WHERE session_uid IN (?, ?)", (uid, parent["uid"]))
        self.conn.commit()
        return changed

    def reconcile_queued_sessions(self) -> int:
        """Finishes every reconcile a label left undone (a crash, or `_reconcile_now` failing);
        the first thing each learning pass does. Returns how many visits were queued."""
        queued = [row["session_uid"] for row in self.conn.execute("SELECT session_uid FROM learning_queue")]
        for session_uid in queued:
            self.reconcile_session_training(session_uid)
        return len(queued)

    def _reconcile_sample_training(self, sample_row: sqlite3.Row, target: str | None) -> bool:
        """Makes one sample's training row match `target`: `None` means "not trained"
        (removes an existing row and its files; a no-op if there wasn't one); any other value
        upserts it via `_upsert_training_row`, moving the files if it was previously trained
        as a different cat. Returns whether the gallery changed."""
        training_uid = f"{sample_row['uid']}-train"
        if target is None:
            existing = self.conn.execute(
                "SELECT cat FROM training WHERE uid=?", (training_uid,)
            ).fetchone()
            if existing is None:
                return False
            self._delete_training_row(training_uid, existing["cat"])
            return True
        return self._upsert_training_row(target, sample_row)

    # --- session splitting (docs/36-ai-pipeline.md): one device track, more than one cat -----

    def replan_session(
        self,
        uid: str,
        scores: Mapping[int, sessions.IdentityScores] | None = None,
        *,
        human_cats: Sequence[tuple[str, bool]] | None = None,
    ) -> bool:
        """Re-plan a whole session from all current photos and human decisions.

        The family is reconciled on every ingest touch and human write. A stable plan causes no
        writes; the result tells the caller whether the visible timeline actually changed.
        Human cat parts are authoritative, while automatic parts require the pure planner's
        co-occurrence and confidence gates. The chronological plan remains the legacy fallback.
        """
        row = self.conn.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
        if row is None:
            return False
        session_uid = row["parent_uid"] or uid
        if session_uid != uid:
            row = self.conn.execute("SELECT * FROM events WHERE uid=?", (session_uid,)).fetchone()
            if row is None:
                return False
        children = self.conn.execute(
            "SELECT * FROM events WHERE parent_uid=? ORDER BY rowid", (session_uid,)
        ).fetchall()
        family_uids = [session_uid, *(child["uid"] for child in children)]
        placeholders = ",".join("?" for _ in family_uids)
        sample_rows = self.conn.execute(
            f"SELECT uid, t, body, face, guess, guess_confidence, review, review_src, sid, frame_k, event_uid "
            f"FROM samples WHERE event_uid IN ({placeholders}) ORDER BY t, uid",
            family_uids,
        ).fetchall()
        if human_cats is None:
            human_cats = self._persisted_human_cats(row, children)

        if human_cats is not None:
            if len(human_cats) == 1:
                cat, ate = human_cats[0]
                changed = self._unsplit_session(
                    row, children, identity=(cat, "reviewed", None, 1),
                    kind="eat" if ate else "visit",
                )
                return changed
            if len(human_cats) > 1:
                parts = sessions.plan_parts(
                    [self._planning_sample(sample) for sample in sample_rows],
                    scores or {}, self._subject_spans(row), human_cats=human_cats,
                )
                assert parts is not None
                return self._apply_parts(row, children, sample_rows, parts)
            # A reviewed unknown/not-a-cat session is a verdict, not an invitation for the
            # engine to split it again.
            return self._unsplit_session(row, children)

        parts = (
            sessions.plan_parts(
                [self._planning_sample(sample) for sample in sample_rows],
                scores or {}, self._subject_spans(row),
            )
            if sessions.SESSION_SPLIT_ENABLED else None
        )
        if parts is not None:
            return self._apply_parts(row, children, sample_rows, parts)

        session_end = row["end"]
        if session_end is None:
            session_end = max((sample["t"] for sample in sample_rows), default=row["start"])
        guesses = [
            sessions.SampleGuess(
                uid=sample["uid"], t=sample["t"], guess=sample["guess"],
                confidence=sample["guess_confidence"],
            )
            for sample in sample_rows
        ]
        plan = (
            sessions.plan_split(guesses, session_start=row["start"], session_end=session_end)
            if sessions.SESSION_SPLIT_ENABLED else None
        )
        segment_children = [child for child in children if "-seg" in child["uid"][len(session_uid):]]
        if plan is not None:
            if len(segment_children) != len(children):
                self._unsplit_session(row, children)
                children = []
            if not children:
                self._create_split(row, plan)
                return True
            changed = self._extend_split(row, children, plan)
            if changed:
                self.conn.commit()
            return changed
        return self._unsplit_session(row, children)

    @staticmethod
    def _planning_sample(sample: sqlite3.Row) -> sessions.PlanningSample:
        return sessions.PlanningSample(
            uid=sample["uid"], t=sample["t"], sid=sample["sid"], frame_k=sample["frame_k"],
            has_crop=bool(sample["body"] or sample["face"]), guess=sample["guess"],
            guess_confidence=sample["guess_confidence"], review=sample["review"],
            review_src=sample["review_src"],
        )

    @staticmethod
    def _persisted_human_cats(
        row: sqlite3.Row, children: Sequence[sqlite3.Row]
    ) -> list[tuple[str, bool]] | None:
        cats = [
            (child["cat"], child["kind"] == "eat") for child in children
            if child["reviewed"] and child["identity_status"] == "reviewed" and child["cat"]
        ]
        if cats:
            return cats
        if not row["reviewed"]:
            return None
        if row["identity_status"] == "reviewed" and row["cat"]:
            return [(row["cat"], row["kind"] == "eat")]
        return []

    def _unsplit_session(
        self,
        row: sqlite3.Row,
        children: Sequence[sqlite3.Row],
        *,
        identity: tuple[str | None, str | None, float | None, int] | None = None,
        kind: str | None = None,
    ) -> bool:
        session_uid = row["uid"]
        changed = False
        for child in children:
            self.conn.execute("UPDATE samples SET event_uid=? WHERE event_uid=?", (session_uid, child["uid"]))
            self.conn.execute("DELETE FROM events WHERE uid=?", (child["uid"],))
            changed = True
        updates: dict[str, Any] = {}
        if row["hidden"]:
            updates["hidden"] = 0
        if identity is not None:
            cat, status, confidence, reviewed = identity
            updates.update(cat=cat, identity_status=status, confidence=confidence, reviewed=reviewed)
        if kind is not None:
            updates["kind"] = kind
        updates = {key: value for key, value in updates.items() if row[key] != value}
        if updates:
            assignments = ", ".join(f"{key}=?" for key in updates)
            self.conn.execute(
                f"UPDATE events SET {assignments}, updated=? WHERE uid=?",
                (*updates.values(), _now(), session_uid),
            )
            changed = True
        if changed:
            self.conn.commit()
        return changed

    def _apply_parts(
        self,
        row: sqlite3.Row,
        children: Sequence[sqlite3.Row],
        sample_rows: Sequence[sqlite3.Row],
        parts: Sequence[sessions.CatPart],
    ) -> bool:
        session_uid = row["uid"]
        now = _now()
        by_sample = {sample["uid"]: sample for sample in sample_rows}
        spans = self._subject_spans(row)
        existing = {child["uid"]: child for child in children}
        kept: set[str] = set()
        changed = False
        session_end = row["end"]
        if session_end is None:
            session_end = max((sample["t"] for sample in sample_rows), default=row["start"])

        for part in parts:
            child_uid = f"{session_uid}-cat-{slugify_cat(part.cat)}"
            kept.add(child_uid)
            part_samples = [by_sample[uid] for uid in part.sample_uids if uid in by_sample]
            subject_ids = {sample["sid"] for sample in part_samples if sample["sid"] is not None}
            times = [sample["t"] for sample in part_samples]
            firsts = [spans[sid].first for sid in subject_ids if sid in spans]
            lasts = [spans[sid].last for sid in subject_ids if sid in spans]
            if not part_samples:
                start, end = row["start"], session_end
            else:
                start = max(row["start"], min([*times, *firsts]))
                end = None if row["open"] else min(session_end, max([*times, *lasts]))
            eat_starts = [spans[sid].eat_start for sid in subject_ids if sid in spans and spans[sid].eat_start is not None]
            eat_start = (min(eat_starts) if eat_starts else row["eat_start"]) if part.ate else None
            kind = "eat" if part.ate else "visit"
            status = "reviewed" if part.reviewed else "auto"
            confidence = None if part.reviewed else part.confidence
            before = row["before"] if part.ate else None
            after = row["after"] if part.ate else None
            old = existing.get(child_uid)
            if old is None:
                self.conn.execute(
                    "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
                    "before, after, cat, identity_status, confidence, reviewed, updated, parent_uid, hidden) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (child_uid, row["device_event_id"], kind, start, end, int(bool(row["open"])), eat_start,
                     row["scene"], before, after, part.cat, status, confidence, int(part.reviewed), now, session_uid),
                )
                changed = True
            else:
                desired = {
                    "kind": kind, "start": start, "end": end, "open": int(bool(row["open"])),
                    "eat_start": eat_start, "scene": row["scene"], "before": before, "after": after,
                    "cat": part.cat, "identity_status": status, "confidence": confidence,
                    "reviewed": int(part.reviewed), "hidden": 0,
                }
                updates = {
                    key: value for key, value in desired.items()
                    if old[key] != value and not (old["reviewed"] and key in ("cat", "identity_status", "confidence", "reviewed"))
                }
                if updates:
                    assignments = ", ".join(f"{key}=?" for key in updates)
                    self.conn.execute(
                        f"UPDATE events SET {assignments}, updated=? WHERE uid=?",
                        (*updates.values(), now, child_uid),
                    )
                    changed = True
            if part.sample_uids:
                placeholders = ",".join("?" for _ in part.sample_uids)
                cur = self.conn.execute(
                    f"UPDATE samples SET event_uid=? WHERE uid IN ({placeholders}) AND event_uid!=?",
                    (child_uid, *part.sample_uids, child_uid),
                )
                changed = changed or cur.rowcount > 0

        for child in children:
            if child["uid"] not in kept:
                self.conn.execute("UPDATE samples SET event_uid=? WHERE event_uid=?", (session_uid, child["uid"]))
                self.conn.execute("DELETE FROM events WHERE uid=?", (child["uid"],))
                changed = True
        parent_updates = {"hidden": 1}
        if any(part.reviewed for part in parts):
            parent_updates.update(cat=None, identity_status=None, confidence=None, reviewed=0)
        parent_updates = {key: value for key, value in parent_updates.items() if row[key] != value}
        if parent_updates:
            assignments = ", ".join(f"{key}=?" for key in parent_updates)
            self.conn.execute(
                f"UPDATE events SET {assignments}, updated=? WHERE uid=?",
                (*parent_updates.values(), now, session_uid),
            )
            changed = True
        if changed:
            self.conn.commit()
        return changed

    def _subject_spans(self, row: sqlite3.Row) -> dict[int, sessions.SubjectSpan]:
        """Parse the top-level event's `subjects` summary into the planner's subject spans.
        Return `{}` for a legacy session or malformed JSON; the planner derives spans from
        sample timestamps when this summary is unavailable."""
        raw = row["subjects"]
        if not raw:
            return {}
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        out: dict[int, sessions.SubjectSpan] = {}
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict) or item.get("sid") is None:
                continue
            try:
                sid = int(item["sid"])
                first, last = int(item.get("first") or 0), int(item.get("last") or 0)
                eat_start = item.get("eat_start")
                out[sid] = sessions.SubjectSpan(
                    sid=sid, first=first, last=last,
                    eat_start=int(eat_start) if eat_start is not None else None,
                )
            except (TypeError, ValueError):
                continue
        return out

    def _create_split(self, row: sqlite3.Row, plan: list[sessions.Segment]) -> None:
        now = _now()
        self._append_split_segments(row, plan, start_index=0)
        self.conn.execute("UPDATE events SET hidden=1, updated=? WHERE uid=?", (now, row["uid"]))
        self.conn.commit()

    def _extend_split(self, row: sqlite3.Row, children: Sequence[sqlite3.Row], plan: list[sessions.Segment]) -> bool:
        """Reconciles an ALREADY-split family against a freshly recomputed `plan`: matches
        existing children to plan segments by position (oldest-first) and cat, extending each
        match with whatever new samples the plan now assigns it and refreshing its own
        non-identity fields (`end`/`open`/`after`) exactly like `upsert_event` refreshes the
        parent's own -- never its `cat`/`identity_status`/`confidence`/`reviewed`, and never any
        field at all once a child is `reviewed`. Stops matching at the first position where the
        plan's cat no longer agrees with the existing child's own -- a genuine identity mismatch
        (relabel, reclassification drift) is never silently overwritten, and nothing past that
        point is touched either. Only once EVERY existing child matched cleanly, and the plan
        has segments beyond them, are the extra trailing one(s) created -- exactly like the
        first-ever split. The reverse, a plan that SHRANK so that every segment it still has
        matched but children remain past its end, deletes those surplus children (an unreviewed
        one) or closes them (a reviewed one): the plan has already moved the photos it still
        wants, and an orphan left open kept its cat "present" -- and counted as an extra meal --
        long after the session ended (e3438, 2026-10-03). Returns whether anything changed."""
        changed = False
        now = _now()
        last_plan_index = len(plan) - 1
        matched = 0
        for i, child in enumerate(children):
            if i >= len(plan) or child["cat"] != plan[i].cat:
                break
            matched = i + 1
            seg = plan[i]
            already = {
                r["uid"] for r in self.conn.execute("SELECT uid FROM samples WHERE event_uid=?", (child["uid"],)).fetchall()
            }
            new_uids = [u for u in seg.sample_uids if u not in already]
            if new_uids:
                ph = ",".join("?" for _ in new_uids)
                self.conn.execute(f"UPDATE samples SET event_uid=? WHERE uid IN ({ph})", (child["uid"], *new_uids))
                changed = True
            if not child["reviewed"]:
                is_last = i == last_plan_index
                new_open = int(is_last and bool(row["open"]))
                new_after = child["after"] if child["after"] is not None else (row["after"] if is_last else None)
                if seg.end != child["end"] or new_open != child["open"] or new_after != child["after"]:
                    self.conn.execute(
                        "UPDATE events SET end=?, open=?, after=?, updated=? WHERE uid=?",
                        (seg.end, new_open, new_after, now, child["uid"]),
                    )
                    changed = True
        if matched == len(plan):
            for child in children[matched:]:
                if child["reviewed"]:
                    if child["open"]:
                        self.conn.execute("UPDATE events SET open=0, updated=? WHERE uid=?", (now, child["uid"]))
                        changed = True
                    continue
                self.conn.execute("UPDATE samples SET event_uid=? WHERE event_uid=?", (row["uid"], child["uid"]))
                self.conn.execute("DELETE FROM events WHERE uid=?", (child["uid"],))
                changed = True
        if matched == len(children) and len(plan) > matched:
            self._append_split_segments(row, plan[matched:], start_index=matched)
            changed = True
        return changed

    def _append_split_segments(self, row: sqlite3.Row, segments: Sequence[sessions.Segment], *, start_index: int) -> None:
        """Creates one or more trailing child segments past whatever already exists -- the
        shared tail of both a first-ever split (`start_index=0`, nothing existed yet) and
        `_extend_split` growing a family (a brand new run appeared past the last matched
        child)."""
        now = _now()
        last_index = start_index + len(segments) - 1
        for offset, seg in enumerate(segments):
            i = start_index + offset
            child_uid = f"{row['uid']}-seg{i}"
            eat_start = (
                row["eat_start"] if row["eat_start"] is not None and seg.start <= row["eat_start"] < seg.end else None
            )
            is_last = i == last_index
            self.conn.execute(
                """
                INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start,
                                    scene, before, after, cat, identity_status, confidence,
                                    reviewed, updated, parent_uid, hidden)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, 0, ?, ?, 0)
                """,
                (
                    child_uid, row["device_event_id"], row["kind"], seg.start, seg.end,
                    int(is_last and bool(row["open"])), eat_start,
                    row["scene"] if eat_start is not None else None,
                    row["before"] if i == 0 else None,
                    row["after"] if is_last else None,
                    seg.cat, seg.confidence, now, row["uid"],
                ),
            )
            if seg.sample_uids:
                ph = ",".join("?" for _ in seg.sample_uids)
                self.conn.execute(f"UPDATE samples SET event_uid=? WHERE uid IN ({ph})", (child_uid, *seg.sample_uids))

    # --- kibble/sample/label -------------------------------------------------------------------

    def label_sample(
        self,
        sample_uid: str,
        label: str,
        scores: Mapping[int, sessions.IdentityScores] | None = None,
    ) -> dict[str, Any] | None:
        """Saves one photo's answer -- the unit of review (docs/36-ai-pipeline.md, "One photo per
        screen") -- and returns `{"sample", "visit"}`: the photo as it now stands and what the
        visit's identity became because of it. `None` for an unknown photo.

        `label` is a cat name, `"both"` (two cats in this one crop: only with exactly two cats
        enrolled; never trained, both are recorded as present), `"not_a_cat"`, `"skip"` ("Don't
        use": never trained) or `"follow"` (undo: back to no answer).

        The answer is durable before anything else happens (the visit's `learning_queue` row
        commits with it); the visit's identity and the training gallery are then brought in line
        straight away, and the model itself relearns later, in the learning job. Idempotent:
        saying what the photo already says writes nothing and wakes nothing."""
        row = self.conn.execute(
            "SELECT uid, event_uid, review, guess FROM samples WHERE uid=?", (sample_uid,)
        ).fetchone()
        if row is None:
            return None
        known = self._cat_names()
        if label == LABEL_BOTH:
            if len(known) != 2:
                raise ValueError("'both' needs exactly two cats enrolled")
        elif label not in (LABEL_FOLLOW, _NOT_A_CAT, LABEL_SKIP) and label not in known:
            raise ValueError(f"unknown cat: {label}")
        review = None if label == LABEL_FOLLOW else label
        if row["review"] == review:
            return self._label_sample_result(sample_uid)
        generation = self._training_generation()
        found = self._session_and_parts(row["event_uid"])
        self.conn.execute(
            "UPDATE samples SET review=?, review_src=? WHERE uid=?",
            (review, None if review is None else "photo", sample_uid),
        )
        if found is not None:
            self._queue_reconcile(found[0]["uid"])
        self.conn.commit()
        if found is not None:
            parent, children = found
            self._apply_photo_answer(parent, children, label, scores)
            self._reconcile_now(parent["uid"])
        if row["guess"] and row["guess"] != _NOT_A_CAT and label not in (LABEL_FOLLOW, LABEL_SKIP, LABEL_BOTH):
            self._record_review_outcome(sample_uid, row["guess"], correct=row["guess"] == label)
            self._refresh_auto_learn_state(row["guess"])
        self._count_answers(generation)
        return self._label_sample_result(sample_uid)

    def _upsert_training_row(
        self, cat: str, sample_row: sqlite3.Row, *, source: str = "label", confidence: float | None = None
    ) -> bool:
        """Upserts one sample's training row under `training_uid = f"{sample_row['uid']}-train"`;
        `True` when the gallery actually changed (a row appeared, moved to another cat, or had
        its files rewritten), `False` when it was already there as asked. `source`/`confidence`
        default to the original human-review path (`"label"`, no confidence);
        `add_upload_training`/`add_auto_training` pass their own.

        Idempotent: a row that already carries this cat and still has its files is only
        touched when its source differs -- a human answering a photo that was `auto`-learned
        promotes it to `"label"` (protected, per `_select_eviction_candidates`'s design note) --
        and never has its files rewritten or the learning generation advanced. Re-saving the
        same answer must not look like a change to the learning job."""
        training_uid = f"{sample_row['uid']}-train"
        existing = self.conn.execute(
            "SELECT cat, source, confidence, body, face FROM training WHERE uid=?", (training_uid,)
        ).fetchone()
        if existing is not None and existing["cat"] == cat and self._training_files_present(existing):
            if existing["source"] != source or existing["confidence"] != confidence:
                self.conn.execute(
                    "UPDATE training SET source=?, confidence=?, created=? WHERE uid=?",
                    (source, confidence, _now(), training_uid),
                )
            return False
        if existing is not None and existing["cat"] != cat:
            self.delete_training_files(existing["cat"], training_uid)
        body_asset = None
        face_asset = None
        if sample_row["body"] is not None:
            data = self._read_media(sample_row["body"])
            if data is not None:
                body_asset = self.write_training(cat, training_uid, "body", data)
        if sample_row["face"] is not None:
            data = self._read_media(sample_row["face"])
            if data is not None:
                face_asset = self.write_training(cat, training_uid, "face", data)
        self.conn.execute(
            """
            INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, face_feat, mode, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                cat=excluded.cat, source=excluded.source, created=excluded.created, body=excluded.body,
                face=excluded.face, face_emb=excluded.face_emb, body_feat=excluded.body_feat,
                face_feat=excluded.face_feat, mode=excluded.mode, confidence=excluded.confidence
            """,
            (
                training_uid, cat, source, _now(), body_asset, face_asset,
                sample_row["face_emb"], sample_row["body_feat"], sample_row["face_feat"], sample_row["mode"],
                confidence,
            ),
        )
        self._bump_training_generation()
        return True

    def _training_files_present(self, training_row: sqlite3.Row) -> bool:
        return all((self.root / rel).exists() for rel in (training_row["body"], training_row["face"]) if rel)

    def _read_media(self, asset_id: str) -> bytes | None:
        try:
            return (self.media_root / asset_id).read_bytes()
        except FileNotFoundError:
            return None

    def _enforce_training_cap(self, cat: str) -> None:
        rows = self.conn.execute(
            "SELECT uid, source, confidence, created FROM training WHERE cat=? ORDER BY created ASC",
            (cat,),
        ).fetchall()
        if len(rows) <= TRAINING_CAP_PER_CAT:
            return
        candidates = [
            TrainingCandidate(uid=r["uid"], source=r["source"], confidence=r["confidence"], created=r["created"])
            for r in rows
        ]
        auto_uids = [c.uid for c in candidates if c.source not in _PROTECTED_TRAINING_SOURCES]
        if not auto_uids and len(candidates) <= TRAINING_HARD_CAP_PER_CAT:
            return  # only protected rows are over the soft cap, and those go only past the hard cap
        redundancy: dict[str, float] = {}
        if auto_uids:
            # Only the auto rows are ranked (most redundant first), each against the whole pool.
            redundancy = _nearest_distance_ranks(
                [
                    (r["uid"], identity.unpack(r["body_feat"]) if r["body_feat"] is not None else identity.unpack(r["face_feat"]), r["mode"])
                    for r in self.conn.execute(
                        "SELECT uid, body_feat, face_feat, mode FROM training WHERE cat=? ORDER BY created ASC",
                        (cat,),
                    )
                ],
                only=auto_uids,
            )
        for uid in _select_eviction_candidates(candidates, redundancy, TRAINING_CAP_PER_CAT, TRAINING_HARD_CAP_PER_CAT):
            self._delete_training_row(uid, cat)

    def _training_file_bytes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.conn.execute("SELECT uid, body, face FROM training"):
            total = 0
            for rel in (row["body"], row["face"]):
                if rel:
                    try:
                        total += (self.root / rel).stat().st_size
                    except FileNotFoundError:
                        pass
            out[row["uid"]] = total
        return out

    def _enforce_total_training_cap(self) -> None:
        """Whole-store companion to `_enforce_training_cap`: same soft/hard split and eviction
        order (auto rows, most redundant/lowest-confidence/oldest first; protected rows only
        past the hard cap, oldest first), just measured in bytes across every cat's pool
        combined -- see `TRAINING_CAP_TOTAL_BYTES`'s comment. Redundancy is still ranked within
        each cat's own same-mode pool: a different cat's appearance descriptors are never
        expected to be close, and the classifier never compares across cats either."""
        sizes = self._training_file_bytes()
        total = sum(sizes.values())
        if total <= TRAINING_CAP_TOTAL_BYTES:
            return
        rows = self.conn.execute(
            "SELECT uid, cat, source, confidence, created, body_feat, face_feat, mode FROM training "
            "ORDER BY created ASC"
        ).fetchall()
        by_cat: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_cat.setdefault(r["cat"], []).append(r)
        redundancy: dict[str, float] = {}
        for cat_rows in by_cat.values():
            redundancy.update(
                _nearest_distance_ranks(
                    [
                        (
                            r["uid"],
                            identity.unpack(r["body_feat"]) if r["body_feat"] is not None else identity.unpack(r["face_feat"]),
                            r["mode"],
                        )
                        for r in cat_rows
                    ]
                )
            )
        by_uid = {r["uid"]: r for r in rows}
        candidates = [
            TrainingCandidate(uid=r["uid"], source=r["source"], confidence=r["confidence"], created=r["created"])
            for r in rows
        ]
        auto_ranked = sorted(
            (c for c in candidates if c.source not in _PROTECTED_TRAINING_SOURCES),
            key=lambda c: (redundancy.get(c.uid, float("inf")), c.confidence if c.confidence is not None else 0.0, c.created),
        )
        for c in auto_ranked:
            if total <= TRAINING_CAP_TOTAL_BYTES:
                break
            self._delete_training_row(c.uid, by_uid[c.uid]["cat"])
            total -= sizes.get(c.uid, 0)
        if total > TRAINING_HARD_CAP_TOTAL_BYTES:
            protected_by_age = sorted(
                (c for c in candidates if c.source in _PROTECTED_TRAINING_SOURCES), key=lambda c: c.created
            )
            for c in protected_by_age:
                if total <= TRAINING_HARD_CAP_TOTAL_BYTES:
                    break
                self._delete_training_row(c.uid, by_uid[c.uid]["cat"])
                total -= sizes.get(c.uid, 0)

    def add_import_training(
        self, *, cat: str, uid: str, body: bytes | None, face: bytes | None, features: identity.Features
    ) -> None:
        """Migration-only: writes one training row/files directly, source="import"."""
        body_asset = self.write_training(cat, uid, "body", body) if body else None
        face_asset = self.write_training(cat, uid, "face", face) if face else None
        self.conn.execute(
            """
            INSERT OR REPLACE INTO training(uid, cat, source, created, body, face, face_emb, body_feat, face_feat, mode)
            VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid, cat, _now(), body_asset, face_asset,
                identity.pack(features.face_emb), identity.pack(features.body_feat),
                identity.pack(features.face_feat), features.mode,
            ),
        )

    # --- kibble/review/photos ------------------------------------------------------------------

    def _review_gallery(self) -> photo_queue.Gallery | None:
        """The training gallery's Coral centroids that rank which photos look new. Rebuilt when
        the gallery changed, and every few minutes regardless: embeddings are cached after
        the fact (Coral catches up) without the gallery's generation moving."""
        generation = self._training_generation()
        now = time.monotonic()
        cached = self._review_gallery_cache
        if cached is None or cached[0] != generation or now - cached[1] > 300:
            cached = (generation, now, photo_queue.load_gallery(self.conn))
            self._review_gallery_cache = cached
        return cached[2]

    def review_photos_page(self, *, limit: int, cursor: str | None, retention_cutoff: int) -> dict[str, Any]:
        """One page of the photo review queue (`photo_queue.photo_page` picks the photos); read-only,
        and independent of any learning pass -- it reads stored answers and cached embeddings,
        never the model."""
        page = photo_queue.photo_page(
            self.conn, cutoff=retention_cutoff, cursor=cursor, limit=limit, gallery=self._review_gallery()
        )
        items: list[dict[str, Any]] = []
        for visit in page.visits:
            visit_dict = {
                "uid": visit.uid, "start": visit.start, "kind": visit.kind,
                "scene": _asset(self.entry_id, visit.scene),
            }
            for pick in visit.picks:
                items.append(
                    {"sample": self._sample_dict(pick.uid), "visit": visit_dict | {"in_scene": pick.in_scene}}
                )
        return {
            "items": items, "remaining": page.remaining, "visits": page.total_visits,
            "cursor": page.cursor, "has_more": page.has_more,
        }

    # --- kibble/cats ---------------------------------------------------------------------------

    def cats(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT name, color, created FROM cats ORDER BY color").fetchall()]

    def add_cat(self, name: str) -> None:
        existing = self.conn.execute("SELECT 1 FROM cats WHERE name=?", (name,)).fetchone()
        if existing is not None:
            return
        color = self.conn.execute("SELECT COUNT(*) AS n FROM cats").fetchone()["n"]
        self.conn.execute(
            "INSERT INTO cats(name, color, created) VALUES (?, ?, ?)", (name, color, _now())
        )
        self.conn.commit()

    def delete_cat(self, name: str) -> None:
        row = self.conn.execute("SELECT avatar_asset FROM cats WHERE name=?", (name,)).fetchone()
        if row is not None and row["avatar_asset"]:
            try:
                (self.root / row["avatar_asset"]).unlink()
            except FileNotFoundError:
                pass
        self.conn.execute("DELETE FROM cats WHERE name=?", (name,))
        self.conn.execute("DELETE FROM review_outcomes WHERE cat=?", (name,))
        self.conn.execute("DELETE FROM auto_learn_state WHERE cat=?", (name,))
        self.conn.commit()

    def cat_exists(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM cats WHERE name=?", (name,)).fetchone() is not None

    def training_counts(self, cat: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN face IS NOT NULL THEN 1 ELSE 0 END) AS face, "
            "SUM(CASE WHEN body IS NOT NULL THEN 1 ELSE 0 END) AS body "
            "FROM training WHERE cat=?",
            (cat,),
        ).fetchone()
        return {"total": row["total"] or 0, "face": row["face"] or 0, "body": row["body"] or 0}

    def _avatar_state(self, cat: str) -> tuple[dict[str, str] | None, bool, int | None]:
        """`(asset, is_custom, updated_ts)` -- `cats.avatar_asset` when the user picked one
        (`is_custom=True`), else the newest trained photo (`is_custom=False`), else `(None,
        False, None)` for a cat with neither. The one place both `cat_avatar`/`avatar_info`
        (WS-facing) and `identity_summary` (the per-cat `image` entity) resolve "what picture
        represents this cat" from, so they can never disagree."""
        row = self.conn.execute(
            "SELECT avatar_asset, avatar_updated FROM cats WHERE name=?", (cat,)
        ).fetchone()
        if row is not None and row["avatar_asset"]:
            return _asset(self.entry_id, row["avatar_asset"]), True, row["avatar_updated"]
        auto = self.conn.execute(
            "SELECT COALESCE(face, body) AS asset, created FROM training WHERE cat=? "
            "AND (face IS NOT NULL OR body IS NOT NULL) ORDER BY created DESC LIMIT 1",
            (cat,),
        ).fetchone()
        if auto is None:
            return None, False, None
        return _asset(self.entry_id, auto["asset"]), False, auto["created"]

    def cat_avatar(self, cat: str) -> dict[str, str] | None:
        return self._avatar_state(cat)[0]

    def avatar_info(self, cat: str) -> dict[str, Any]:
        asset, custom, _updated = self._avatar_state(cat)
        return {"avatar": asset, "custom": custom}

    def cats_overview(self, retention_days: int) -> dict[str, Any]:
        """Everything `kibble/cats` reads from the store, in ONE store job instead of one per
        cat per fact -- so the page costs the store thread a single short turn however long the
        learning job's own queue is."""
        cats = []
        for row in self.cats():
            name = row["name"]
            asset, custom, _updated = self._avatar_state(name)
            cats.append(
                {
                    "name": name,
                    "color": row["color"],
                    "avatar": asset,
                    "avatar_custom": custom,
                    "learning_paused": self.auto_learn_paused(name),
                    "training": self.training_counts(name),
                }
            )
        return {"cats": cats, "storage": self.storage_summary(retention_days)}

    def set_cat_avatar(self, cat: str, data: bytes) -> dict[str, str] | None:
        """Writes already-processed JPEG bytes (`media_processing.process_upload_image`'s
        output) as `cat`'s custom avatar: a dedicated file under `avatars/`, never a reference
        to a media/training asset, so a later retention purge or training-cap eviction can
        never break it. Atomic write, same tmp-then-replace pattern as `write_training`/
        `write_media`. `None` if `cat` isn't enrolled."""
        if not self.cat_exists(cat):
            return None
        path = self.root / AVATAR_DIR / f"{slugify_cat(cat)}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        asset_id = f"{AVATAR_DIR}/{slugify_cat(cat)}.jpg"
        now = _now()
        self.conn.execute(
            "UPDATE cats SET avatar_asset=?, avatar_updated=? WHERE name=?", (asset_id, now, cat)
        )
        self.conn.commit()
        return _asset(self.entry_id, asset_id)

    def set_cat_avatar_from_asset(self, cat: str, asset_id: str) -> dict[str, str] | None:
        """Copies an existing media/training asset's bytes into `cat`'s dedicated avatar file
        -- "choose one of this cat's existing photos" never leaves the avatar pointing at a row
        that could later be evicted or purged out from under it. `None` if `cat` isn't enrolled
        or `asset_id` doesn't resolve to a real file under this entry's own store."""
        if not self.cat_exists(cat):
            return None
        source = resolve_asset_path(self.root, asset_id)
        if source is None or not source.is_file():
            return None
        return self.set_cat_avatar(cat, source.read_bytes())

    def clear_cat_avatar(self, cat: str) -> bool:
        row = self.conn.execute("SELECT avatar_asset FROM cats WHERE name=?", (cat,)).fetchone()
        if row is None:
            return False
        if row["avatar_asset"]:
            try:
                (self.root / row["avatar_asset"]).unlink()
            except FileNotFoundError:
                pass
        self.conn.execute("UPDATE cats SET avatar_asset=NULL, avatar_updated=NULL WHERE name=?", (cat,))
        self.conn.commit()
        return True

    def add_upload_training(
        self, *, cat: str, uid: str, data: bytes, features: identity.Features
    ) -> dict[str, Any] | None:
        """Writes one uploaded photo as a training row, `source='upload'`. `None` if `cat`
        isn't enrolled. The caller (`views.py`'s upload view) has already run `data` through
        `media_processing.process_upload_image` (downscale/strip/orient) and the no-cat/dedupe
        gates in `identity.py` -- this method only persists and enforces caps."""
        if not self.cat_exists(cat):
            return None
        body_asset = self.write_training(cat, uid, "body", data)
        self.conn.execute(
            """
            INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, face_feat, mode, confidence)
            VALUES (?, ?, 'upload', ?, ?, NULL, NULL, ?, NULL, ?, NULL)
            """,
            (uid, cat, _now(), body_asset, identity.pack(features.body_feat), features.mode),
        )
        self._bump_training_generation()
        self._enforce_training_cap(cat)
        self._enforce_total_training_cap()
        self.conn.commit()
        return self._training_row_dict(uid)

    def add_auto_training(self, *, cat: str, sample_uid: str, confidence: float) -> bool:
        """Copies one already-archived live sample into training, `source='auto'`, with
        `confidence` set for eviction ranking. Idempotent and non-clobbering: a no-op if a
        training row already exists for this exact sample under ANY source -- a human's or an
        earlier upload's opinion on a specific photo is never silently downgraded to `auto`,
        and a closed event may be re-evaluated for auto-learning across more than one poll."""
        training_uid = f"{sample_uid}-train"
        if self.conn.execute("SELECT 1 FROM training WHERE uid=?", (training_uid,)).fetchone() is not None:
            return False
        row = self.conn.execute(
            "SELECT uid, event_uid, review, body, face, face_emb, body_feat, face_feat, mode FROM samples WHERE uid=?",
            (sample_uid,),
        ).fetchone()
        if row is None or self._withheld_from_training(row):
            return False
        self._upsert_training_row(cat, row, source="auto", confidence=confidence)
        self._enforce_training_cap(cat)
        self._enforce_total_training_cap()
        self.conn.commit()
        return True

    def _withheld_from_training(self, sample_row: sqlite3.Row) -> bool:
        """True for a photo a person said not to learn from: its own answer is "Don't use",
        "Both", "Not a cat" or the legacy "Can't tell", or its visit was reviewed as "Can't
        tell". Auto-learn (`add_auto_training`) must never put such a photo (back) into the
        gallery -- the person's answer outranks any later confident guess."""
        if sample_row["review"] in (LABEL_SKIP, LABEL_BOTH, LABEL_UNKNOWN, _NOT_A_CAT):
            return True
        owner = self.conn.execute("SELECT * FROM events WHERE uid=?", (sample_row["event_uid"],)).fetchone()
        if owner is None:
            return False
        return any(
            part["reviewed"] and part["identity_status"] == LABEL_UNKNOWN
            for part in (owner, self._session_row(owner))
        )

    def _training_row_dict(self, uid: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT uid, cat, created, source, body, face FROM training WHERE uid=?", (uid,)
        ).fetchone()
        if r is None:
            return None
        return {
            "uid": r["uid"], "cat": r["cat"], "created": r["created"], "source": r["source"],
            "body": _asset(self.entry_id, r["body"]), "face": _asset(self.entry_id, r["face"]),
        }

    def free_disk_bytes(self) -> int:
        """Free space on the filesystem backing this entry's store root -- checked by the
        upload/auto-learn write paths against `FREE_DISK_FLOOR_BYTES` before writing, never
        inferred from the byte caps above (a neighbour on the same disk fills it independently
        of anything Kibble wrote)."""
        return shutil.disk_usage(self.root).free

    def event_training_context(self, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT open, reviewed, identity_status, cat, judge_cat, judge_at FROM events WHERE uid=?",
            (uid,),
        ).fetchone()
        if row is None:
            return None
        return {
            "open": bool(row["open"]), "reviewed": bool(row["reviewed"]),
            "identity_status": row["identity_status"], "cat": row["cat"],
            "judge_cat": row["judge_cat"], "judge_at": row["judge_at"],
        }

    def sample_guesses(self, event_uid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT uid, t, guess, guess_confidence, review, body_feat, face_feat, mode, "
            "box_x1, box_y1, box_x2, box_y2 "
            "FROM samples WHERE event_uid=? ORDER BY t",
            (event_uid,),
        ).fetchall()
        out = []
        for r in rows:
            box = None
            if r["box_x1"] is not None:
                box = (r["box_x1"], r["box_y1"], r["box_x2"], r["box_y2"])
            out.append({
                "uid": r["uid"], "t": r["t"], "guess": r["guess"],
                "guess_confidence": r["guess_confidence"], "review": r["review"],
                "feat": identity.unpack(r["body_feat"]) if r["body_feat"] is not None else identity.unpack(r["face_feat"]),
                "mode": r["mode"], "box": box,
            })
        return out

    def training_feats_for_cat(self, cat: str, mode: str | None) -> list[Any]:
        rows = self.conn.execute(
            "SELECT body_feat, face_feat, mode FROM training WHERE cat=? AND mode=?", (cat, mode)
        ).fetchall()
        out: list[Any] = []
        for r in rows:
            feat = identity.unpack(r["body_feat"]) if r["body_feat"] is not None else identity.unpack(r["face_feat"])
            if feat is not None:
                out.append(feat)
        return out

    def auto_learn_paused(self, cat: str) -> bool:
        row = self.conn.execute("SELECT paused FROM auto_learn_state WHERE cat=?", (cat,)).fetchone()
        return bool(row["paused"]) if row is not None else False

    def _recent_outcomes(self, cat: str, limit: int) -> list[bool]:
        rows = self.conn.execute(
            "SELECT correct FROM review_outcomes WHERE cat=? ORDER BY ts DESC LIMIT ?", (cat, limit)
        ).fetchall()
        return [bool(r["correct"]) for r in rows]

    def _recent_guess_confidences(self, cat: str, limit: int) -> list[float]:
        rows = self.conn.execute(
            "SELECT guess_confidence FROM samples WHERE guess=? AND guess_confidence IS NOT NULL "
            "ORDER BY t DESC LIMIT ?",
            (cat, limit),
        ).fetchall()
        return [r["guess_confidence"] for r in rows]

    def rolling_accuracy(self, cat: str) -> float | None:
        """Mean of the last `autolearn.ROLLING_REVIEW_WINDOW` human review outcomes for `cat`,
        or `None` under `autolearn.AUTO_LEARN_MIN_REVIEWS` of them -- not enough evidence to
        mean anything, the same floor `next_paused_state`/`_refresh_auto_learn_state` use."""
        outcomes = self._recent_outcomes(cat, autolearn.ROLLING_REVIEW_WINDOW)
        if len(outcomes) < autolearn.AUTO_LEARN_MIN_REVIEWS:
            return None
        return sum(1 for ok in outcomes if ok) / len(outcomes)

    def clear_training(self, cat: str | None, *, keep_uploads: bool) -> int:
        """Removes training rows (and their files) for `cat`, or every cat when `cat is None`
        (`kibble.clear_training`'s `cat: "all"`). `keep_uploads=True` preserves `source=
        'upload'` rows, removing every other source; the default removes everything for the
        target. Never touches `events`/`samples` -- event history and live evidence are
        retention's job, not this one. Returns the number of rows removed; caller
        (`__init__.py`'s service handler) owns rebuilding the model and refreshing afterward."""
        clauses = ["cat=?"] if cat is not None else []
        args: list[Any] = [cat] if cat is not None else []
        if keep_uploads:
            clauses.append("source != 'upload'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT uid, cat FROM training{where}", args).fetchall()
        for row in rows:
            self.delete_training_files(row["cat"], row["uid"])
        if rows:
            placeholders = ",".join("?" for _ in rows)
            uids = [r["uid"] for r in rows]
            self.conn.execute(
                f"DELETE FROM coral_embeddings WHERE row_kind='training' AND row_uid IN ({placeholders})", uids
            )
            self.conn.execute(f"DELETE FROM training WHERE uid IN ({placeholders})", uids)
            self._bump_training_generation()
            self.conn.commit()
        return len(rows)

    def _record_review_outcome(self, event_uid: str, cat: str, *, correct: bool) -> None:
        self.conn.execute(
            "INSERT INTO review_outcomes(uid, cat, ts, correct) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET cat=excluded.cat, ts=excluded.ts, correct=excluded.correct",
            (event_uid, cat, _now(), int(correct)),
        )

    def _refresh_auto_learn_state(self, cat: str) -> None:
        recent = self._recent_outcomes(cat, autolearn.ROLLING_REVIEW_WINDOW)
        current = self.conn.execute("SELECT paused FROM auto_learn_state WHERE cat=?", (cat,)).fetchone()
        currently_paused = bool(current["paused"]) if current is not None else False
        next_paused = autolearn.next_paused_state(currently_paused, recent)
        self.conn.execute(
            "INSERT INTO auto_learn_state(cat, paused, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(cat) DO UPDATE SET paused=excluded.paused, updated=excluded.updated",
            (cat, int(next_paused), _now()),
        )

    # --- kibble/training ---------------------------------------------------------------------

    def training_page(self, *, cat: str, limit: int, cursor: str | None) -> dict[str, Any]:
        bound = _decode_cursor(cursor) if cursor else None
        rows = self._page_rows("training", "created", "cat=?", (cat,), bound, limit + 1)
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [
            {
                "uid": r["uid"], "cat": r["cat"], "created": r["created"], "source": r["source"],
                "body": _asset(self.entry_id, r["body"]), "face": _asset(self.entry_id, r["face"]),
            }
            for r in page_rows
        ]
        total = self.conn.execute("SELECT COUNT(*) AS n FROM training WHERE cat=?", (cat,)).fetchone()["n"]
        cursor_out = _encode_cursor(page_rows[-1]["created"], page_rows[-1]["uid"]) if has_more and page_rows else None
        return {"items": items, "total": total, "cursor": cursor_out, "has_more": has_more}

    def training_remove(self, uids: Sequence[str]) -> int:
        removed = 0
        for uid in uids:
            row = self.conn.execute("SELECT cat FROM training WHERE uid=?", (uid,)).fetchone()
            if row is None:
                continue
            self._delete_training_row(uid, row["cat"])
            removed += 1
        self.conn.commit()
        return removed

    # --- learning job state (docs/36-ai-pipeline.md, "Learning") -----------------------------------

    def _training_generation(self) -> int:
        return self.conn.execute("SELECT gen FROM learning WHERE id=1").fetchone()["gen"]

    def _bump_training_generation(self) -> None:
        """Called inside the same transaction as EVERY change to the training gallery. `gen` is
        what the learning job compares with the generation its last pass covered (`done_gen`), so
        "the model is out of date" survives a restart, and no caller has to remember to tell
        anyone: `KibbleStore` reports the change after the job ends (`take_learning_mark`)."""
        self.conn.execute("UPDATE learning SET gen = gen + 1 WHERE id = 1")
        self._learning_dirty = True

    def _count_answers(self, generation_before: int, answers: int = 1) -> None:
        """Counts human answers that changed the gallery -- the "N answers" the learning status
        reports. Answers that left the gallery as it was (a retry, a photo of a visit that
        trains nothing) need no learning and are not counted."""
        if self._training_generation() != generation_before and answers > 0:
            self.conn.execute("UPDATE learning SET answers = answers + ? WHERE id = 1", (answers,))
        self.conn.commit()

    def take_learning_mark(self) -> tuple[int, int] | None:
        """`(gen, answers)` once after the gallery changed, then `None` until it changes again."""
        if not self._learning_dirty:
            return None
        self._learning_dirty = False
        row = self.conn.execute("SELECT gen, answers FROM learning WHERE id=1").fetchone()
        return row["gen"], row["answers"]

    def learning_state(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT gen, done_gen, answers, answers_done, last_result, counts_done, last_error "
            "FROM learning WHERE id=1"
        ).fetchone()
        queued = self.conn.execute("SELECT COUNT(*) FROM learning_queue").fetchone()[0]
        return {
            "gen": row["gen"],
            "done_gen": row["done_gen"],
            "answers": row["answers"],
            "answers_done": row["answers_done"],
            "queued": queued,
            "last_result": json.loads(row["last_result"]) if row["last_result"] else None,
            "counts_done": json.loads(row["counts_done"]) if row["counts_done"] else None,
            "error": row["last_error"],
        }

    def training_counts_by_cat(self) -> dict[str, int]:
        return {
            row["cat"]: row["n"]
            for row in self.conn.execute("SELECT cat, COUNT(*) AS n FROM training GROUP BY cat")
        }

    def learning_set_counts(self, counts: Mapping[str, int]) -> None:
        """Records the per-cat training counts as the baseline "what was learned" is measured from."""
        self.conn.execute("UPDATE learning SET counts_done=? WHERE id=1", (json.dumps(dict(counts)),))
        self.conn.commit()

    def learning_complete(
        self, *, gen: int, answers: int, result: Mapping[str, Any], counts: Mapping[str, int]
    ) -> None:
        """A pass finished: it covered everything up to `gen` and `answers`."""
        self.conn.execute(
            "UPDATE learning SET done_gen=MAX(done_gen, ?), answers_done=MAX(answers_done, MIN(?, answers)), "
            "last_result=?, counts_done=?, last_error=NULL WHERE id=1",
            (gen, answers, json.dumps(dict(result)), json.dumps(dict(counts))),
        )
        self.conn.commit()

    def learning_failed(self, error: str) -> None:
        self.conn.execute("UPDATE learning SET last_error=? WHERE id=1", (error,))
        self.conn.commit()

    def touch_training_generation(self) -> None:
        """Marks the gallery changed without changing a row: CoralHub embeddings the model has
        not seen yet just landed (`CoralRecognizer.async_backfill`), so the next learning pass
        must rebuild even though nobody answered anything."""
        self._bump_training_generation()
        self.conn.commit()

    def enforce_training_caps(self) -> None:
        """The per-cat and whole-store training caps, run by the learning job just before it
        rebuilds -- label writes no longer pay for them inline."""
        for row in self.conn.execute("SELECT DISTINCT cat FROM training").fetchall():
            self._enforce_training_cap(row["cat"])
        self._enforce_total_training_cap()
        self.conn.commit()

    # --- storage summary ---------------------------------------------------------------------

    def storage_summary(self, retention_days: int) -> dict[str, Any]:
        used_bytes = self._media_used_bytes()
        events = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        oldest_row = self.conn.execute("SELECT MIN(start) AS m FROM events").fetchone()
        return {
            "used_bytes": used_bytes,
            "events": events,
            "retention_days": retention_days,
            "oldest": oldest_row["m"],
        }

    # --- identity summary for entities -------------------------------------------------------

    def identity_summary(self) -> DeviceIdentitySummary:
        """Every per-cat and device-wide stat here is derived from `events` rows a person can
        actually see -- `hidden=0` on every query. A split's own hidden PARENT row is excluded
        outright: `upsert_event`/`async_classify_event` keep refreshing its `cat`/`kind` from
        ALL of its samples fused together even after a split (nonsensical once the family truly
        holds two different cats), so counting it here on top of its own now-visible per-cat
        lane children would double an eat/visit against whichever cat that stale fused guess
        happens to name -- see docs/42-multi-cat.md, invariant 5."""
        cat_names = [r["name"] for r in self.conn.execute("SELECT name FROM cats").fetchall()]
        cats: dict[str, CatStats] = {}
        cutoff_48h = _now() - 48 * 3600
        for name in cat_names:
            last_seen_row = self.conn.execute(
                "SELECT MAX(start) AS m FROM events WHERE cat=? AND kind IN ('visit','eat') AND hidden=0",
                (name,),
            ).fetchone()
            last_meal_row = self.conn.execute(
                "SELECT MAX(start) AS m FROM events WHERE cat=? AND kind='eat' AND hidden=0", (name,)
            ).fetchone()
            recent = self.conn.execute(
                "SELECT start FROM events WHERE cat=? AND kind='eat' AND hidden=0 AND start>=?",
                (name, cutoff_48h),
            ).fetchall()
            present_row = self.conn.execute(
                "SELECT 1 FROM events WHERE cat=? AND open=1 AND hidden=0 LIMIT 1", (name,)
            ).fetchone()
            avatar_asset, _avatar_custom, avatar_updated = self._avatar_state(name)
            paused = self.auto_learn_paused(name)
            counts = self.training_counts(name)
            rolling = self.rolling_accuracy(name)
            saturated = autolearn.is_saturated(counts["total"], rolling)
            score, basis = autolearn.recognition_score(
                human_outcomes=self._recent_outcomes(name, autolearn.RECOGNITION_REVIEW_WINDOW),
                auto_confidences=self._recent_guess_confidences(name, autolearn.RECOGNITION_REVIEW_WINDOW),
                training_samples=counts["total"],
            )
            uploads = self.conn.execute(
                "SELECT COUNT(*) AS n FROM training WHERE cat=? AND source='upload'", (name,)
            ).fetchone()["n"]
            cats[name] = CatStats(
                last_seen=last_seen_row["m"], last_meal=last_meal_row["m"],
                recent_meals=tuple(r["start"] for r in recent), present=present_row is not None,
                avatar=avatar_asset["id"] if avatar_asset else None, avatar_updated=avatar_updated,
                learning_state=autolearn.learning_state(paused=paused, saturated=saturated),
                recognition_score=score, recognition_basis=basis,
                training_samples=counts["total"], training_uploads=uploads,
            )
        newest = self.conn.execute(
            "SELECT uid, cat, start FROM events WHERE cat IS NOT NULL AND kind IN ('visit','eat') "
            "AND hidden=0 ORDER BY start DESC LIMIT 1"
        ).fetchone()
        thumb = None
        if newest is not None:
            candidate = pick_thumb(self._thumb_candidates(newest["uid"]))
            if candidate is not None:
                thumb = _asset(self.entry_id, thumb_asset_id(candidate))
        return DeviceIdentitySummary(
            last_seen_pet=newest["cat"] if newest is not None else None,
            last_seen_pet_ts=newest["start"] if newest is not None else None,
            last_detection_thumb=thumb,
            cats=cats,
        )

    # --- retention -----------------------------------------------------------------------------

    def purge(self, retention_days: int) -> None:
        cutoff = _now() - retention_days * 86400
        self._purge_older_than(cutoff)
        self._enforce_media_cap()

    def _purge_older_than(self, cutoff: int) -> None:
        event_uids = [
            r["uid"]
            for r in self.conn.execute(
                "SELECT uid FROM events WHERE open=0 AND start<? AND parent_uid IS NULL", (cutoff,)
            ).fetchall()
        ]
        for uid in event_uids:
            self._delete_event(uid)
        feed_uids = [
            r["uid"] for r in self.conn.execute("SELECT uid FROM feeds WHERE ts<?", (cutoff,)).fetchall()
        ]
        for uid in feed_uids:
            self._delete_feed(uid)
        self.conn.commit()

    def _delete_event(self, uid: str) -> None:
        for child_uid in [
            r["uid"] for r in self.conn.execute("SELECT uid FROM events WHERE parent_uid=?", (uid,)).fetchall()
        ]:
            self._delete_event(child_uid)
        row = self.conn.execute("SELECT scene, before, after FROM events WHERE uid=?", (uid,)).fetchone()
        if row is not None:
            for asset in (row["scene"], row["before"], row["after"]):
                if asset:
                    self.delete_media(asset)
        for r in self.conn.execute("SELECT body, face FROM samples WHERE event_uid=?", (uid,)).fetchall():
            if r["body"]:
                self.delete_media(r["body"])
            if r["face"]:
                self.delete_media(r["face"])
        self.conn.execute(
            "DELETE FROM coral_embeddings WHERE row_kind='sample' AND row_uid IN "
            "(SELECT uid FROM samples WHERE event_uid=?)", (uid,)
        )
        self.conn.execute("DELETE FROM samples WHERE event_uid=?", (uid,))
        self.conn.execute("DELETE FROM events WHERE uid=?", (uid,))

    def _delete_feed(self, uid: str) -> None:
        row = self.conn.execute("SELECT before, after FROM feeds WHERE uid=?", (uid,)).fetchone()
        if row is not None:
            for asset in (row["before"], row["after"]):
                if asset:
                    self.delete_media(asset)
        self.conn.execute("DELETE FROM feeds WHERE uid=?", (uid,))

    def _enforce_media_cap(self) -> None:
        """If `media/` exceeds the cap, deletes whole oldest day-directories (and every event/
        feed whose start falls on that date, regardless of the time-based cutoff above) until
        back under the target ratio."""
        by_date = self._media_bytes_by_date()
        total = sum(b for _, b in by_date)
        if total <= MEDIA_CAP_BYTES:
            return
        target = int(MEDIA_CAP_BYTES * MEDIA_CAP_TARGET_RATIO)
        for date, size in by_date:
            if total <= target:
                break
            day_start = int(time.mktime(time.strptime(date, "%Y-%m-%d")))
            day_end = day_start + 86400
            for uid in [
                r["uid"]
                for r in self.conn.execute(
                    "SELECT uid FROM events WHERE start>=? AND start<?", (day_start, day_end)
                ).fetchall()
            ]:
                self._delete_event(uid)
            for uid in [
                r["uid"]
                for r in self.conn.execute(
                    "SELECT uid FROM feeds WHERE ts>=? AND ts<?", (day_start, day_end)
                ).fetchall()
            ]:
                self._delete_feed(uid)
            day_dir = self.media_root / date
            if day_dir.exists() and not any(day_dir.rglob("*")):
                day_dir.rmdir()
            total -= size
        self.conn.commit()


# --- The async facade ---------------------------------------------------------------------------


class KibbleStore:
    """Async, executor-backed facade over `_SyncStore` for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._root = Path(hass.config.path(DOMAIN, entry_id))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"kibble-store-{entry_id[:8]}")
        self._sync: _SyncStore | None = None
        self._learning_listener: Callable[[int, int], None] | None = None

    def set_learning_listener(self, listener: Callable[[int, int], None] | None) -> None:
        """`listener(gen, answers)` is called on the event loop after any store job that changed
        the training gallery -- a label, an upload, auto-learn, an eviction, a removal --
        so the learning job hears about every one of them without each caller having to say so."""
        self._learning_listener = listener

    def _job(self, fn: Any, *args: Any) -> Any:
        """What actually runs on the store thread: `fn`, then the learning hand-off."""
        try:
            return fn(*args)
        finally:
            mark = self._sync.take_learning_mark() if self._sync is not None else None
            if mark is not None and self._learning_listener is not None:
                try:
                    self._hass.loop.call_soon_threadsafe(self._learning_listener, *mark)
                except RuntimeError:  # the loop is already closed: Home Assistant is stopping
                    pass

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await self._hass.loop.run_in_executor(self._executor, self._job, fn, *args)

    async def async_setup(self) -> None:
        def _init() -> _SyncStore:
            return _SyncStore(self._root, self._entry_id)

        self._sync = await self._run(_init)

    async def async_close(self) -> None:
        if self._sync is not None:
            await self._run(self._sync.close)
        self._executor.shutdown(wait=False)

    # media/asset ingest --------------------------------------------------------------------

    async def async_media_exists(self, date: str, filename: str) -> bool:
        return await self._run(self._sync.media_exists, date, filename)

    async def async_write_media(self, date: str, filename: str, data: bytes) -> str:
        return await self._run(self._sync.write_media, date, filename, data)

    async def async_upsert_event(self, **kwargs: Any) -> None:
        await self._run(lambda: self._sync.upsert_event(**kwargs))

    async def async_close_orphaned_open(self, live_open_uids: Collection[str]) -> int:
        return await self._run(self._sync.close_orphaned_open, live_open_uids)

    async def async_event_asset_fields(self, uid: str) -> dict[str, str | None] | None:
        return await self._run(self._sync.event_asset_fields, uid)

    async def async_set_event_clip(self, uid: str, *, clip_id: str, clip_start_ms: int, clip_end_ms: int) -> None:
        await self._run(
            lambda: self._sync.set_event_clip(
                uid, clip_id=clip_id, clip_start_ms=clip_start_ms, clip_end_ms=clip_end_ms
            )
        )

    async def async_clear_event_clip(self, uid: str) -> None:
        await self._run(self._sync.clear_event_clip, uid)

    async def async_resolve_event_clip(self, uid: str) -> dict[str, Any] | None:
        return await self._run(self._sync.resolve_event_clip, uid)

    async def async_events_needing_clip_link(self, cutoff: int) -> list[tuple[str, int, int]]:
        return await self._run(self._sync.events_needing_clip_link, cutoff)

    async def async_existing_sample_uids(self, event_uid: str) -> set[str]:
        return await self._run(self._sync.existing_sample_uids, event_uid)

    async def async_session_uid_for_sample(self, sample_uid: str) -> str | None:
        return await self._run(self._sync.session_uid_for_sample, sample_uid)

    async def async_insert_sample(self, **kwargs: Any) -> None:
        await self._run(lambda: self._sync.insert_sample(**kwargs))

    async def async_features_for_event(self, event_uid: str) -> list[identity.Features]:
        return await self._run(self._sync.features_for_event, event_uid)

    async def async_sample_uids_ordered(self, event_uid: str) -> list[str]:
        return await self._run(self._sync.sample_uids_ordered, event_uid)

    async def async_features_by_sid_for_family(self, uid: str) -> dict[int, list[identity.Features]]:
        return await self._run(self._sync.features_by_sid_for_family, uid)

    async def async_vision_cats_for_event(self, device_event_id: int) -> dict[int, str | None]:
        return await self._run(self._sync.vision_cats_for_event, device_event_id)

    async def async_set_event_classification(self, uid: str, **kwargs: Any) -> bool:
        return await self._run(lambda: self._sync.set_event_classification(uid, **kwargs))

    async def async_upsert_feed(self, **kwargs: Any) -> None:
        await self._run(lambda: self._sync.upsert_feed(**kwargs))

    async def async_feed_asset_fields(self, uid: str) -> dict[str, str | None] | None:
        return await self._run(self._sync.feed_asset_fields, uid)

    async def async_get_bowl_fill_learning(self, bucket: str) -> tuple[float, int] | None:
        return await self._run(self._sync.get_bowl_fill_learning, bucket)

    async def async_set_bowl_fill_learning(self, bucket: str, fill_per_portion: float, samples: int) -> None:
        await self._run(lambda: self._sync.set_bowl_fill_learning(bucket, fill_per_portion, samples))

    async def async_events_for_reclassify(self, cutoff: int) -> list[str]:
        return await self._run(self._sync.events_for_reclassify, cutoff)

    async def async_all_training_features(self) -> list[tuple[str, identity.Features]]:
        return await self._run(self._sync.all_training_features)

    async def async_coral_embedding(
        self, row_kind: str, row_uid: str, crop_kind: str, model_id: str
    ) -> bytes | None:
        return await self._run(self._sync.coral_embedding, row_kind, row_uid, crop_kind, model_id)

    async def async_set_coral_embeddings(
        self, row_kind: str, items: Sequence[tuple[str, str, str, bytes]]
    ) -> None:
        await self._run(self._sync.set_coral_embeddings, row_kind, list(items))

    async def async_training_rows_needing_coral(
        self, body_model: str, face_model: str, limit: int
    ) -> list[dict[str, Any]]:
        return await self._run(self._sync.training_rows_needing_coral, body_model, face_model, limit)

    async def async_samples_needing_coral(
        self, body_model: str, face_model: str, limit: int
    ) -> list[dict[str, Any]]:
        return await self._run(self._sync.samples_needing_coral, body_model, face_model, limit)

    async def async_samples_needing_coral_for_event(
        self, event_uid: str, body_model: str, face_model: str
    ) -> list[dict[str, Any]]:
        return await self._run(self._sync.samples_needing_coral_for_event, event_uid, body_model, face_model)

    async def async_all_training_coral_features(
        self, body_model: str, face_model: str
    ) -> list[tuple[str, coral_identity.CoralFeatures]]:
        return await self._run(self._sync.all_training_coral_features, body_model, face_model)

    async def async_coral_features_for_event(
        self, event_uid: str, body_model: str, face_model: str
    ) -> list[coral_identity.CoralFeatures]:
        return await self._run(self._sync.coral_features_for_event, event_uid, body_model, face_model)

    async def async_coral_features_by_sid_for_family(
        self, uid: str, body_model: str, face_model: str
    ) -> dict[int, list[coral_identity.CoralFeatures]]:
        return await self._run(self._sync.coral_features_by_sid_for_family, uid, body_model, face_model)

    # card-facing queries ---------------------------------------------------------------------

    async def async_timeline_page(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        return await self._run(lambda: self._sync.timeline_page(limit=limit, cursor=cursor))

    async def async_event_detail(self, uid: str) -> dict[str, Any] | None:
        return await self._run(self._sync.event_detail, uid)

    async def async_label_events(self, uids: Sequence[str], label: str) -> list[dict[str, Any]]:
        return await self._run(self._sync.label_events, list(uids), label)

    async def async_reconcile_queued_sessions(self) -> int:
        return await self._run(self._sync.reconcile_queued_sessions)

    async def async_enforce_training_caps(self) -> None:
        await self._run(self._sync.enforce_training_caps)

    async def async_learning_state(self) -> dict[str, Any]:
        return await self._run(self._sync.learning_state)

    async def async_training_counts_by_cat(self) -> dict[str, int]:
        return await self._run(self._sync.training_counts_by_cat)

    async def async_learning_set_counts(self, counts: Mapping[str, int]) -> None:
        await self._run(self._sync.learning_set_counts, counts)

    async def async_learning_complete(
        self, *, gen: int, answers: int, result: Mapping[str, Any], counts: Mapping[str, int]
    ) -> None:
        await self._run(lambda: self._sync.learning_complete(gen=gen, answers=answers, result=result, counts=counts))

    async def async_learning_failed(self, error: str) -> None:
        await self._run(self._sync.learning_failed, error)

    async def async_touch_training_generation(self) -> None:
        await self._run(self._sync.touch_training_generation)

    async def async_replan_session(
        self, uid: str, scores: Mapping[int, sessions.IdentityScores] | None = None
    ) -> bool:
        return await self._run(self._sync.replan_session, uid, scores)

    async def async_session_cats(
        self, uid: str, cats: Sequence[tuple[str, bool]]
    ) -> dict[str, Any] | None:
        return await self._run(self._sync.set_session_cats, uid, list(cats))

    async def async_session_subject(
        self,
        uid: str,
        sid: int,
        label: str,
        scores: Mapping[int, sessions.IdentityScores] | None = None,
    ) -> dict[str, Any] | None:
        return await self._run(self._sync.set_session_subject, uid, sid, label, scores)

    async def async_session_ate(self, uid: str, ate: Sequence[str]) -> dict[str, Any] | None:
        return await self._run(self._sync.set_session_ate, uid, list(ate))

    async def async_label_sample(
        self,
        sample_uid: str,
        label: str,
        scores: Mapping[int, sessions.IdentityScores] | None = None,
    ) -> dict[str, Any] | None:
        return await self._run(self._sync.label_sample, sample_uid, label, scores)

    async def async_review_photos_page(
        self, *, limit: int, cursor: str | None, retention_cutoff: int
    ) -> dict[str, Any]:
        return await self._run(
            lambda: self._sync.review_photos_page(limit=limit, cursor=cursor, retention_cutoff=retention_cutoff)
        )

    async def async_cats(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.cats)

    async def async_cats_overview(self, retention_days: int) -> dict[str, Any]:
        return await self._run(self._sync.cats_overview, retention_days)

    async def async_add_cat(self, name: str) -> None:
        await self._run(self._sync.add_cat, name)

    async def async_delete_cat(self, name: str) -> None:
        await self._run(self._sync.delete_cat, name)

    async def async_training_counts(self, cat: str) -> dict[str, int]:
        return await self._run(self._sync.training_counts, cat)

    async def async_cat_avatar(self, cat: str) -> dict[str, str] | None:
        return await self._run(self._sync.cat_avatar, cat)

    async def async_avatar_info(self, cat: str) -> dict[str, Any]:
        return await self._run(self._sync.avatar_info, cat)

    async def async_set_cat_avatar(self, cat: str, data: bytes) -> dict[str, str] | None:
        return await self._run(self._sync.set_cat_avatar, cat, data)

    async def async_set_cat_avatar_from_asset(self, cat: str, asset_id: str) -> dict[str, str] | None:
        return await self._run(self._sync.set_cat_avatar_from_asset, cat, asset_id)

    async def async_clear_cat_avatar(self, cat: str) -> bool:
        return await self._run(self._sync.clear_cat_avatar, cat)

    async def async_cat_exists(self, name: str) -> bool:
        return await self._run(self._sync.cat_exists, name)

    async def async_add_upload_training(self, **kwargs: Any) -> dict[str, Any] | None:
        return await self._run(lambda: self._sync.add_upload_training(**kwargs))

    async def async_add_auto_training(self, **kwargs: Any) -> bool:
        return await self._run(lambda: self._sync.add_auto_training(**kwargs))

    async def async_free_disk_bytes(self) -> int:
        return await self._run(self._sync.free_disk_bytes)

    async def async_event_training_context(self, uid: str) -> dict[str, Any] | None:
        return await self._run(self._sync.event_training_context, uid)

    async def async_sample_guesses(self, event_uid: str) -> list[dict[str, Any]]:
        return await self._run(self._sync.sample_guesses, event_uid)

    async def async_training_feats_for_cat(self, cat: str, mode: str | None) -> list[Any]:
        return await self._run(self._sync.training_feats_for_cat, cat, mode)

    async def async_auto_learn_paused(self, cat: str) -> bool:
        return await self._run(self._sync.auto_learn_paused, cat)

    async def async_clear_training(self, cat: str | None, *, keep_uploads: bool) -> int:
        return await self._run(lambda: self._sync.clear_training(cat, keep_uploads=keep_uploads))

    async def async_training_page(self, *, cat: str, limit: int, cursor: str | None) -> dict[str, Any]:
        return await self._run(lambda: self._sync.training_page(cat=cat, limit=limit, cursor=cursor))

    async def async_training_remove(self, uids: Sequence[str]) -> int:
        return await self._run(self._sync.training_remove, list(uids))

    async def async_storage_summary(self, retention_days: int) -> dict[str, Any]:
        return await self._run(self._sync.storage_summary, retention_days)

    async def async_identity_summary(self) -> DeviceIdentitySummary:
        return await self._run(self._sync.identity_summary)

    async def async_purge(self, retention_days: int) -> None:
        await self._run(self._sync.purge, retention_days)

    # --- vision judge (docs/40-vision-judge.md) -----------------------------------------------

    async def async_events_needing_judge(self, cutoff: int, limit: int) -> list[str]:
        return await self._run(self._sync.events_needing_judge, cutoff, limit)

    async def async_judge_event_context(self, uid: str) -> dict[str, Any] | None:
        return await self._run(self._sync.judge_event_context, uid)

    async def async_event_sample_candidates(self, event_uid: str) -> list[ThumbCandidate]:
        return await self._run(self._sync.event_sample_candidates, event_uid)

    async def async_apply_judge_verdict(self, uid: str, **kwargs: Any) -> bool:
        return await self._run(lambda: self._sync.apply_judge_verdict(uid, **kwargs))

    async def async_invalidate_scene_asset(self, uid: str, old_asset_id: str | None) -> None:
        await self._run(self._sync.invalidate_scene_asset, uid, old_asset_id)

    async def async_cats_needing_description(self) -> list[str]:
        return await self._run(self._sync.cats_needing_description)

    async def async_set_cat_description(self, cat: str, description: str) -> None:
        await self._run(self._sync.set_cat_description, cat, description)

    async def async_cat_descriptions(self) -> dict[str, str | None]:
        return await self._run(self._sync.cat_descriptions)

    async def async_training_crop_for_mode(self, cat: str, mode: str) -> dict[str, Any] | None:
        return await self._run(self._sync.training_crop_for_mode, cat, mode)

    async def async_judge_diagnostics(self) -> dict[str, Any]:
        return await self._run(self._sync.judge_diagnostics)

    def asset_path(self, asset_id: str) -> Path | None:
        """Synchronous on purpose: `views.py`'s HTTP handler only needs a path to serve from
        the executor-free `aiohttp` file-send path, never SQLite."""
        return resolve_asset_path(self._root, asset_id)
