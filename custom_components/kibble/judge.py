"""Second-opinion vision judge: sends a closed meal/uncertain-visit event's own photos to a
VLM (llama-swap, docs/40-vision-judge.md) to catch the local recognizer's mistakes -- especially
the night false "meals" a cold static-clutter memory produces (docs/40-vision-judge.md, "Why") and
the day/night identity confusion its hand-built histogram classifier is prone to. Runs entirely
off the event loop's critical path, strictly after `ingest.py` has already durably written an
event's evidence; never blocks ingest, never trains the local recognizer (`autolearn.
is_judge_sourced` is the other half of that guarantee), and never touches a row a human has
already reviewed.

Two independent jobs share one model endpoint and the same concurrency-1/timeout/retry/busy-
deferral plumbing (`VisionJudge._call_model_when_free`):

- `ensure_descriptions` (Contract 3): fills a blank `cats.description` once per cat, from its
  avatar plus up to one day and one IR training crop. Run once at startup as its OWN background
  task (never blocking entry setup -- a busy-model deferral can take up to an hour), before any
  event is judged, so the very first verdicts already see a roster with real coat descriptions
  instead of blank lines.
- `schedule_judge`/the debounced background arc it starts (Contract 1/2/4/5): judges one event's
  current best evidence and applies the rule table below. Modelled on `eating_clips.ClipLinker`'s
  own background-task-per-uid shape, but debounced rather than retried -- a scene replacement or
  a late-arriving `after` frame restarts the delay instead of the request being retried against
  evidence that has not changed.

Neither job is ever urgent: `_call_model_when_free` checks llama-swap's own `GET /running`
before every request and defers (10, then 30, then 60 minutes) while the judge model is unloaded
and an on-demand model (non-zero ttl -- Nitin's own manually-loaded heavy model) is running,
since llama-swap's groups are exclusive and a request for the judge model would evict it
mid-conversation.
Exhausting every deferral leaves the event unjudged, the same outcome as the judge being
unreachable; a failed `/running` check itself is ALSO treated as "judge unreachable" (proceed to
the ordinary request, which fails/retries/gives up through the usual path) rather than a reason
to defer -- the two failure modes are indistinguishable and neither should invent a retry
schedule the other doesn't already have.

Rule table (thresholds are module constants, set from the 2026-09-25 bake-off -- the "Bake-off"
section of docs/40-vision-judge.md):

(a) `cat_present=False` with `confidence` at or below `JUDGE_SUPPRESS_MAX_CONFIDENCE`:
    `identity_status` becomes `not_a_cat`, `cat` cleared, `reviewed` left at 0 -- the existing
    timeline filter (`identity_status IS NOT 'not_a_cat'`) hides it, and a human can still
    override it later. Qwen3-VL-4B's `confidence` reads as its belief that a cat is present (its
    correct no-cat verdicts measured 0.0-0.1), so a confident "no cat" is a LOW number; a "no
    cat" that still rates a cat likely is self-contradictory and changes nothing (rule c).
(b) `cat_present=True` naming an enrolled cat at or above `JUDGE_IDENTIFY_MIN_CONFIDENCE`, NOT
    flagged `multiple_cats` (the bake-off's own worst identity misses were exactly the
    ambiguous-which-cat multi-subject frames), and the event's current identity is `None`/
    `unknown`, or `auto` naming a DIFFERENT cat: `cat`/`identity_status='auto'`/`confidence` are
    overwritten with the verdict's own.
(c) Anything else: the verdict is still recorded (`judge_*` columns), but nothing about the
    event's own identity fields changes.

Every verdict is recorded regardless of which rule fired -- `judge_evidence` is what makes an
unchanged event skip a repeat request, not whether a rule applied.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Container, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import crop_geometry, identity
from .const import CONF_VISION_JUDGE_MODEL, CONF_VISION_JUDGE_URL, DEFAULT_VISION_JUDGE_MODEL
from .store import ThumbCandidate, pick_thumb

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import KibbleCoordinator
    from .store import KibbleStore

_LOGGER = logging.getLogger(__name__)

# --- Prompt wording -----------------------------------------------------------------------------
# Confirmed production strings from the 2026-09-25 bake-off (docs/40-vision-judge.md,
# "Bake-off") -- every caller only ever
# references the constants below, never inline text.

JUDGE_SYSTEM_PROMPT = (
    "You are a vision assistant for a home cat feeder's camera. Photos come from a fisheye "
    "lens above a steel food bowl. At night the camera switches to infrared (IR): frames are "
    "grayscale, the steel bowl often shows bright specular reflections that can look like a "
    "pale animal shape, and cats can appear washed out or textureless. Close-up crops are "
    "auto-selected by a motion tracker and can be blurry, poorly framed, or centered on the "
    "wrong part of the room. Judge only what is actually visible in the images; do not assume "
    "a cat is present just because the tracker flagged the frame."
)

JUDGE_QUESTION = (
    "Given the images above (camera scene, then close-up crops, in that order), is an enrolled "
    'cat present? If yes, which one (or "unknown" if a cat is clearly present but you cannot '
    "tell which enrolled cat it is)? Set multiple_cats true only if more than one enrolled cat "
    'is visible across the images. Keep "reason" to at most 20 words. Answer strictly with the '
    "JSON schema."
)

# Contract 3's description-fill prompt reuses `JUDGE_SYSTEM_PROMPT` verbatim ("same feeder-camera
# framing" -- docs/40-vision-judge.md) rather than a separate system message.
DESCRIPTION_QUESTION = (
    "Here are 1-3 photos of one household cat from the feeder's camera (some may be grayscale "
    "IR, some may be blurry close-ups). Describe this cat's coat in one short phrase suitable "
    "for a roster line: base color, pattern (solid / tabby / tuxedo / other), and any one "
    "distinctive marking if visible. At most 12 words, no sentence, just the phrase."
)

# Wire-format sentinels for the verdict's `cat` field -- Contract 1, verbatim.
_CAT_NONE = "none"
_CAT_UNKNOWN = "unknown"

# Whether to include one reference image per enrolled cat (Contract 2, item 2). Measured false:
# the bake-off's own strategy ablation found reference images cost ~9 points of presence
# accuracy (87.9% vs 97.0%) regardless of whether description text is also present -- the model
# sometimes describes the REFERENCE photo's cat as evidence for the target scene, especially on
# an otherwise-empty frame. Text descriptions alone cost nothing and do not have this failure
# mode. See docs/40-vision-judge.md, "Bake-off".
JUDGE_INCLUDE_REFERENCE_IMAGES = False

JUDGE_MAX_CROPS = 3  # Contract 2: "up to 3 close-ups, best score first"
JUDGE_CONCURRENCY = 1
JUDGE_REQUEST_TIMEOUT_S = 45.0
JUDGE_REQUEST_TIMEOUT = ClientTimeout(total=JUDGE_REQUEST_TIMEOUT_S)
JUDGE_MAX_RETRIES = 1  # one retry -- two attempts total per request
JUDGE_MAX_TOKENS = 256
DESCRIPTION_MAX_TOKENS = 60
JUDGE_TEMPERATURE = 0
# Sent on every request so the shared llama-swap server's own sampling defaults (another app may
# want a presence penalty) can never change the decoding the bake-off validated: greedy, unpenalised.
JUDGE_PENALTIES = {"presence_penalty": 0, "frequency_penalty": 0}

# Debounce before a scheduled judge run actually starts: gives a late-arriving `after` frame or
# a scene replacement (Contract 4) time to land in the same ingest pass before the request is
# built, without materially delaying the verdict.
JUDGE_DEBOUNCE_S = 8.0

# Rule thresholds (docs/40-vision-judge.md's rule table and "Bake-off" section). Measured on the
# 33-event production-prompt run: every correct "no cat" verdict carried confidence 0.0-0.1 and
# every real cat event was judged present at >= 0.7, so rule (a) fires only on a LOW confidence
# and could not have hidden a real meal in that set. Detection thresholds elsewhere in this
# integration are never loosened by this feature; these gate only the judge's own rules.
JUDGE_SUPPRESS_MAX_CONFIDENCE = 0.2  # rule (a): cat_present=false and a cat rated this unlikely
JUDGE_IDENTIFY_MIN_CONFIDENCE = 0.7  # rule (b): cat_present=true, enrolled name
# Rule (a) never hides an `eat` the feeder itself kept at least this many samples for. Every false
# meal on record kept at most 3 (IR bowl reflections: 0-3), while real meals keep 4-31; one blurry
# frame the judge cannot read must never erase a meal the device watched for that long.
JUDGE_PROTECTED_EAT_SAMPLES = 4

# Item 4's visit eligibility: a visit already at least this confidently `auto`-identified skips
# the judge entirely -- only an eat (always eligible) or a genuinely uncertain visit is worth
# the round trip. Not addressed by the bake-off (a LOCAL-recognizer-confidence gate, not a
# verdict threshold) -- still provisional.
VISIT_JUDGE_SKIP_CONFIDENCE = 0.85

ELIGIBLE_KINDS = ("eat", "visit")
ELIGIBLE_LOOKBACK_S = 48 * 3600
BACKFILL_LIMIT = 100

# Deferral schedule while one of Nitin's on-demand llama-swap models is loaded (see
# `VisionJudge._big_model_is_loaded`): 10, then 30, then 60 minutes. Exhausting all three leaves
# the event unjudged -- the judge is never urgent enough to bump a person mid-conversation.
BUSY_RETRY_DELAYS_S: tuple[float, ...] = (600.0, 1800.0, 3600.0)

# --- Pure request building -----------------------------------------------------------------


def roster_text(roster: Sequence[tuple[str, str]]) -> str:
    """One line per enrolled cat: `<Name>: <coat description>` -- Contract 2, item 1. A cat
    with no description yet (`ensure_descriptions` has not run, or produced nothing usable)
    still gets a line, just with an empty description, so the roster's cat *count* -- something
    the judge needs to know "none of these" against -- is never silently short."""
    return "\n".join(f"{name}: {description}" for name, description in roster)


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(data_url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_url}}


def build_user_content(
    *,
    roster: Sequence[tuple[str, str]],
    reference_images: Sequence[tuple[str, str]],
    scene_image: str | None,
    crop_images: Sequence[str],
    question: str = JUDGE_QUESTION,
) -> list[dict[str, Any]]:
    """The `kibble/chat/completions` user message's `content` array, in Contract 2's own order:
    roster text, then one `Reference: <Name>` + image pair per referenced cat, then the scene
    (or `after`-frame fallback -- resolved by the caller, this only knows it as "the first
    target image") labelled `Scene`, then each crop labelled `Close-up`, then the question."""
    content: list[dict[str, Any]] = [_text_part(roster_text(roster))]
    for name, data_url in reference_images:
        content.append(_text_part(f"Reference: {name}"))
        content.append(_image_part(data_url))
    if scene_image is not None:
        content.append(_text_part("Scene"))
        content.append(_image_part(scene_image))
    for data_url in crop_images:
        content.append(_text_part("Close-up"))
        content.append(_image_part(data_url))
    content.append(_text_part(question))
    return content


def verdict_json_schema(enrolled: Sequence[str]) -> dict[str, Any]:
    """The Contract 1 verdict's JSON Schema, with `cat`'s `enum` built from the CURRENT roster
    plus the two sentinel values. llama-server's `json_schema` response format is
    grammar-enforced (confirmed live during the 2026-09-25 bake-off), so this
    makes the model structurally unable to emit a name outside the roster, not merely
    instructed to avoid one. `parse_verdict` still validates independently regardless: never
    trust a schema alone against an endpoint that might not actually enforce it."""
    return {
        "type": "object",
        "properties": {
            "cat_present": {"type": "boolean"},
            "cat": {"type": "string", "enum": [*enrolled, _CAT_UNKNOWN, _CAT_NONE]},
            "confidence": {"type": "number"},
            "multiple_cats": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["cat_present", "cat", "confidence", "multiple_cats", "reason"],
        "additionalProperties": False,
    }


def _response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The exact structured-output request field this llama-server build honours -- confirmed
    live by VlmBakeoff against gemma4-e4b: OpenAI's `json_schema` response-format shape,
    `strict: true`. Centralized here alone: if a future llama-server build needs a different
    shape instead, this is the one place to change."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


def build_verdict_payload(
    *,
    model: str,
    roster: Sequence[tuple[str, str]],
    reference_images: Sequence[tuple[str, str]],
    scene_image: str | None,
    crop_images: Sequence[str],
) -> dict[str, Any]:
    """The full `POST {base_url}/chat/completions` body for one event's verdict -- Contract 1's
    request shape: temperature 0, `max_tokens<=256`, the roster/reference/scene/crop/question
    content Contract 2 orders, and grammar-enforced structured output against `enrolled`."""
    enrolled = [name for name, _description in roster]
    return {
        "model": model,
        "temperature": JUDGE_TEMPERATURE,
        **JUDGE_PENALTIES,
        "max_tokens": JUDGE_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_content(
                    roster=roster, reference_images=reference_images,
                    scene_image=scene_image, crop_images=crop_images,
                ),
            },
        ],
        "response_format": _response_format("kibble_vision_verdict", verdict_json_schema(enrolled)),
    }


def build_description_payload(*, model: str, images: Sequence[str]) -> dict[str, Any]:
    """The full request body for Contract 3's description fill: every available reference image
    (avatar, then up to one day and one IR training crop -- the caller's own selection order),
    followed by the question. Plain text response, no structured-output schema."""
    content: list[dict[str, Any]] = [_image_part(url) for url in images]
    content.append(_text_part(DESCRIPTION_QUESTION))
    return {
        "model": model,
        "temperature": JUDGE_TEMPERATURE,
        **JUDGE_PENALTIES,
        "max_tokens": DESCRIPTION_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    }


# --- Pure response parsing -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """One strictly-parsed Contract 1 verdict."""

    cat_present: bool
    cat: str  # "none" | "unknown" | an enrolled name -- verbatim, see `parse_verdict`
    confidence: float
    multiple_cats: bool
    reason: str


def _message_content(payload: Any) -> Any:
    return payload["choices"][0]["message"]["content"]


def parse_verdict(payload: Any, *, enrolled: Container[str]) -> Verdict | None:
    """Strictly parses and validates one chat-completions response body against Contract 1's
    schema. `None` for anything malformed: a response shape the endpoint did not honour, a
    missing or wrong-typed field, a `cat` that is neither "none"/"unknown" nor a name in
    `enrolled`, or a `confidence` outside 0..1 -- "malformed means no action"
    (docs/40-vision-judge.md)."""
    try:
        content = _message_content(payload)
        data = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cat_present = data.get("cat_present")
    cat = data.get("cat")
    confidence = data.get("confidence")
    multiple_cats = data.get("multiple_cats")
    reason = data.get("reason")
    if not isinstance(cat_present, bool) or not isinstance(multiple_cats, bool):
        return None
    if not isinstance(cat, str) or (cat not in (_CAT_NONE, _CAT_UNKNOWN) and cat not in enrolled):
        return None
    # `bool` is a `int` subclass in Python -- excluded explicitly so a stray `true`/`false`
    # response for `confidence` is rejected, not silently coerced to 1.0/0.0.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not (0.0 <= float(confidence) <= 1.0):
        return None
    if not isinstance(reason, str):
        return None
    return Verdict(
        cat_present=cat_present, cat=cat, confidence=float(confidence),
        multiple_cats=multiple_cats, reason=reason,
    )


def parse_description(payload: Any) -> str | None:
    """The model's plain-text coat description, stripped, or `None` for a malformed response or
    one with nothing usable."""
    try:
        text = _message_content(payload)
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


# --- Pure crop selection / evidence key ---------------------------------------------------------


def select_judge_crops(candidates: Sequence[ThumbCandidate], limit: int) -> list[ThumbCandidate]:
    """Up to `limit` crops with an actual, trustworthy body image, best `score` first (Contract
    2, item 3; ties keep sample order, newest first -- same tiebreak idiom as
    `store.pick_thumb`). Unlike `pick_thumb`, this never excludes a `not_a_cat`-guessed sample:
    that guess is exactly the local recognizer's own uncertainty this judge exists to
    double-check. A legacy sample whose crop `crop_geometry.is_legacy_crop_trustworthy` flags as
    unreliable IS excluded, though -- the judge must never see or decide from a crop that mostly
    shows unrelated room content; it falls back to whatever scene/`after` frame the caller
    already sends as the primary image instead."""
    ranked = [
        c for c in candidates
        if c.body is not None and crop_geometry.is_legacy_crop_trustworthy(c.box, c.t)
    ]
    ranked.sort(key=lambda c: (c.score if c.score is not None else -1.0, c.t), reverse=True)
    return ranked[:limit]


def evidence_key(scene_asset: str | None, crop_uids: Sequence[str]) -> str:
    """A stable string identifying exactly what evidence one verdict was based on -- the scene
    (or `after`-frame) asset id plus the chosen crop sample uids. Stored verbatim in
    `events.judge_evidence`; any change (a new best crop arrives, or `ingest.py`'s Contract-4
    scene replacement invalidates it) changes this string and makes the event eligible again."""
    return "|".join((scene_asset or "", *crop_uids))


# --- The background judge ----------------------------------------------------------------------


class VisionJudge:
    """Background second-opinion judge for one config entry -- see the module docstring for the
    two jobs it runs and the rule table it applies."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: KibbleStore) -> None:
        self._hass = hass
        self._entry = entry
        self._store = store
        self._base_url = (entry.options.get(CONF_VISION_JUDGE_URL) or "").strip().rstrip("/")
        self.model = entry.options.get(CONF_VISION_JUDGE_MODEL) or DEFAULT_VISION_JUDGE_MODEL
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(JUDGE_CONCURRENCY)
        self._consecutive_failures = 0
        self.last_error: str | None = None
        # Wired by `__init__.py` right after both objects exist -- same chicken-and-egg reason
        # as `Ingestor.coordinator`: applying a verdict needs to push the identity snapshot/
        # timeline-subscribe notification, but `KibbleCoordinator.__init__` itself takes the
        # `Ingestor` this judge is attached to.
        self.coordinator: KibbleCoordinator | None = None

    @property
    def enabled(self) -> bool:
        """Off by default: an empty `vision_judge_url` option means no requests, no
        description-filling and no verdict ever recorded -- same "empty means off" shape as
        `eating_clips.ClipLinker.enabled`."""
        return bool(self._base_url)

    # --- scheduling ----------------------------------------------------------------------------

    def schedule_judge(self, uid: str) -> None:
        """(Re)starts the debounced background arc for one event's `uid`. Unlike
        `ClipLinker.schedule_link` (which leaves an in-flight retry arc alone), a call here
        CANCELS any still-sleeping run and restarts the debounce delay -- so a late-arriving
        `after` frame or scene replacement is folded into the run that actually fires, instead
        of racing an already-in-flight one built from stale evidence."""
        if not self.enabled:
            return
        existing = self._pending.get(uid)
        if existing is not None and not existing.done():
            existing.cancel()
        self._pending[uid] = self._entry.async_create_background_task(
            self._hass, self._debounced_process(uid), name=f"kibble vision judge {uid}"
        )

    async def _debounced_process(self, uid: str) -> None:
        await asyncio.sleep(JUDGE_DEBOUNCE_S)
        try:
            await self._process_event(uid)
        except Exception:  # noqa: BLE001 -- one bad event must never crash the background task
            _LOGGER.exception("Vision judge failed for %s", uid)

    async def async_backfill_eligible(self) -> None:
        """Startup sweep (item 4's "Backfill" clause): schedules every still-open-to-judging
        closed event from the last 48h, newest first, capped at `BACKFILL_LIMIT` --
        `_process_event`'s own eligibility/evidence gate decides what actually gets sent, so
        this only needs to be a superset."""
        if not self.enabled:
            return
        cutoff = int(time.time()) - ELIGIBLE_LOOKBACK_S
        for uid in await self._store.async_events_needing_judge(cutoff, BACKFILL_LIMIT):
            self.schedule_judge(uid)

    async def async_cancel(self) -> None:
        """Cancels every still-pending debounce/run -- `entry.async_on_unload`'s own cleanup,
        so unloading the entry never leaves a background task pointed at a closed store."""
        pending = [t for t in self._pending.values() if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- per-event processing --------------------------------------------------------------

    async def _process_event(self, uid: str) -> None:
        ctx = await self._store.async_judge_event_context(uid)
        if ctx is None or ctx["reviewed"] or ctx["hidden"] or ctx["open"]:
            return
        if ctx["kind"] not in ELIGIBLE_KINDS:
            return
        candidates = await self._store.async_event_sample_candidates(uid)
        if ctx["kind"] == "visit":
            if pick_thumb(candidates) is None:  # would not show in the timeline at all
                return
            if (
                ctx["identity_status"] == "auto"
                and (ctx["confidence"] or 0.0) >= VISIT_JUDGE_SKIP_CONFIDENCE
            ):
                return
        scene_asset = ctx["scene"] or ctx["after"]
        crops = select_judge_crops(candidates, JUDGE_MAX_CROPS)
        if scene_asset is None and not crops:
            return  # "at least one image" gate -- nothing to actually send
        evidence = evidence_key(scene_asset, [c.uid for c in crops])
        if ctx["judge_evidence"] == evidence:
            return  # already judged against exactly this evidence

        cats = await self._store.async_cats()
        enrolled = [c["name"] for c in cats]
        descriptions = await self._store.async_cat_descriptions()
        roster = [(name, descriptions.get(name) or "") for name in enrolled]

        scene_data_url = await self._encode_asset(scene_asset)
        crop_data_urls = [
            url for c in crops if (url := await self._encode_asset(c.body)) is not None
        ]
        if scene_data_url is None and not crop_data_urls:
            return  # every fetch/decode failed -- nothing left to send

        references: list[tuple[str, str]] = []
        if JUDGE_INCLUDE_REFERENCE_IMAGES:
            for name in enrolled:
                avatar = await self._store.async_cat_avatar(name)
                if avatar is None:
                    continue
                data_url = await self._encode_asset(avatar["id"])
                if data_url is not None:
                    references.append((name, data_url))

        payload = build_verdict_payload(
            model=self.model, roster=roster, reference_images=references,
            scene_image=scene_data_url, crop_images=crop_data_urls,
        )
        response = await self._call_model_when_free(payload)
        if response is None:
            return
        verdict = parse_verdict(response, enrolled=set(enrolled))
        if verdict is None:
            _LOGGER.debug("Vision judge returned a malformed verdict for %s", uid)
            return
        await self._apply_verdict(uid, evidence=evidence, verdict=verdict)

    async def _apply_verdict(self, uid: str, *, evidence: str, verdict: Verdict) -> None:
        # Re-read: the identity state this decides against may have moved on (a fresh local
        # reclassification, or a human review) during the request's own round trip.
        ctx = await self._store.async_judge_event_context(uid)
        if ctx is None or ctx["reviewed"] or ctx["hidden"]:
            return
        apply_identity = False
        new_cat: str | None = None
        new_status: str | None = None
        new_confidence: float | None = None
        protected_meal = ctx["kind"] == "eat" and ctx["sample_count"] >= JUDGE_PROTECTED_EAT_SAMPLES
        if not verdict.cat_present and verdict.confidence <= JUDGE_SUPPRESS_MAX_CONFIDENCE and not protected_meal:
            # Rule (a).
            apply_identity = True
            new_cat, new_status, new_confidence = None, identity.NOT_A_CAT, None
        elif (
            verdict.cat_present
            and verdict.cat not in (_CAT_NONE, _CAT_UNKNOWN)
            and not verdict.multiple_cats
            and verdict.confidence >= JUDGE_IDENTIFY_MIN_CONFIDENCE
            and (
                ctx["identity_status"] in (None, "unknown")
                or (ctx["identity_status"] == "auto" and ctx["cat"] != verdict.cat)
            )
        ):
            # Rule (b).
            apply_identity = True
            new_cat, new_status, new_confidence = verdict.cat, "auto", verdict.confidence
        # Else rule (c): record the verdict only -- `apply_identity` stays False.
        changed = await self._store.async_apply_judge_verdict(
            uid,
            model=self.model,
            evidence=evidence,
            present=verdict.cat_present,
            cat=verdict.cat,
            confidence=verdict.confidence,
            multiple=verdict.multiple_cats,
            reason=verdict.reason,
            apply_identity=apply_identity,
            new_cat=new_cat,
            new_identity_status=new_status,
            new_confidence=new_confidence,
        )
        if changed and self.coordinator is not None:
            await self.coordinator.async_refresh_identity_snapshot()

    # --- Contract 3: coat descriptions ------------------------------------------------------

    async def ensure_descriptions(self) -> None:
        """Fills every enrolled cat's blank `cats.description`, from its avatar plus up to one
        day and one IR training crop (Contract 3). Run once at startup (`__init__.py`), before
        any event verdict needs the roster. A cat with nothing to describe from yet (no avatar,
        no training) is left blank -- it just gets an empty roster line until it has something
        to learn from."""
        if not self.enabled:
            return
        for name in await self._store.async_cats_needing_description():
            images: list[str] = []
            avatar = await self._store.async_cat_avatar(name)
            if avatar is not None:
                data_url = await self._encode_asset(avatar["id"])
                if data_url is not None:
                    images.append(data_url)
            for mode in (identity.MODE_DAY, identity.MODE_IR):
                crop = await self._store.async_training_crop_for_mode(name, mode)
                if crop is not None:
                    data_url = await self._encode_asset(crop["body"])
                    if data_url is not None:
                        images.append(data_url)
            if not images:
                continue
            response = await self._call_model_when_free(build_description_payload(model=self.model, images=images))
            if response is None:
                continue
            text = parse_description(response)
            if text:
                await self._store.async_set_cat_description(name, text)

    # --- model I/O -----------------------------------------------------------------------------

    async def _encode_asset(self, asset_id: str | None) -> str | None:
        """One stored asset (media, training, or avatar -- anything `store.asset_path`
        resolves), base64-encoded as a data URL at NATIVE resolution -- the bake-off measured
        no accuracy gain and slightly higher latency from downscaling the feeder's own
        already-modest (~1280x720) frames (bake-off, docs/40-vision-judge.md). `None` for a missing
        name or an asset that no longer exists on disk. Never raises: one bad image must never
        abort the whole verdict/description request."""
        if not asset_id:
            return None
        path = self._store.asset_path(asset_id)
        if path is None:
            return None
        try:
            raw = await self._hass.async_add_executor_job(path.read_bytes)
        except OSError:
            return None
        return f"data:image/jpeg;base64,{base64.b64encode(raw).decode()}"

    async def _call_model_when_free(self, payload: dict[str, Any]) -> Any | None:
        """Checks llama-swap's own `GET /running` before every request and defers
        (`BUSY_RETRY_DELAYS_S`: 10, 30, 60 minutes) rather than evict an on-demand model Nitin
        loaded by hand. Exhausting every deferral leaves the event unjudged: the same outcome as
        `_call_model` itself giving up, and logged the same quiet way. Never holds
        `_call_model`'s own concurrency semaphore while waiting -- a deferral is not network
        work."""
        for delay in (0.0, *BUSY_RETRY_DELAYS_S):
            if delay:
                await asyncio.sleep(delay)
            if not await self._big_model_is_loaded():
                return await self._call_model(payload)
        _LOGGER.debug("Vision judge deferred past every busy retry; leaving this event unjudged")
        return None

    async def _big_model_is_loaded(self) -> bool:
        """Whether asking for the judge model right now would evict an on-demand model.
        llama-swap's groups are exclusive: while a heavy model runs, the pinned resident set --
        the judge model included -- is unloaded, and a request for the judge model evicts the
        heavy one. So the judge model already being loaded means no swap at all; otherwise any
        loaded model with a non-zero ttl (the residents are pinned at ttl 0) is an on-demand one
        worth waiting for. Reading this from llama-swap itself, not a hard-coded roster, keeps it
        right when the resident set changes. `False` (safe to proceed) whenever `/running` is
        unreachable or unparseable -- indistinguishable from the judge server simply being down,
        and left to `_call_model`'s own ordinary retry/give-up path."""
        running = await self._running_models()
        if running is None or self.model in running:
            return False
        return any(ttl > 0 for ttl in running.values())

    async def _running_models(self) -> dict[str, float] | None:
        """`GET <server root>/running` -- llama-swap's currently-loaded models, as name -> ttl
        seconds (0 when absent). The configured URL is the OpenAI base (`http://host:9292/v1`),
        but llama-swap serves its management routes at the root (`/v1/running` is a 404), so a
        trailing `/v1` is dropped. llama-swap answers `{"running": [{"model": name, "state": ...,
        "ttl": seconds}, ...]}`; a bare list of those objects or of name strings is accepted too.
        Any other shape or a fetch failure is `None`, "treat llama-swap as unreachable, exactly
        as" an ordinary `_call_model` failure."""
        root = self._base_url.removesuffix("/v1")
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                f"{root}/running", timeout=JUDGE_REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError):
            return None
        if isinstance(data, dict):
            data = data.get("running")
        if not isinstance(data, list):
            return None
        models: dict[str, float] = {}
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("model"), str):
                ttl = item.get("ttl")
                valid = isinstance(ttl, (int, float)) and not isinstance(ttl, bool)
                models[item["model"]] = float(ttl) if valid else 0.0
            elif isinstance(item, str):
                models[item] = 0.0
        return models

    async def _call_model(self, payload: dict[str, Any]) -> Any | None:
        """POSTs one chat-completions request, concurrency-`JUDGE_CONCURRENCY`, `JUDGE_MAX_
        RETRIES` retries on top of the first attempt, each bounded by `JUDGE_REQUEST_TIMEOUT`.
        `None` on total failure -- logged once per new failure streak, silent for every repeat
        while the streak continues, so a judge server that is simply off does not spam the log
        once per event."""
        url = f"{self._base_url}/chat/completions"
        session = async_get_clientsession(self._hass)
        last_err: Exception | None = None
        async with self._semaphore:
            for _attempt in range(JUDGE_MAX_RETRIES + 1):
                try:
                    async with session.post(
                        url, json=payload, timeout=JUDGE_REQUEST_TIMEOUT
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
                except (ClientError, TimeoutError, ValueError) as err:
                    last_err = err
                    continue
                self._consecutive_failures = 0
                self.last_error = None
                return data
        self._consecutive_failures += 1
        self.last_error = str(last_err)
        if self._consecutive_failures == 1:
            _LOGGER.warning("Vision judge request failed, giving up quietly: %s", last_err)
        else:
            _LOGGER.debug(
                "Vision judge request failed (streak %d): %s", self._consecutive_failures, last_err
            )
        return None

