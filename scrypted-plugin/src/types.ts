// Wire shape of one entry from `GET /events` / `GET /events/stream?since=N` on the feeder's
// agent (LibreFeed's `librefeedd`, `ai::Feed::events_json`/`events_since_json` -- see
// `librefeed/daemon/src/vision.rs`'s `Track::to_json()`). Keep this in sync with that function's
// field list, not with what would be convenient here -- the whole point of this plugin is to
// report what the agent actually says, not a nicer-looking guess. Verified against the live
// feeder's real `GET /events` response (2026-09-25), which no longer matches this file's earlier
// draft: that draft described a flat, one-shot-detection shape (top-level `score`/`box`/`cat`/
// `pet_id`/`total_score`) from a prior agent build. The current build reports TRACKS, not single
// instants -- see the doc comment on `RawDetection` below.

/** One piece of evidence captured for a track. `box`/`score` are honest, live vision-pipeline
 * output; `body`/`face` name crops fetchable the same way as `RawDetection.image` (`GET
 * /events/<file>`), each independently `null` when that particular admitted frame didn't keep
 * one (evidence sampling is throttled -- see `vision.rs`'s module doc, "Evidence sampling"). */
export interface RawDetectionSample {
    /** 1-based index within this track's kept samples (bounded; oldest pruned first). */
    k: number;
    /** Wall-clock unix seconds this sample was captured. */
    t: number;
    /** `[x0, y0, x1, y1]`, each a FRACTION of the frame's width/height in `[0, 1]` -- not pixels,
     * and not `[x, y, width, height]`. Convert with `fractionalBoxToPixels` (`mixin.ts`) before
     * handing to the SDK, whose `ObjectDetectionResult.boundingBox` is pixel `[x, y, w, h]`. */
    box: [number, number, number, number] | null;
    score: number | null;
    body: string | null;
    face: { jpeg: string; emb: string; score: number } | null;
}

/** One feeder track: a "visit" to the bowl, or (once continuous bowl overlap crosses
 * `VisionConfig::eat_hold_ms`, default 3s) an "eat" session. `class`/`open`/`eat_start` together
 * are what this plugin's eating trigger (`KibbleFeederMixin.handleEating`) watches for. */
export interface RawDetection {
    /** Stable for this track's whole life -- unlike `seq` below. Use this, never `seq`, for a
     * detectionId that must stay the same across repeated polls of the same still-open track. */
    event_id: number;
    /** The feed's global monotonic counter. Bumped on every admitted body frame for whichever
     * track is currently open (`vision.rs`'s `Feed::advance`), not just once per track -- so it
     * keeps climbing for as long as a track stays open and its body keeps being admitted. This is
     * what `since`-based poll dedup keys off; it is NOT a stable per-track id (see `event_id`). */
    seq: number;
    /** Wall-clock unix seconds the track opened. */
    ts: number;
    /** Wall-clock unix seconds the track closed, or `null` while still `open`. */
    end: number | null;
    /** `true` while the daemon still considers this track ongoing -- its `samples`, `seq`, and
     * (once crossed) `eat_start`/`class` can all still change on a later poll. */
    open: boolean;
    /** `"visit"` or `"eat"`. Sticky: once a track becomes `"eat"` it never reverts to `"visit"`
     * for the rest of its life. */
    class: string;
    /** Wall-clock unix seconds the track crossed into `eat`, or `null` if it never has (yet). */
    eat_start: number | null;
    /** A wide establishing-frame crop filename for the track, or `null`. */
    scene: string | null;
    /** Evidence samples captured over the track's life, oldest first, bounded (oldest pruned). */
    samples: RawDetectionSample[];
    /** The track's own representative crop filename, or `null` if none was ever captured. */
    image: string | null;
    /** Extra frames bracketing an `eat` close (before/after the bowl visit). Not currently
     * surfaced by this plugin. */
    image_before: string | null;
    image_after: string | null;
}

/** Agent connection settings, read live off the provider's hand-rolled `Settings` on every use so
 * a change in the plugin's Settings UI takes effect without detaching/reattaching the mixin. */
export interface FeederConfig {
    host: string;
    httpPort: number;
    rtspPort: number;
    rtspPath: string;
    secondPassEnabled: boolean;
}


/** What the plugin can ask an attached feeder camera to do directly -- today, just trigger a
 * synthetic test recording (see `KibbleFeederMixin.recordTestClip`). */
export interface RegisteredCamera {
    recordTestClip(): Promise<void>;
}

/** Lets `KibbleFeederMixin` tell the plugin which camera(s) it is currently attached to, and hand
 * over a live reference to itself, since `MixinProvider.getMixin`/`releaseMixin` don't hand the
 * plugin the underlying camera's own `id` directly (`mixinDevice` there is typed narrowly).
 * Mirrors the `plugin.currentMixins[this.id] = this` pattern
 * `@apocaliss92/scrypted-events-recorder`'s own mixin uses for the identical reason. The
 * plugin's `clips` webhook and "Record test clip" button (`main.ts`) both use this to reach "the"
 * feeder camera without hardcoding a device id that can differ across Scrypted installs.
 *
 * Deliberately NOT done via `KibbleFeederMixin` implementing `Settings` itself and contributing a
 * per-camera button the way `objectdetectionplugin:134:*`/`eventsRecorder:*`/etc. do: verified
 * live against this exact instance (2026-09-25) that an ALREADY-ATTACHED outermost mixin gaining
 * a NEW declared interface does not get picked up by device 240's aggregate `getSettings()` --
 * even after a full `setMixins` detach/reattach -- short of a full Scrypted restart. The button
 * lives on the plugin's own Settings (`main.ts`'s `KibbleFeederPlugin`, already proven reliable)
 * instead, and reaches the live mixin instance through this registry. */
export interface CameraRegistry {
    registerCamera(id: string, camera: RegisteredCamera): void;
    unregisterCamera(id: string): void;
}
