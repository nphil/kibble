"""`sensor.pack_schedule_card_state` and `KibbleCoordinator._require_schedule_writes_enabled`:
the two pieces of real logic behind the `sensor.…_schedule_card_state` entity and the
`schedule_card_*` services that read/write it.

The packed-format assertions below re-implement `dispenser-schedule-card`'s own
`status_pattern` regex verbatim from that project's `docs/custom.md` (the `device.type: custom`
adapter's parser), rather than trusting our own docstring -- this is the actual contract the
Kibble card's `kibble-schedule-summary.ts` embed depends on.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ServiceValidationError
from kibble.api import ScheduleEntry
from kibble.const import CONF_ENABLE_SCHEDULE_WRITES
from kibble.coordinator import KibbleCoordinator
from kibble.sensor import MAX_STATE_LENGTH, pack_schedule_card_state

# `dispenser-schedule-card`'s `device.type: custom` adapter's own `status_pattern`
# (docs/custom.md), verbatim.
_CARD_STATUS_PATTERN = re.compile(
    r"(?P<id>[^,]+),(?P<hour>[0-9]{1,2}),(?P<minute>[0-9]{1,2}),(?P<amount>[0-9]{1,2}),(?P<status>[0-9]);?"
)


def _entry(
    id: str, time: str, amount_l: int = 5, amount_r: int = 5, enabled: bool = True
) -> ScheduleEntry:
    return ScheduleEntry(id=id, time=time, amount_l=amount_l, amount_r=amount_r, enabled=enabled)


def test_pack_matches_the_real_cards_status_pattern_regex() -> None:
    """Two enabled entries, differing amount_l/amount_r (mirrored to `max`), pack to a string
    the card's own regex parses back into exactly the fields we fed it -- the worked example:
    `sched-1,7,30,5,2;sched-2,18,0,10,2`."""
    entries = [
        _entry("sched-1", "07:30", amount_l=5, amount_r=5),
        _entry("sched-2", "18:00", amount_l=8, amount_r=10),
    ]

    packed, packed_count, eligible = pack_schedule_card_state(entries)

    assert packed == "sched-1,7,30,5,2;sched-2,18,0,10,2"
    assert packed_count == 2
    assert eligible == 2
    matches = list(_CARD_STATUS_PATTERN.finditer(packed))
    assert [m.group("id") for m in matches] == ["sched-1", "sched-2"]
    assert [m.group("hour") for m in matches] == ["7", "18"]
    assert [m.group("minute") for m in matches] == ["30", "0"]
    assert [m.group("amount") for m in matches] == ["5", "10"]
    assert [m.group("status") for m in matches] == ["2", "2"]


def test_pack_handles_an_entry_at_midnight() -> None:
    """A single-digit hour/minute (midnight) still satisfies the card's `{1,2}` regex quantifier
    and round-trips through it -- the entry that "crosses" into a new day at 00:00."""
    packed, packed_count, eligible = pack_schedule_card_state([_entry("sched-mid", "00:00")])

    assert packed == "sched-mid,0,0,5,2"
    assert packed_count == 1
    assert eligible == 1
    match = _CARD_STATUS_PATTERN.fullmatch(packed + ";")
    assert match is not None
    assert match.group("hour") == "0"
    assert match.group("minute") == "0"


def test_pack_omits_disabled_entries_so_an_all_disabled_table_is_empty() -> None:
    """The custom adapter's `status_map` has no "disabled" code and this integration configures
    no `switch:`, so a disabled entry can only be represented honestly by leaving it out."""
    entries = [
        _entry("a", "08:00", enabled=False),
        _entry("b", "20:00", enabled=False),
    ]

    packed, packed_count, eligible = pack_schedule_card_state(entries)

    assert packed == ""
    assert packed_count == 0
    assert eligible == 0


def test_pack_truncates_before_exceeding_the_255_char_state_limit() -> None:
    """24 entries (the device's own cap) with realistic-length ids blow well past HA's
    255-character state limit; packing must stop before crossing it rather than emit a state
    the recorder would truncate mid-entry, and must say so via the returned counts."""
    entries = [_entry(f"sched-{1_700_000_000 + i}", f"{i % 24:02d}:00") for i in range(24)]

    packed, packed_count, eligible = pack_schedule_card_state(entries)

    assert len(packed) <= MAX_STATE_LENGTH
    assert eligible == 24
    assert 0 < packed_count < eligible
    # every packed entry parses cleanly -- nothing was cut off mid-entry.
    assert len(list(_CARD_STATUS_PATTERN.finditer(packed))) == packed_count


def _fake_coordinator(options: dict) -> SimpleNamespace:
    return SimpleNamespace(entry=SimpleNamespace(options=options))


def test_schedule_writes_gate_refuses_unless_explicitly_enabled() -> None:
    """Default-off, and any falsy/missing option value, refuses with a clear error; only an
    explicit `True` lets a schedule-card write path proceed."""
    for options in ({}, {CONF_ENABLE_SCHEDULE_WRITES: False}):
        fake_self = _fake_coordinator(options)
        with pytest.raises(ServiceValidationError) as excinfo:
            KibbleCoordinator._require_schedule_writes_enabled(fake_self)
        assert excinfo.value.translation_key == "schedule_writes_disabled"

    enabled_self = _fake_coordinator({CONF_ENABLE_SCHEDULE_WRITES: True})
    KibbleCoordinator._require_schedule_writes_enabled(enabled_self)  # must not raise
