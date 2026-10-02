"""Dual-hopper feed: `amount2`'s schema default, `KibbleClient.feed`'s payload, and
`KibbleFeedButton`'s refusal to press a per-hopper button at 0 portions
(`kibble-card/DESIGN` batch: the hero's dual-mode controls now write `feed_amount_hopper_1`/
`_2` directly, and 0 is a real, legitimate value meaning "nothing from this hopper" -- see
`number.py`'s `KibbleFeedAmount.native_min_value` and `button.py`'s own module docstring).

The feed-call *mapping* itself (0/n, n/0, a/b, 0/0 -> which hopper(s), which amount(s)) is
front-end-side logic (`kibble-card/src/lib/dual-feed.ts`, `dual-feed.test.ts`) -- this file
covers only the integration side: the schema stays backward compatible, the HTTP payload only
carries amount2 when given, and a per-hopper button never turns a configured 0 into either a
silent no-op or a real 0-portion dispense.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from kibble.api import KibbleClient
from kibble.button import FEEDS, KibbleFeedButton
from kibble.const import ATTR_AMOUNT, ATTR_AMOUNT2, ATTR_HOPPER, HOPPER_BOTH, HOPPERS, MAX_AMOUNT

# A trimmed copy of __init__.py's own FEED_SCHEMA -- that module chains into coordinator.py,
# which needs a full Home Assistant runtime to import (tests/conftest.py's own docstring), so
# this suite can't import the real schema object directly. Kept in lockstep with it: any drift
# here would only miss a real schema bug, never invent a false one, since both are the same
# three-line vol.All(vol.Coerce(int), vol.Range(...)) shape __init__.py uses for `amount`.
FEED_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Optional(ATTR_HOPPER, default=HOPPER_BOTH): vol.In(HOPPERS),
        vol.Required(ATTR_AMOUNT): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_AMOUNT)),
        vol.Optional(ATTR_AMOUNT2): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_AMOUNT)),
    }
)


def test_amount2_is_optional_and_absent_when_not_given() -> None:
    """Every pre-existing caller never passes amount2 at all -- the schema must not require
    it, and a validated call must not invent a key nothing supplied."""
    validated = FEED_SCHEMA({"device_id": "feeder-1", "hopper": "both", "amount": 3})
    assert ATTR_AMOUNT2 not in validated


def test_amount2_is_coerced_and_range_checked_like_amount() -> None:
    validated = FEED_SCHEMA(
        {"device_id": "feeder-1", "hopper": "both", "amount": 3, "amount2": "5"}
    )
    assert validated[ATTR_AMOUNT2] == 5

    with pytest.raises(vol.Invalid):
        FEED_SCHEMA({"device_id": "feeder-1", "hopper": "both", "amount": 3, "amount2": 0})
    with pytest.raises(vol.Invalid):
        FEED_SCHEMA(
            {"device_id": "feeder-1", "hopper": "both", "amount": 3, "amount2": MAX_AMOUNT + 1}
        )


async def test_client_feed_payload_omits_amount2_when_absent() -> None:
    """`api.feed`'s own default -- letting the agent/daemon apply its own `amount2 = amount`
    fallback (`compat.rs::feed`) instead of resending it -- must not regress into always
    sending the key."""
    client = KibbleClient(session=None, host="127.0.0.1", port=8765)
    captured: dict = {}

    async def _fake_request(method, path, payload=None, **kwargs):
        captured["method"], captured["path"], captured["payload"] = method, path, payload
        return {"ok": True}

    client._request = _fake_request  # type: ignore[method-assign]

    await client.feed("both", 4)

    assert captured["payload"] == {"hopper": "both", "amount": 4}


async def test_client_feed_payload_carries_amount2_when_given() -> None:
    client = KibbleClient(session=None, host="127.0.0.1", port=8765)
    captured: dict = {}

    async def _fake_request(method, path, payload=None, **kwargs):
        captured["payload"] = payload
        return {"ok": True}

    client._request = _fake_request  # type: ignore[method-assign]

    await client.feed("both", 4, feed_id="f1", amount2=7)

    assert captured["payload"] == {"hopper": "both", "amount": 4, "id": "f1", "amount2": 7}


class _FakeFeedButton(KibbleFeedButton):
    """`KibbleFeedButton` with `_amount` stubbed straight to a chosen value -- `_amount`'s own
    entity-registry lookup and clamping need a real Home Assistant instance (see
    `tests_ha/test_setup.py` for that end), so this isolates exactly what's new here: does
    `async_press` refuse to call the coordinator at all once that value is 0."""

    def __init__(self, hopper: str, amount: int) -> None:  # noqa: super-init-not-called
        self.entity_description = next(d for d in FEEDS if d.hopper == hopper)
        self.coordinator = SimpleNamespace(async_feed=AsyncMock())
        self._fake_amount = amount

    def _amount(self) -> int:
        return self._fake_amount


async def test_feed_hopper_button_refuses_a_zero_amount() -> None:
    button = _FakeFeedButton(hopper="1", amount=0)

    with pytest.raises(ServiceValidationError):
        await button.async_press()

    button.coordinator.async_feed.assert_not_awaited()


async def test_feed_hopper_button_dispenses_a_nonzero_amount() -> None:
    button = _FakeFeedButton(hopper="2", amount=3)

    await button.async_press()

    button.coordinator.async_feed.assert_awaited_once_with("2", 3)


async def test_combined_feed_button_is_never_refused() -> None:
    """The combined button's own floor is MIN_AMOUNT, never 0 (`_floor`) -- `_amount` can't
    produce a 0 for it, so this exercises the "amount happens to be nonzero" path once more
    for the one button whose companion control was never given a 0 floor at all."""
    button = _FakeFeedButton(hopper=HOPPER_BOTH, amount=1)

    await button.async_press()

    button.coordinator.async_feed.assert_awaited_once_with(HOPPER_BOTH, 1)
