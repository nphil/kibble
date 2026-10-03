# Kibble vs. the Home Assistant Integration Quality Scale

Audit against `homeassistant==2026.9.2`'s real quality-scale rule set (verified via `reolink`'s
own bundled `quality_scale.yaml` and `developers.home-assistant.io`'s rule pages, not memory).
Full per-rule status lives in `custom_components/kibble/quality_scale.yaml`; this document is
the reasoning behind it, and — the most important part — where the scale would and would not
have caught each of the five real incidents that motivated this work.

**Kibble is a HACS-distributed custom integration, never submitted to `home-assistant/core`.**
`quality_scale.yaml` is a voluntary self-assessment against the same rubric core integrations
are held to; `hassfest` (the CI tool that validates it) never runs on this repo, and rules tied
to the core repo's own infrastructure (`brands`, most `docs-*` rules, `.strict-typing`
registration) are marked `exempt` for that structural reason, not because the underlying
practice doesn't matter.

## Defect → rule mapping

| # | Defect | Rule(s) that govern it | Did the rule catch it? |
|---|---|---|---|
| 1 | Starved device HTTP server → all 51 entities `unavailable` mid-dashboard | `entity-unavailable` (Silver), `log-when-unavailable` (Silver), `parallel-updates` (Silver) | **No — and this is the important finding.** `entity-unavailable`'s own canonical example is "raise `UpdateFailed` on any fetch failure," which is *exactly* the shipped behavior that caused the outage. The rule's literal example does not distinguish "the fetch failed once" from "the device is down"; nothing in the quality scale would have flagged the original code as wrong, because by the rule's own letter it was compliant. What actually protects against this is deeper: `coordinator.py`'s module docstring makes an explicit, considered exception to the rule's example (a consecutive-failure tolerance window) and defends it from the device's own known behavior (three of five attempts failed in the real incident). No mechanical rule check produces this; it takes knowing the device. |
| 2 | Bad import (`UnitOfSignalStrength`, removed in 2026.9) → entire config entry failed, all platforms | No rule directly targets this. `common-modules`, `strict-typing` are adjacent but don't cover it. | **No.** There is no "verify your imports against the installed HA version" rule in the quality scale at all — it's a CI-time concern (`hassfest`/`mypy` in `home-assistant/core`'s own pipeline), invisible to a HACS integration with no such pipeline. This is why the assignment's own verification step (a project-local venv, AST-checking every `homeassistant.*` import against the real package) exists: it's a gap the quality scale doesn't cover for custom integrations, full stop. The *isolation* half of this defect (one platform's failure taking down all nine) is closer to `config-entry-unloading`'s spirit but isn't literally that rule either — nothing in the scale asks for cross-platform failure isolation within one entry's setup. |
| 3 | `EntityCategory.CONFIG` on `sensor`/`binary_sensor` → 7 entities failed to add every startup | `entity-category` (Gold) | **Yes, directly** — `entity-category` says entities must be assigned an *appropriate* category, and HA's own `SensorEntity`/`BinarySensorEntity.async_internal_added_to_hass` hard-enforces this specific combination by raising `HomeAssistantError`. Gold-tier review (or the regression test this pass adds, `tests/test_entity_platform_rules.py`) would have caught it immediately. The gap wasn't the rule; it was that nothing before this pass checked for it in code. |
| 4 | Entity IDs missing area prefixes / stray `_2` suffixes, visible only once a competing integration was removed | `entity-unique-id` (Bronze), `has-entity-name` (Bronze) indirectly | **Partially.** `entity-unique-id`/`has-entity-name` (both already `done`, unchanged by this pass) prevent unique-id collisions and let HA generate correct, de-duplicated entity IDs from device+entity names — which is exactly the mechanism that *should* have produced correct IDs from day one. But the rules don't cover the specific failure mode here (a naming collision surfacing only once a *different* integration's device was removed and stopped occupying an entity-id slot); that's an emergent HA-registry interaction, not something a static rule check can see ahead of time. Fixed by hand before this pass; nothing further needed here. |
| 5 | Single-client-ish device HTTP server shared with Scrypted/HomeKit, polled every 10s with no timeout budget | `appropriate-polling` (Bronze), `parallel-updates` (Silver) | **Partially.** `appropriate-polling` asks for *a* sane interval, which Kibble already had (10s, deliberately chosen — see `const.py`); it doesn't ask for a *timeout budget* proportional to that interval, which was the actual defect (`api.TIMEOUT` was 10s per request, equal to the whole poll interval, times up to twelve sequential requests per cycle). `parallel-updates` is about concurrent requests *from HA*, which were already correctly serialized by `api.py`'s own lock — that part was never the problem. The real fix (tightened per-request timeout, an aggregate `POLL_TIMEOUT` bounding the whole cycle, backoff once genuinely down) isn't described by any single rule; it's ordinary defensive engineering for a slow, shared, embedded HTTP server that the scale doesn't have a dedicated rule for. |

**The honest summary for Nitin:** of the five real incidents, the quality scale's own rules
would only have unambiguously caught #3. #2 and #5 are structurally outside what the scale
checks (import-version drift, timeout budgeting) — those need the AST-import-verification
habit and ordinary defensive-engineering judgement this pass applied, not a rule citation. #1 is
the most interesting case: the rule's own *example* would have produced the exact bug; avoiding
it took contradicting that example on purpose, for a documented, device-specific reason. #4 is
a registry-interaction edge case no static rule reaches. None of this means the quality scale is
without value — it caught real, fixable gaps (`entity-category`, `parallel-updates`,
`entity-disabled-by-default`, `diagnostics`, `repair-issues`, `exception-translations`) — but it
is not a substitute for understanding the specific device this integration talks to.

## The degraded-availability policy (defect #1), in one place

Implemented in `coordinator.py`; full reasoning in that module's docstring. Summary:

- A poll that fails is tolerated up to `CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1` (2) times in a
  row *if* a prior good snapshot exists: `_async_update_data` returns the stale snapshot instead
  of raising, so `DataUpdateCoordinator.last_update_success` stays `True` and **every entity
  keeps showing its last real value** — stale-but-present, not unavailable. For a cat feeder,
  this is the correct trade: bowl-fill percentage, Wi-Fi signal, the cached schedule don't go
  stale in any way that matters over 10–20 seconds, and a dashboard that flickers unavailable
  and back for a device that was never actually down is strictly worse than one showing a
  20-second-old number.
- At the 3rd consecutive failure, it raises `UpdateFailed` for real — HA's own coordinator then
  correctly marks every entity unavailable (because by this point it is no longer a guess), logs
  once at `error` (satisfying `log-when-unavailable` for free), and `coordinator.py` raises a
  repair issue (`feeder_unresponsive`) explaining *why*, which clears automatically on recovery.
  `UpdateFailed.retry_after` also kicks in an exponential backoff (capped at 60s) past this
  point, so a feeder down for minutes gets polled less aggressively.
- The one exception: the very first poll (no prior data to fall back on) always raises
  immediately on failure. Since 0.29.2 it no longer runs inside `async_setup_entry`, so HA does
  not turn that into `ConfigEntryNotReady` and retry setup; `async_poll_until_first_data` tries
  again in the background (5 s growing to 30 s) until the feeder answers — see "Startup: the
  first poll runs in the background" below.
- A new disabled-by-default (house rule 4) diagnostic binary sensor, `binary_sensor.…_reachable`
  (`device_class: connectivity`), deliberately overrides `available` to always be `True` so it
  keeps reporting *through* the tolerance window that hides everything else — it is the one
  entity a user who enables it can watch to see "starting to have trouble" before anything goes
  unavailable for real.
- `CONSECUTIVE_FAILURES_FOR_UNAVAILABLE = 3` is not arbitrary: the real incident saw `GET
  /state` fail 3 of 5 attempts at a 10s timeout — ones and twos are this device's ordinary noise
  floor at this poll interval (some other consumer mid-request), three in a row is ~30s of zero
  contact despite three independent tries, which is where "busy" stops being the likelier
  explanation than "down."

Regression test: `tests/test_coordinator_availability.py` (17 tests) exercises the real, bound
`KibbleCoordinator` methods directly — below-threshold tolerance, threshold-crossing raise,
no-prior-data-raises-immediately, backoff growth, repair-issue create/clear timing, and the
aggregate `POLL_TIMEOUT` actually bounding a hung fetch.

## Startup: the first poll runs in the background (0.29.2)

Home Assistant reports "started" only after every integration's `async_setup_entry` has
returned. This feeder's first poll is a dozen serial calls to a slow device (8–18 s; **17.7 s**
measured at a real restart), and `async_setup_entry` used to await it, so one slow feeder held
the whole restart up. Now:

- `async_setup_entry` returns within `SETUP_BUDGET_SECONDS` (5 s) however the feeder is doing.
  Measured against the previous release in the same harness: a feeder that never answers held
  setup for 25.7 s and then failed into a setup retry; one taking 1.4 s per call held it 19.3 s.
  Both now return in 5.0 s with the entry loaded.
- Setup itself only does local work (open the store, forward every platform). The first poll
  runs in a task the entry owns (`_async_finish_setup`), and setup waits for it only for what is
  left of the budget — a feeder that answers in time leaves setup looking exactly as before.
- Every platform's `async_setup_entry` hands its entity creation to
  `entity.async_when_data_ready`: at once if the first poll has landed, otherwise the moment it
  does. Entities are built from the poll's data (serial, firmware, running stack), so none is
  created from a guess; until the poll lands the entity registry's restored/unavailable
  placeholders stand in. The push channel, retention and the two restart sweeps start at the
  same moment, in the order setup always ran them.
- No `ConfigEntryNotReady` for a feeder that has not answered: the task retries by itself
  (`FIRST_POLL_RETRY_MIN`..`MAX`, 5 s growing to 30 s), which recovers faster than HA's own
  retry delay (up to 80 s). Setup still fails with `ConfigEntryNotReady` only when no platform
  could be set up at all.
- While there is no data, services answer `feeder_not_ready` and websocket commands answer
  `not_found` ("Entry not loaded yet…") — the code an entry that is still setting up has always
  got — so nothing reads a missing snapshot and nothing reaches the feeder. Diagnostics works
  throughout (its `data` is `null` until the poll lands).
- Unload cancels the task first, then unloads the platforms, so a poll cannot land in between
  and create entities for platforms that are gone.
- Rule `test-before-setup` is therefore deliberately not met any more (`quality_scale.yaml`
  records it as exempt with this reason); `config_flow.py` still proves the feeder answers
  before the entry is created.
- Push's connect/upgrade handshake is also bounded to 10 s now (`CONNECT_TIMEOUT_SECONDS`): a
  feeder that accepts the connection and never answers used to hold the push task for as long
  as the shared HTTP session allowed.

Tests: `tests_ha/test_setup.py` (setup within budget with a never-answering feeder, entities
when the poll lands later, nothing written to the feeder meanwhile, background retry of refused
polls, unload while the poll is pending, services/websocket refusing, stack-change reload) and
`tests/test_first_poll.py` (retry back-off, queued creation, cancellation).

## Platform-isolation reality check (defect #2)

`__init__.py`'s `_async_forward_platforms_isolated` forwards each platform on its own
`async_forward_entry_setups(entry, [platform])` call instead of one call for the whole list.
This matters because HA's own implementation batches every platform in one call under a single
`asyncio.gather(...)` **without** `return_exceptions=True` — one platform's exception fails that
whole `gather`, taking every other platform down with it. Calling it once per platform gives
each platform its own isolated `gather` (of one), so a bad import or setup bug in, say, `image.py`
no longer prevents `camera`/`button`/`sensor` from loading. This is a real, structurally-verified
fix (`tests/test_platform_isolation.py`), not a re-implementation of retry/exception handling —
it relies entirely on the fact that a single-element `gather` fails independently of any other
`gather`.

**What is genuinely NOT isolated, and why that's correct:**
- The coordinator's *first* poll is no longer part of setup at all (see "Startup" above), so
  there is no first-refresh failure to isolate: a feeder that has not answered just means no
  platform has created its entities yet, and every one of them does when it answers.
- A hard config-entry-lifecycle exception (`ConfigEntryNotReady`/`ConfigEntryAuthFailed`) raised
  from *within* a platform's own `async_setup_entry` would be caught by the broad
  `except Exception` in `_async_forward_platforms_isolated` and merely logged, not given the
  special handling HA's setup machinery would otherwise apply (e.g. triggering a retry).
  Structural note, not a live gap: none of Kibble's nine platforms do any independent I/O or
  auth in their own `async_setup_entry` — they only register entities against data the
  coordinator already fetched — so none of them currently has a code path that raises one.
- If literally every platform fails, the entry still fails setup (`ConfigEntryNotReady`) rather
  than silently loading with zero entities.

## Regression test for defect #3

`tests/test_entity_platform_rules.py` (28 tests, 139 parametrized cases) walks every entity
Kibble defines — both `EntityDescription` tuples and one-off hardcoded classes — and asserts,
among other things, that no `sensor`/`binary_sensor` entity ever carries
`EntityCategory.CONFIG`. Two tests specifically prove the check has teeth by feeding it a
synthetic instance of the exact defect shape (`EntityCategory.CONFIG` on a
`SensorEntityDescription`) and confirming it's rejected — this is the literal regression test
requested: it demonstrably fails on the shape of the shipped defect and passes on the current,
fixed entity descriptions. The same test file also caught and let this pass fix 5 previously
undetected house-rule-4 violations (`desiccant_days`, `cloud_connection`, `control_path`,
`wifi`, `wifi_signal` sensors — all `DIAGNOSTIC` but missing
`entity_registry_enabled_default=False`) that existed before this pass and had no other check.

## Quality-scale rules newly satisfied this pass

`entity-unavailable`, `log-when-unavailable`, `parallel-updates`, `action-exceptions`,
`exception-translations`, `entity-category` (regression-tested), `entity-disabled-by-default`
(regression-tested, 5 real violations fixed), `diagnostics`, `repair-issues`, and the
`devices` rule's `configuration_url` field. See `quality_scale.yaml` for the full rule-by-rule
status, including 15 rules honestly left `todo` (mostly README-content rules not verified this
session — no claim is made there either way) rather than claimed without verification.
