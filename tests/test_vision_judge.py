"""`judge.py`: the Contract 1/2 request-building and verdict-parsing pure functions, and
`VisionJudge`'s eligibility gates, rule (a)/(b)/(c) application, debounced scheduling, and
concurrency/timeout/retry -- see docs/40-vision-judge.md for the full contract this backs.

`VisionJudge`'s own tests never touch a real event loop timer or a real `aiohttp` session for
the eligibility/rule-application tests: `_call_model` is overridden directly (the one place a
verdict/description request would happen), the same pattern `test_eating_clips.py` uses for
`ClipLinker._fetch_candidates`. A dedicated, narrower set of tests exercises `_call_model` itself
against a fake `aiohttp` session (concurrency, retry, timeout, failure-streak logging) --
`entry.async_create_background_task` is faked with plain `asyncio.ensure_future`, same as
`test_eating_clips.py`'s own `_fake_entry`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kibble import identity
from kibble.const import CONF_VISION_JUDGE_MODEL, CONF_VISION_JUDGE_URL
from kibble.judge import (
    JUDGE_QUESTION,
    Verdict,
    VisionJudge,
    build_description_payload,
    build_user_content,
    build_verdict_payload,
    evidence_key,
    parse_description,
    parse_verdict,
    roster_text,
    select_judge_crops,
)
from kibble.store import ThumbCandidate

# --- roster_text / build_user_content (Contract 2) ----------------------------------------------


def test_roster_text_is_one_line_per_cat_name_colon_description() -> None:
    assert roster_text([("Kitty", "brown tabby"), ("Pancake", "solid black")]) == (
        "Kitty: brown tabby\nPancake: solid black"
    )


def test_roster_text_of_no_enrolled_cats_is_empty() -> None:
    assert roster_text([]) == ""


def test_roster_text_keeps_a_cat_with_no_description_yet_as_an_empty_line() -> None:
    assert roster_text([("NewCat", "")]) == "NewCat: "


def test_build_user_content_orders_roster_then_references_then_scene_then_crops_then_question() -> None:
    content = build_user_content(
        roster=[("Kitty", "brown tabby"), ("Pancake", "solid black")],
        reference_images=[("Kitty", "data:image/jpeg;base64,AAA")],
        scene_image="data:image/jpeg;base64,SCENE",
        crop_images=["data:image/jpeg;base64,C1", "data:image/jpeg;base64,C2"],
    )
    assert content == [
        {"type": "text", "text": "Kitty: brown tabby\nPancake: solid black"},
        {"type": "text", "text": "Reference: Kitty"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        {"type": "text", "text": "Scene"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,SCENE"}},
        {"type": "text", "text": "Close-up"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,C1"}},
        {"type": "text", "text": "Close-up"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,C2"}},
        {"type": "text", "text": JUDGE_QUESTION},
    ]


def test_build_user_content_with_nothing_but_roster_and_question() -> None:
    content = build_user_content(roster=[], reference_images=[], scene_image=None, crop_images=[])
    assert content == [{"type": "text", "text": ""}, {"type": "text", "text": JUDGE_QUESTION}]


def test_build_user_content_omits_the_scene_part_entirely_when_there_is_no_scene_image() -> None:
    content = build_user_content(
        roster=[("Kitty", "x")], reference_images=[], scene_image=None,
        crop_images=["data:image/jpeg;base64,C1"],
    )
    kinds_and_texts = [(c["type"], c.get("text")) for c in content]
    assert ("text", "Scene") not in kinds_and_texts


# --- build_verdict_payload / structured output ---------------------------------------------------


def test_build_verdict_payload_uses_temperature_zero_and_the_confirmed_json_schema_response_format() -> None:
    payload = build_verdict_payload(
        model="qwen3-vl-4b", roster=[("Kitty", "brown tabby")], reference_images=[],
        scene_image="data:image/jpeg;base64,S", crop_images=[],
    )
    assert payload["model"] == "qwen3-vl-4b"
    assert payload["temperature"] == 0
    assert payload["presence_penalty"] == 0 and payload["frequency_penalty"] == 0, "server defaults must never shift the judge"
    assert payload["max_tokens"] <= 256
    assert payload["response_format"]["type"] == "json_schema"
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["cat_present", "cat", "confidence", "multiple_cats", "reason"]
    assert schema["schema"]["additionalProperties"] is False


def test_build_verdict_payload_verdict_schema_enum_is_built_from_the_live_roster() -> None:
    """VlmBakeoff's own upgrade: grammar-enforce `cat` to the current roster + sentinels, so the
    model is structurally unable to name a cat that isn't actually enrolled."""
    payload = build_verdict_payload(
        model="m", roster=[("Kitty", ""), ("Pancake", "")], reference_images=[],
        scene_image=None, crop_images=[],
    )
    cat_enum = payload["response_format"]["json_schema"]["schema"]["properties"]["cat"]["enum"]
    assert cat_enum == ["Kitty", "Pancake", "unknown", "none"]


def test_build_description_payload_has_no_structured_output_and_orders_images_then_question() -> None:
    payload = build_description_payload(model="m", images=["data:image/jpeg;base64,A", "data:image/jpeg;base64,B"])
    assert "response_format" not in payload
    content = payload["messages"][1]["content"]
    assert content[0]["type"] == "image_url" and content[1]["type"] == "image_url"
    assert content[2] == {"type": "text", "text": content[2]["text"]}
    assert content[-1]["type"] == "text"


# --- parse_verdict: valid, malformed, schema-violation shapes -----------------------------------


def _response(content: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


def _verdict_json(**overrides: Any) -> str:
    base = {"cat_present": True, "cat": "Kitty", "confidence": 0.9, "multiple_cats": False, "reason": "because"}
    base.update(overrides)
    return json.dumps(base)


def test_parse_verdict_accepts_a_valid_json_string_content() -> None:
    v = parse_verdict(_response(_verdict_json()), enrolled={"Kitty", "Pancake"})
    assert v == Verdict(cat_present=True, cat="Kitty", confidence=0.9, multiple_cats=False, reason="because")


def test_parse_verdict_accepts_an_already_decoded_dict_content() -> None:
    v = parse_verdict(
        _response({"cat_present": False, "cat": "none", "confidence": 0.8, "multiple_cats": False, "reason": "empty"}),
        enrolled={"Kitty"},
    )
    assert v is not None and v.cat_present is False and v.cat == "none"


def test_parse_verdict_accepts_none_and_unknown_even_with_an_empty_roster() -> None:
    assert parse_verdict(_response(_verdict_json(cat="none", cat_present=False)), enrolled=set()) is not None
    assert parse_verdict(_response(_verdict_json(cat="unknown")), enrolled=set()) is not None


@pytest.mark.parametrize(
    "content",
    [
        '{"cat_present": true, "cat": "Kitty", "confidence": 0.9, "multiple_cats": false}',  # missing "reason"
        None,
    ],
)
def test_parse_verdict_rejects_a_missing_field(content: Any) -> None:
    assert parse_verdict(_response(content), enrolled={"Kitty"}) is None


def test_parse_verdict_rejects_a_cat_name_outside_the_roster_and_not_a_sentinel() -> None:
    assert parse_verdict(_response(_verdict_json(cat="Whiskers")), enrolled={"Kitty"}) is None


@pytest.mark.parametrize("confidence", [1.5, -0.1, True, "0.9"])
def test_parse_verdict_rejects_an_out_of_range_or_wrong_typed_confidence(confidence: Any) -> None:
    assert parse_verdict(_response(_verdict_json(confidence=confidence)), enrolled={"Kitty"}) is None


def test_parse_verdict_rejects_a_wrong_typed_cat_present() -> None:
    assert parse_verdict(_response(_verdict_json(cat_present="yes")), enrolled={"Kitty"}) is None


def test_parse_verdict_rejects_invalid_json_text() -> None:
    assert parse_verdict(_response("not json{{"), enrolled={"Kitty"}) is None


@pytest.mark.parametrize("payload", [{"error": "boom"}, {"choices": []}, None, "garbage", {}])
def test_parse_verdict_rejects_a_response_whose_own_shape_is_broken(payload: Any) -> None:
    """A timeout/malformed-endpoint response never reaches `parse_verdict` in production
    (`_call_model` already returns `None` for those) -- this covers the case an endpoint answers
    200 with a body that doesn't even have the expected `choices[0].message.content` shape."""
    assert parse_verdict(payload, enrolled={"Kitty"}) is None


# --- parse_description ---------------------------------------------------------------------------


def test_parse_description_strips_a_valid_response() -> None:
    assert parse_description(_response("  Brown tabby with bold stripes.  ")) == "Brown tabby with bold stripes."


@pytest.mark.parametrize("payload", [_response("   "), _response(123), {"choices": []}, None])
def test_parse_description_rejects_blank_or_malformed_responses(payload: Any) -> None:
    assert parse_description(payload) is None


# --- select_judge_crops / evidence_key -----------------------------------------------------------


def _candidate(
    uid: str, t: int, score: float | None, *, body: str | None = "b.jpg", guess: str | None = None,
    box: tuple[float, float, float, float] | None = None,
) -> ThumbCandidate:
    return ThumbCandidate(uid=uid, t=t, body=body, has_face=False, score=score, box=box, guess=guess)


def test_select_judge_crops_ranks_by_score_best_first() -> None:
    low, high = _candidate("a", 1, 0.3), _candidate("b", 2, 0.9)
    assert [c.uid for c in select_judge_crops([low, high], 3)] == ["b", "a"]


def test_select_judge_crops_excludes_candidates_with_no_body_crop() -> None:
    no_body = _candidate("a", 1, 0.99, body=None)
    with_body = _candidate("b", 2, 0.1)
    assert [c.uid for c in select_judge_crops([no_body, with_body], 3)] == ["b"]


def test_select_judge_crops_never_excludes_a_not_a_cat_guessed_sample() -> None:
    """That guess is exactly the local recognizer's own uncertainty this judge exists to
    double-check -- unlike `store.pick_thumb`, it must still be selectable."""
    flagged = _candidate("a", 1, 0.99, guess=identity.NOT_A_CAT)
    assert select_judge_crops([flagged], 3) == [flagged]


def test_select_judge_crops_excludes_an_untrustworthy_legacy_crop() -> None:
    """The judge must never see or decide from a crop the 2026-09-25 shift bug corrupted
    (docs/40-vision-judge.md's crop-geometry note) -- even when it would otherwise rank first
    by score."""
    bad = _candidate("bad", 1, 0.99, box=(0.478, 0.208, 0.593, 0.622))  # e1492-s1, overlap 0.0
    good = _candidate("good", 2, 0.1, box=(0.0, 0.4, 0.0625, 0.5))  # left-edge, overlap 1.0
    assert [c.uid for c in select_judge_crops([bad, good], 3)] == ["good"]


def test_select_judge_crops_respects_the_limit() -> None:
    cands = [_candidate(str(i), i, float(i)) for i in range(5)]
    assert [c.uid for c in select_judge_crops(cands, 2)] == ["4", "3"]


def test_evidence_key_joins_scene_and_crop_uids() -> None:
    assert evidence_key("2026-09-25/e1-scene.jpg", ["e1-s1", "e1-s2"]) == "2026-09-25/e1-scene.jpg|e1-s1|e1-s2"


def test_evidence_key_of_nothing_is_empty() -> None:
    assert evidence_key(None, []) == ""


# --- VisionJudge fixtures --------------------------------------------------------------------


def _verdict_response(cat_present: bool, cat: str, confidence: float, *, multiple: bool = False, reason: str = "r") -> dict[str, Any]:
    return _response(_verdict_json(cat_present=cat_present, cat=cat, confidence=confidence, multiple_cats=multiple, reason=reason))


class _FakeHass:
    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        return fn(*args)


def _fake_entry(url: str = "http://judge.local:9292/v1", model: str = "qwen3-vl-4b") -> SimpleNamespace:
    def _spawn(hass: Any, coro: Any, name: str | None = None) -> asyncio.Task[None]:
        return asyncio.ensure_future(coro)

    return SimpleNamespace(
        options={CONF_VISION_JUDGE_URL: url, CONF_VISION_JUDGE_MODEL: model},
        async_create_background_task=_spawn,
    )


def _make_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        kind="eat", open=False, reviewed=False, hidden=False,
        scene="2026-09-25/e1-scene.jpg", before=None, after=None,
        cat=None, identity_status=None, confidence=None, judge_evidence=None, sample_count=0,
    )
    base.update(overrides)
    return base


class _FakeStore:
    """Just enough of `KibbleStore`'s async surface for `VisionJudge`, mirroring
    `test_eating_clips.py`'s own `_FakeStore` shape."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, list[ThumbCandidate]] = {}
        self.cats_list: list[str] = []
        self.descriptions: dict[str, str] = {}
        self.avatars: dict[str, dict[str, str]] = {}
        self.training_crops: dict[tuple[str, str], dict[str, Any]] = {}
        self.applied_calls: list[tuple[str, dict[str, Any]]] = []
        self.backfill_uids: list[str] = []

    async def async_judge_event_context(self, uid: str) -> dict[str, Any] | None:
        ev = self.events.get(uid)
        return dict(ev) if ev is not None else None

    async def async_event_sample_candidates(self, uid: str) -> list[ThumbCandidate]:
        return self.candidates.get(uid, [])

    async def async_cats(self) -> list[dict[str, str]]:
        return [{"name": n} for n in self.cats_list]

    async def async_cat_descriptions(self) -> dict[str, str]:
        return self.descriptions

    async def async_cat_avatar(self, name: str) -> dict[str, str] | None:
        return self.avatars.get(name)

    async def async_training_crop_for_mode(self, cat: str, mode: str) -> dict[str, Any] | None:
        return self.training_crops.get((cat, mode))

    async def async_cats_needing_description(self) -> list[str]:
        return [n for n in self.cats_list if not self.descriptions.get(n)]

    async def async_set_cat_description(self, cat: str, description: str) -> None:
        self.descriptions[cat] = description

    def asset_path(self, asset_id: str | None) -> SimpleNamespace | None:
        if asset_id is None:
            return None
        return SimpleNamespace(read_bytes=lambda: b"not-actually-decoded-in-these-tests")

    async def async_apply_judge_verdict(self, uid: str, **kwargs: Any) -> bool:
        self.applied_calls.append((uid, dict(kwargs)))
        ev = self.events.get(uid)
        if ev is None or ev["reviewed"]:
            return False
        ev["judge_present"] = kwargs["present"]
        ev["judge_cat"] = kwargs["cat"]
        ev["judge_evidence"] = kwargs["evidence"]
        if kwargs["apply_identity"]:
            ev["cat"] = kwargs["new_cat"]
            ev["identity_status"] = kwargs["new_identity_status"]
            ev["confidence"] = kwargs["new_confidence"]
        return True

    async def async_events_needing_judge(self, cutoff: int, limit: int) -> list[str]:
        return list(self.backfill_uids)


def _judge(store: _FakeStore, *, url: str = "http://judge.local:9292/v1") -> VisionJudge:
    return VisionJudge(_FakeHass(), _fake_entry(url), store)


def _stub_call_model(vj: VisionJudge, responses: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Overrides `vj._call_model` (the one place a request would happen) with a stub that
    returns `responses` in sequence (a list) or never needs a queue at all (a single dict reused
    every call). Records every payload it was asked to send. Also reports llama-swap as free, so
    `_call_model_when_free`'s `/running` check never needs a real session -- the deferral has its
    own tests below."""
    sent: list[dict[str, Any]] = []
    queue = list(responses) if isinstance(responses, list) else None

    async def _fake(payload: dict[str, Any]) -> Any:
        sent.append(payload)
        return queue.pop(0) if queue is not None else responses

    async def _free() -> bool:
        return False

    vj._call_model = _fake  # type: ignore[method-assign]
    vj._big_model_is_loaded = _free  # type: ignore[method-assign]
    return sent


# --- enabled / schedule_judge -----------------------------------------------------------------


def test_enabled_is_false_with_an_empty_url() -> None:
    assert _judge(_FakeStore(), url="").enabled is False


def test_enabled_is_true_with_a_url_configured() -> None:
    assert _judge(_FakeStore()).enabled is True


def test_schedule_judge_is_a_no_op_while_disabled() -> None:
    vj = _judge(_FakeStore(), url="")
    vj.schedule_judge("e1")
    assert vj._pending == {}


async def test_schedule_judge_cancels_a_still_debouncing_prior_run_for_the_same_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kibble.judge.JUDGE_DEBOUNCE_S", 0.2)
    store = _FakeStore()
    store.events["e1"] = _make_event(scene=None, after=None)  # no image -- a fast no-op if it ever runs
    vj = _judge(store)
    vj.schedule_judge("e1")
    first = vj._pending["e1"]
    await asyncio.sleep(0.05)
    assert not first.done()
    vj.schedule_judge("e1")  # restart the debounce
    await asyncio.sleep(0.05)
    assert first.cancelled(), "the superseded debounce task must be cancelled"
    second = vj._pending["e1"]
    assert second is not first
    await asyncio.sleep(0.3)
    assert second.done() and not second.cancelled()


async def test_async_cancel_cancels_every_still_pending_task() -> None:
    store = _FakeStore()
    store.events["e1"] = _make_event()
    vj = _judge(store)
    vj.schedule_judge("e1")
    task = vj._pending["e1"]
    await asyncio.sleep(0)
    assert not task.done()
    await vj.async_cancel()
    assert task.cancelled()


# --- async_backfill_eligible ---------------------------------------------------------------------


async def test_async_backfill_eligible_schedules_every_candidate_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kibble.judge.JUDGE_DEBOUNCE_S", 1000.0)
    store = _FakeStore()
    store.backfill_uids = ["e3", "e2", "e1"]
    vj = _judge(store)
    await vj.async_backfill_eligible()
    assert set(vj._pending) == {"e1", "e2", "e3"}
    await vj.async_cancel()


async def test_async_backfill_eligible_is_a_no_op_while_disabled() -> None:
    store = _FakeStore()
    store.backfill_uids = ["e1"]
    vj = _judge(store, url="")
    await vj.async_backfill_eligible()
    assert vj._pending == {}


# --- _process_event: eligibility gates ---------------------------------------------------------


async def test_a_visit_already_confidently_auto_identified_is_never_sent_to_the_judge() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["v1"] = _make_event(kind="visit", identity_status="auto", confidence=0.9)
    store.candidates["v1"] = [_candidate("v1-s1", 1, 0.5)]
    vj = _judge(store)
    sent = _stub_call_model(vj, {})
    await vj._process_event("v1")
    assert sent == []


async def test_a_visit_just_below_the_confident_auto_threshold_is_judged() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["v2"] = _make_event(kind="visit", identity_status="auto", confidence=0.84)
    store.candidates["v2"] = [_candidate("v2-s1", 1, 0.5)]
    vj = _judge(store)
    sent = _stub_call_model(vj, _verdict_response(True, "Kitty", 0.5))
    await vj._process_event("v2")
    assert len(sent) == 1


async def test_a_visit_with_no_usable_thumb_is_never_sent_to_the_judge() -> None:
    store = _FakeStore()
    store.events["v3"] = _make_event(kind="visit", scene=None)
    store.candidates["v3"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, {})
    await vj._process_event("v3")
    assert sent == []


async def test_an_eat_is_always_eligible_regardless_of_current_confidence() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", identity_status="auto", confidence=0.99)
    store.candidates["e1"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, _verdict_response(True, "Kitty", 0.5))
    await vj._process_event("e1")
    assert len(sent) == 1


@pytest.mark.parametrize("field", ["reviewed", "hidden", "open"])
async def test_an_event_flagged_reviewed_hidden_or_open_is_never_sent_to_the_judge(field: str) -> None:
    store = _FakeStore()
    store.events["e1"] = _make_event(kind="eat", **{field: True})
    vj = _judge(store)
    sent = _stub_call_model(vj, {})
    await vj._process_event("e1")
    assert sent == []


async def test_an_event_kind_outside_eat_or_visit_is_never_sent_to_the_judge() -> None:
    store = _FakeStore()
    store.events["e1"] = _make_event(kind="import")
    vj = _judge(store)
    sent = _stub_call_model(vj, {})
    await vj._process_event("e1")
    assert sent == []


async def test_an_event_with_no_scene_after_or_crops_is_never_sent_to_the_judge() -> None:
    store = _FakeStore()
    store.events["e1"] = _make_event(kind="eat", scene=None, after=None)
    store.candidates["e1"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, {})
    await vj._process_event("e1")
    assert sent == []


async def test_an_event_already_judged_against_its_current_evidence_is_skipped() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", scene="d/e1-scene.jpg", judge_evidence=evidence_key("d/e1-scene.jpg", []))
    store.candidates["e1"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, _verdict_response(True, "Kitty", 0.9))
    await vj._process_event("e1")
    assert sent == [], "unchanged evidence must never trigger a second request"


async def test_a_changed_scene_asset_id_makes_a_previously_judged_event_eligible_again() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", scene="d/e1-scene-2.jpg", judge_evidence="d/e1-scene-1.jpg|")
    store.candidates["e1"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, _verdict_response(True, "Kitty", 0.9))
    await vj._process_event("e1")
    assert len(sent) == 1


async def test_the_after_frame_is_used_when_there_is_no_scene() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", scene=None, after="d/e1-after.jpg")
    store.candidates["e1"] = []
    vj = _judge(store)
    sent = _stub_call_model(vj, _verdict_response(False, "none", 0.9))
    await vj._process_event("e1")
    assert len(sent) == 1
    row = store.events["e1"]
    assert row["judge_evidence"] == "d/e1-after.jpg"


async def test_a_malformed_verdict_records_nothing() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat")
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _response("not valid json"))
    await vj._process_event("e1")
    assert store.applied_calls == []


# --- application rules (a) / (b) / (c) ----------------------------------------------------------


async def test_rule_a_suppresses_a_no_cat_verdict_that_rates_a_cat_at_the_bar() -> None:
    """Qwen3-VL-4B's confidence reads as its belief that a cat is present, so a confident "no
    cat" is a LOW number; exactly JUDGE_SUPPRESS_MAX_CONFIDENCE still counts."""
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.7)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(False, "none", 0.2, reason="empty bowl, IR reflection"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] is None and ev["identity_status"] == identity.NOT_A_CAT
    uid, kwargs = store.applied_calls[-1]
    assert kwargs["apply_identity"] is True
    assert kwargs["new_cat"] is None and kwargs["new_identity_status"] == identity.NOT_A_CAT and kwargs["new_confidence"] is None


async def test_rule_a_ignores_a_no_cat_verdict_that_still_rates_a_cat_likely() -> None:
    """A "no cat" answer paired with a middling or high belief in a cat is self-contradictory:
    record it, change nothing."""
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.7)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(False, "none", 0.5, reason="hard to tell"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] == "Kitty" and ev["identity_status"] == "auto", "a contradictory no-cat verdict must not suppress"
    assert store.applied_calls[-1][1]["apply_identity"] is False


async def test_rule_a_never_hides_a_meal_the_feeder_watched_for_several_samples() -> None:
    """A blurry frame the judge cannot read must not erase a meal the device itself kept 4+
    samples for; at 3 samples (the most any false meal on record kept) it still may."""
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["watched"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.7, sample_count=4)
    store.events["brief"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.7, sample_count=3)
    store.candidates["watched"] = []
    store.candidates["brief"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(False, "none", 0.1, reason="no cat visible"))
    await vj._process_event("watched")
    await vj._process_event("brief")
    assert store.events["watched"]["identity_status"] == "auto", "a well-watched meal is never hidden"
    assert store.events["brief"]["identity_status"] == identity.NOT_A_CAT


async def test_rule_b_fixes_an_unset_identity_to_the_verdicts_enrolled_name() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty", "Pancake"]
    store.events["e1"] = _make_event(kind="visit", identity_status=None, confidence=None)
    store.candidates["e1"] = [_candidate("e1-s1", 1, 0.9)]
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.80, reason="clear tabby"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] == "Kitty" and ev["identity_status"] == "auto" and ev["confidence"] == 0.80


async def test_rule_b_corrects_an_auto_identity_naming_a_different_cat() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty", "Pancake"]
    store.events["e1"] = _make_event(kind="eat", cat="Pancake", identity_status="auto", confidence=0.6)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.85, reason="tabby not black"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] == "Kitty" and ev["confidence"] == 0.85


async def test_rule_b_does_not_fire_when_auto_already_names_the_same_cat() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.6)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.9, reason="same cat"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["confidence"] == 0.6, "an already-correct auto identity must be left alone; only the verdict is recorded"
    assert store.applied_calls[-1][1]["apply_identity"] is False


async def test_rule_b_does_not_fire_below_its_confidence_threshold() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", identity_status=None, confidence=None)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.69, reason="not quite sure"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] is None and ev["identity_status"] is None


async def test_rule_b_never_overrides_an_existing_not_a_cat_verdict() -> None:
    """Literal per the rule table: only `None`/`unknown`, or `auto` naming a different cat --
    `not_a_cat` is deliberately not in that condition set."""
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", cat=None, identity_status=identity.NOT_A_CAT, confidence=None)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.95, reason="actually a cat now"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] is None and ev["identity_status"] == identity.NOT_A_CAT
    assert store.applied_calls[-1][1]["apply_identity"] is False


async def test_rule_c_records_a_low_confidence_verdict_without_changing_identity() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="eat", cat="Kitty", identity_status="auto", confidence=0.7)
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(False, "none", 0.5, reason="hard to tell"))
    await vj._process_event("e1")
    ev = store.events["e1"]
    assert ev["cat"] == "Kitty" and ev["identity_status"] == "auto" and ev["confidence"] == 0.7
    uid, kwargs = store.applied_calls[-1]
    assert kwargs["apply_identity"] is False and kwargs["present"] is False and kwargs["cat"] == "none"


async def test_a_reviewed_event_is_never_touched_even_by_an_otherwise_qualifying_verdict() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(
        kind="eat", reviewed=True, cat="Kitty", identity_status="auto", confidence=0.5,
    )
    store.candidates["e1"] = []
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(False, "none", 0.99, reason="not a cat"))
    applied_before = list(store.applied_calls)
    await vj._process_event("e1")
    assert store.events["e1"]["cat"] == "Kitty", "a reviewed event's identity fields must never change"
    assert store.applied_calls == applied_before, "no verdict is even recorded against a reviewed event"


async def test_no_verdict_ever_creates_a_training_row() -> None:
    """The judge has no training-row-creating call at all in its own surface -- `_FakeStore`
    exposes none, so any attempt would raise `AttributeError` rather than silently succeed."""
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.events["e1"] = _make_event(kind="visit", identity_status=None, confidence=None)
    store.candidates["e1"] = [_candidate("e1-s1", 1, 0.9)]
    vj = _judge(store)
    _stub_call_model(vj, _verdict_response(True, "Kitty", 0.95, reason="clear"))
    await vj._process_event("e1")  # must not raise despite no add_auto_training/add_upload_training on _FakeStore
    assert store.events["e1"]["cat"] == "Kitty"


# --- ensure_descriptions (Contract 3) -----------------------------------------------------------


async def test_ensure_descriptions_fills_only_cats_with_a_blank_description() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty", "Pancake", "NoPhotos"]
    store.descriptions = {"Pancake": "already described"}
    store.avatars = {"Kitty": {"id": "avatars/kitty.jpg", "url": "/x"}}
    store.training_crops = {("Kitty", "day"): {"uid": "t1", "body": "training/kitty/t1-body.jpg"}}
    vj = _judge(store)
    sent = _stub_call_model(vj, _response("Brown tabby with bold stripes."))
    await vj.ensure_descriptions()
    assert store.descriptions["Kitty"] == "Brown tabby with bold stripes."
    assert store.descriptions["Pancake"] == "already described"
    assert "NoPhotos" not in store.descriptions, "a cat with no avatar and no training crop is left blank"
    assert len(sent) == 1


async def test_ensure_descriptions_sends_avatar_then_day_crop_then_ir_crop() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    store.avatars = {"Kitty": {"id": "avatars/kitty.jpg", "url": "/x"}}
    store.training_crops = {
        ("Kitty", "day"): {"uid": "t1", "body": "training/kitty/t1-body.jpg"},
        ("Kitty", "ir"): {"uid": "t2", "body": "training/kitty/t2-body.jpg"},
    }
    vj = _judge(store)
    sent = _stub_call_model(vj, _response("desc"))
    await vj.ensure_descriptions()
    assert len(sent) == 1
    image_urls = [p["image_url"]["url"] for p in sent[0]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(image_urls) == 3  # avatar + day crop + ir crop


async def test_ensure_descriptions_is_a_no_op_while_disabled() -> None:
    store = _FakeStore()
    store.cats_list = ["Kitty"]
    vj = _judge(store, url="")
    calls = {"n": 0}

    async def _boom() -> list[str]:
        calls["n"] += 1
        return []

    store.async_cats_needing_description = _boom  # type: ignore[method-assign]
    await vj.ensure_descriptions()
    assert calls["n"] == 0


# --- _call_model: concurrency / retry / timeout / failure-streak logging (real network seam) ----


class _FakeResponse:
    def __init__(self, json_data: Any = None, raise_exc: Exception | None = None) -> None:
        self._json_data = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    async def json(self, content_type: Any = None) -> Any:
        return self._json_data

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    def __init__(self, responses: list[Any], running: Any = None) -> None:
        self._responses = list(responses)
        self._running = running  # what every GET /running answers: a _FakeResponse or an Exception
        self.calls: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def post(self, url: str, json: Any = None, timeout: Any = None) -> _FakeResponse:
        self.calls.append((url, json))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, timeout: Any = None) -> _FakeResponse:
        self.gets.append(url)
        if isinstance(self._running, Exception):
            raise self._running
        return self._running


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr("kibble.judge.async_get_clientsession", lambda hass: session)


async def test_call_model_posts_to_chat_completions_under_the_configured_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    vj = _judge(store, url="http://judge.local:9292/v1")
    session = _FakeSession([_FakeResponse(json_data={"ok": True})])
    _patch_session(monkeypatch, session)
    result = await vj._call_model({"model": "qwen3-vl-4b"})
    assert result == {"ok": True}
    assert session.calls[0][0] == "http://judge.local:9292/v1/chat/completions"


async def test_call_model_retries_once_after_a_transient_failure_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    store = _FakeStore()
    vj = _judge(store)
    session = _FakeSession([ClientError("connection reset"), _FakeResponse(json_data={"ok": True})])
    _patch_session(monkeypatch, session)
    result = await vj._call_model({"model": "m"})
    assert result == {"ok": True}
    assert len(session.calls) == 2
    assert vj._consecutive_failures == 0 and vj.last_error is None


async def test_call_model_gives_up_quietly_after_exhausting_its_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    store = _FakeStore()
    vj = _judge(store)
    session = _FakeSession([ClientError("down"), ClientError("still down")])
    _patch_session(monkeypatch, session)
    result = await vj._call_model({"model": "m"})
    assert result is None
    assert len(session.calls) == 2
    assert vj._consecutive_failures == 1
    assert "still down" in (vj.last_error or "")


async def test_call_model_a_timeout_counts_as_a_failure_like_any_other(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    vj = _judge(store)
    session = _FakeSession([TimeoutError("slow"), TimeoutError("slow again")])
    _patch_session(monkeypatch, session)
    result = await vj._call_model({"model": "m"})
    assert result is None


async def test_call_model_failure_streak_increments_across_calls_and_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    store = _FakeStore()
    vj = _judge(store)
    _patch_session(monkeypatch, _FakeSession([ClientError("a"), ClientError("b")]))
    await vj._call_model({"model": "m"})
    assert vj._consecutive_failures == 1
    _patch_session(monkeypatch, _FakeSession([ClientError("c"), ClientError("d")]))
    await vj._call_model({"model": "m"})
    assert vj._consecutive_failures == 2
    _patch_session(monkeypatch, _FakeSession([_FakeResponse(json_data={"ok": True})]))
    result = await vj._call_model({"model": "m"})
    assert result == {"ok": True}
    assert vj._consecutive_failures == 0 and vj.last_error is None


async def test_call_model_a_non_2xx_status_counts_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    store = _FakeStore()
    vj = _judge(store)
    session = _FakeSession([
        _FakeResponse(raise_exc=ClientError("500")),
        _FakeResponse(raise_exc=ClientError("500")),
    ])
    _patch_session(monkeypatch, session)
    result = await vj._call_model({"model": "m"})
    assert result is None


# --- busy-model deferral (llama-swap GET /running) -------------------------------------------------


def _running(*models: tuple[str, float]) -> _FakeResponse:
    """llama-swap's own `GET /running` answer shape."""
    return _FakeResponse(json_data={"running": [{"model": m, "state": "ready", "ttl": ttl} for m, ttl in models]})


async def test_judge_waits_out_a_loaded_heavy_model_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heavy on-demand model evicts the residents, the judge model included, and asking for
    the judge model would evict it mid-conversation: wait through the schedule, then leave the
    event unjudged."""
    monkeypatch.setattr("kibble.judge.BUSY_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    vj = _judge(_FakeStore(), url="http://judge.local:9292/v1")
    session = _FakeSession([], running=_running(("gemma4-26b", 600)))
    _patch_session(monkeypatch, session)
    assert await vj._call_model_when_free({"model": "qwen3-vl-4b"}) is None
    assert session.calls == [], "never asks for the judge model while the heavy model is loaded"
    assert session.gets == ["http://judge.local:9292/running"] * 4, "llama-swap's root, once per scheduled check"


async def test_judge_proceeds_at_once_while_its_own_model_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    vj = _judge(_FakeStore())
    session = _FakeSession(
        [_FakeResponse(json_data={"ok": True})],
        running=_running(("qwen3-vl-4b", 0), ("whisper-large-v3-turbo", 0)),
    )
    _patch_session(monkeypatch, session)
    assert await vj._call_model_when_free({"model": "qwen3-vl-4b"}) == {"ok": True}
    assert len(session.gets) == 1 and len(session.calls) == 1


async def test_judge_treats_an_unreachable_running_check_as_the_ordinary_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    vj = _judge(_FakeStore())
    session = _FakeSession([_FakeResponse(json_data={"ok": True})], running=ClientError("refused"))
    _patch_session(monkeypatch, session)
    assert await vj._call_model_when_free({"model": "qwen3-vl-4b"}) == {"ok": True}
    assert len(session.calls) == 1, "an unreachable /running must not invent a deferral"
