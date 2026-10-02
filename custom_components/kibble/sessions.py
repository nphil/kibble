"""Pure planners for conservative per-cat parts and legacy sequential session splits.

The feeder tracks one device session through a cat swap. Automatic multi-cat parts are only
created when distinct, confidently identified subjects share a frame; human photo and subject
labels take precedence, and an explicit session cat set is authoritative. Older firmware keeps
the chronological fallback in plan_split. No function here uses SQLite or Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import identity

# Disables automatic session splits without disabling explicit human cat sets.
SESSION_SPLIT_ENABLED = True

# A sequential run must contain at least this many confident samples.
MIN_RUN_LENGTH = 2
MIN_RUN_CONFIDENCE = 0.8

_NOT_A_CAT = identity.NOT_A_CAT


@dataclass(frozen=True, slots=True)
class SampleGuess:
    """Exactly what run detection needs from one sample. `samples` passed to `plan_split` must
    already be ordered by `t` ascending -- this module never sorts, so a caller error there
    would silently produce nonsense runs rather than fail loudly; `store.py`'s own callers read
    samples `ORDER BY t`, matching every other per-event sample query in this codebase."""

    uid: str
    t: int
    guess: str | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Segment:
    """One planned per-cat session: a contiguous slice of `[start, end)` -- segments partition
    the whole input session with no gap and no overlap, one segment's `end` equal to the next
    one's `start` -- the cat a qualifying run of samples named, that run's own mean confidence
    (only over the run's OWN qualifying samples, never any non-qualifying sample the boundary
    rule happens to sweep into the segment), and every sample uid (qualifying or not) assigned to
    this segment by timestamp."""

    cat: str
    start: int
    end: int
    confidence: float
    sample_uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanningSample:
    """Database-free sample facts used by `plan_parts`. A NULL source is a legacy
    per-photo review, never a subject-wide review. `has_crop` means a usable crop is stored."""

    uid: str
    t: int
    sid: int | None
    frame_k: int | None
    has_crop: bool
    guess: str | None
    guess_confidence: float | None
    review: str | None = None
    review_src: str | None = None

@dataclass(frozen=True, slots=True)
class SubjectSpan:
    """The feeder's summary for one subject; sample times are the legacy fallback."""

    sid: int
    first: int
    last: int
    eat_start: int | None


@dataclass(frozen=True, slots=True)
class IdentityScores:
    """One active engine's aggregate class probabilities and its own decision gates."""

    probabilities: tuple[tuple[str, float], ...]
    top_threshold: float
    margin: float


@dataclass(frozen=True, slots=True)
class CatPart:
    """One planned per-cat part; row ids and session-relative times belong to the store."""

    cat: str
    ate: bool
    reviewed: bool
    confidence: float | None
    sample_uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SubjectGroup:
    key: int | str
    samples: tuple[PlanningSample, ...]
    frames: frozenset[int]
    first: int
    last: int
    eat_start: int | None
    subject_label: str | None
    engine_decision: tuple[str, float] | None
@dataclass(frozen=True, slots=True)
class _Run:
    guess: str | None
    items: tuple[SampleGuess, ...]

    def qualifies(self) -> bool:
        if self.guess is None or self.guess == _NOT_A_CAT:
            return False
        if len(self.items) < MIN_RUN_LENGTH:
            return False
        return all(s.confidence is not None and s.confidence >= MIN_RUN_CONFIDENCE for s in self.items)

    def mean_confidence(self) -> float:
        confidences = [s.confidence for s in self.items if s.confidence is not None]
        return sum(confidences) / len(confidences) if confidences else 0.0


def _best_decision(scores: IdentityScores | None) -> tuple[str, float] | None:
    if scores is None:
        return None
    ranked = sorted(
        ((label, probability) for label, probability in scores.probabilities
         if label not in (_NOT_A_CAT, "unknown", "skip")),
        key=lambda pair: (-pair[1], pair[0]),
    )
    if not ranked:
        return None
    label, probability = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if probability < scores.top_threshold or probability - runner_up < scores.margin:
        return None
    return label, probability


def _groups_cooccur(a: _SubjectGroup, b: _SubjectGroup) -> bool:
    return bool(a.frames and b.frames and not a.frames.isdisjoint(b.frames))


def _modal_subject_label(samples: Sequence[PlanningSample]) -> str | None:
    labels = [
        sample.review for sample in samples
        if sample.review_src == "subject" and sample.review not in (None, "", "follow")
    ]
    if not labels:
        return None
    counts = {label: labels.count(label) for label in set(labels)}
    return min(counts, key=lambda label: (-counts[label], label))


def _sample_photo_cat(sample: PlanningSample) -> str | None:
    if sample.review_src not in (None, "photo"):
        return None
    if sample.review in (None, "", "skip", "unknown", "follow", _NOT_A_CAT):
        return None
    return sample.review


def _sample_group_cat(sample: PlanningSample, group: _SubjectGroup) -> str | None:
    photo_cat = _sample_photo_cat(sample)
    if photo_cat is not None:
        return photo_cat
    if group.subject_label is not None:
        return group.subject_label if group.subject_label != _NOT_A_CAT else None
    return group.engine_decision[0] if group.engine_decision is not None else None


def _make_groups(
    samples: Sequence[PlanningSample],
    scores: Mapping[int, IdentityScores],
    subject_spans: Mapping[int, SubjectSpan],
) -> list[_SubjectGroup]:
    grouped: dict[int | str, list[PlanningSample]] = {}
    for sample in samples:
        key: int | str = sample.sid if sample.sid is not None else f"sample:{sample.uid}"
        grouped.setdefault(key, []).append(sample)
    groups: list[_SubjectGroup] = []
    for key, rows in grouped.items():
        rows.sort(key=lambda item: (item.t, item.uid))
        sid = key if isinstance(key, int) else None
        span = subject_spans.get(sid) if sid is not None else None
        crop_count = sum(sample.has_crop for sample in rows)
        decision = _best_decision(scores.get(sid)) if sid is not None and crop_count >= 2 else None
        groups.append(_SubjectGroup(
            key=key,
            samples=tuple(rows),
            frames=frozenset(sample.frame_k for sample in rows if sample.frame_k is not None),
            first=span.first if span is not None else min(sample.t for sample in rows),
            last=span.last if span is not None else max(sample.t for sample in rows),
            eat_start=span.eat_start if span is not None else None,
            subject_label=_modal_subject_label(rows),
            engine_decision=decision,
        ))
    return groups


def _ordered_uids(samples: Sequence[PlanningSample]) -> tuple[str, ...]:
    return tuple(sample.uid for sample in sorted(samples, key=lambda item: (item.t, item.uid)))


def _parts_for_categories(
    samples: Sequence[PlanningSample],
    sample_group: Mapping[str, _SubjectGroup],
    categories: Sequence[tuple[str, bool]],
    *,
    reviewed: bool,
    confidence_by_cat: Mapping[str, float] | None = None,
) -> list[CatPart]:
    assigned: dict[str, str | None] = {}
    counts = [0] * len(categories)
    indexes = {cat: index for index, (cat, _ate) in enumerate(categories)}
    for sample in samples:
        cat = _sample_group_cat(sample, sample_group[sample.uid])
        index = indexes.get(cat) if cat is not None else None
        assigned[sample.uid] = cat if index is not None else None
        if index is not None:
            counts[index] += 1
    main_cat = categories[max(range(len(categories)), key=lambda index: (counts[index], -index))][0]
    parts: list[CatPart] = []
    for cat, ate in categories:
        part_samples = [
            sample for sample in samples
            if assigned[sample.uid] == cat or (assigned[sample.uid] is None and cat == main_cat)
        ]
        parts.append(CatPart(
            cat=cat, ate=ate, reviewed=reviewed,
            confidence=None if reviewed else (confidence_by_cat or {}).get(cat, 0.0),
            sample_uids=_ordered_uids(part_samples),
        ))
    return parts


def plan_parts(
    samples: Sequence[PlanningSample],
    scores: Mapping[int, IdentityScores],
    subject_spans: Mapping[int, SubjectSpan],
    *,
    human_cats: Sequence[tuple[str, bool]] | None = None,
) -> list[CatPart] | None:
    """Plan one part per cat, or return None when a session should stay unsplit.

    Human photo reviews beat subject reviews; subject reviews beat the engine. A human cat set
    is exact, including cats with no photos. Otherwise only confident, crop-supported groups
    sharing a frame can create an automatic split. Sid-less samples are independent groups and
    are retained when a split exists, but never provide automatic split evidence.
    """
    if not samples and human_cats is None:
        return None
    groups = _make_groups(samples, scores, subject_spans)
    sample_group = {sample.uid: group for group in groups for sample in group.samples}
    if human_cats is not None:
        categories_by_cat: dict[str, tuple[str, bool]] = {}
        for cat, ate in human_cats:
            previous = categories_by_cat.get(cat)
            categories_by_cat[cat] = (cat, bool(ate) or (previous[1] if previous else False))
        categories = list(categories_by_cat.values())
        if len(categories) < 2:
            return None
        return _parts_for_categories(samples, sample_group, categories, reviewed=True)
    eligible = [
        group for group in groups
        if group.engine_decision is not None
        and (group.subject_label is None or group.subject_label == group.engine_decision[0])
    ]
    split_keys: set[int | str] = set()
    split_cats: set[str] = set()
    for i, a in enumerate(eligible):
        for b in eligible[i + 1:]:
            cat_a, cat_b = a.engine_decision[0], b.engine_decision[0]
            if cat_a != cat_b and _groups_cooccur(a, b):
                split_keys.update((a.key, b.key))
                split_cats.update((cat_a, cat_b))
    if len(split_cats) < 2:
        return None

    ordered = sorted(
        split_cats,
        key=lambda cat: (min(group.first for group in eligible if group.engine_decision[0] == cat), cat),
    )
    categories = [
        (cat, any(
            group.key in split_keys and group.engine_decision[0] == cat and group.eat_start is not None
            for group in eligible
        ))
        for cat in ordered
    ]
    confidence_by_cat = {
        cat: max(group.engine_decision[1] for group in eligible if group.engine_decision[0] == cat)
        for cat in ordered
    }
    return _parts_for_categories(
        samples, sample_group, categories, reviewed=False, confidence_by_cat=confidence_by_cat
    )

def _runs(samples: Sequence[SampleGuess]) -> list[_Run]:
    """Maximal contiguous runs of the same `guess` value, in the given (time) order."""
    runs: list[_Run] = []
    for s in samples:
        if runs and runs[-1].guess == s.guess:
            runs[-1] = _Run(guess=s.guess, items=(*runs[-1].items, s))
        else:
            runs.append(_Run(guess=s.guess, items=(s,)))
    return runs


def plan_split(samples: Sequence[SampleGuess], session_start: int, session_end: int) -> list[Segment] | None:
    """Plans a split of one session's `samples` (time-ordered by `t`) into per-cat segments, or
    `None` when the evidence is not strong enough to justify one.

    Splits only when at least two *qualifying* runs (`_Run.qualifies`: a contiguous run of the
    same real cat -- never `None`, never `not_a_cat` -- at least `MIN_RUN_LENGTH` samples long,
    every one of them at least `MIN_RUN_CONFIDENCE` confident) name at least two DIFFERENT cats.
    Two qualifying runs of the SAME cat (a low-confidence blip in the middle of one cat's own
    visit breaking an otherwise-contiguous run, say) is not evidence of a second cat and must
    not split. A single qualifying run, a run one sample too short, or a run any of whose
    samples falls under the confidence floor, is the same: `None`.

    Every sample -- qualifying or not, including a low-confidence or `not_a_cat` outlier that
    never itself forms a run long enough to matter -- is assigned to exactly one resulting
    segment by its own timestamp, so nothing is silently dropped. The boundary between two
    adjacent qualifying runs sits at the midpoint (integer floor division, so an odd gap is
    biased toward the earlier segment) between the last sample of one and the first sample of
    the next; the very first segment's range is open below `session_start` (so a sample timed
    even slightly before it, which should not happen, still lands somewhere) and the very last
    segment's range is open above `session_end`, so between them the segments cover every
    possible timestamp with no gap.
    """
    runs = _runs(samples)
    qualifying = [r for r in runs if r.qualifies()]
    if len({r.guess for r in qualifying}) < 2:
        return None

    boundaries = [(prev.items[-1].t + nxt.items[0].t) // 2 for prev, nxt in zip(qualifying, qualifying[1:])]
    edges = [session_start, *boundaries, session_end]

    segments: list[Segment] = []
    last_index = len(qualifying) - 1
    for i, run in enumerate(qualifying):
        start, end = edges[i], edges[i + 1]
        if i == 0 and i == last_index:
            in_range = list(samples)
        elif i == 0:
            in_range = [s for s in samples if s.t < end]
        elif i == last_index:
            in_range = [s for s in samples if s.t >= start]
        else:
            in_range = [s for s in samples if start <= s.t < end]
        segments.append(
            Segment(
                cat=run.guess,  # type: ignore[arg-type]  -- qualifies() already excluded None
                start=start,
                end=end,
                confidence=run.mean_confidence(),
                sample_uids=tuple(s.uid for s in in_range),
            )
        )
    return segments
