// Wire shape of one entry from `GET /events` / `GET /events/stream?since=N` on the feeder's
// agent (LibreFeed's `librefeedd`, still `kibbled`-shaped on this route -- see
// `librefeed/daemon/src/vision.rs`'s `Track::to_json()`, which deliberately mirrors the vendor
// `kibbled`'s own `Detection::to_json()` field list). Keep this in sync with that function's
// field list, not with what would be convenient here -- the whole point of this plugin is to
// report what the agent actually says, not a nicer-looking guess.
//
// `box` and `pet_id` are *always* `null` on this stack: no vendor tracker exists here at all
// (`vision.rs`'s own module doc, "Split" section) -- the vendor's private-mqueue tracker data
// `docs/24-onboard-ai.md` describes belongs to a different, unrelated firmware this plugin does
// not talk to. `score` and `total_score` are the *identification* confidence for `cat`, honestly
// `null` whenever naming is off (the feeder's current `/vision` config) or nothing was matched --
// never a fabricated number. `class`, `image`, `cat`, `vomit`, `image_before`, `image_after` are
// real, live values.
export interface RawDetection {
    seq: number;
    ts: number;
    class: string;
    score: number | null;
    box: [number, number, number, number] | null;
    pet_id: number | null;
    /** A filename under the agent's own events directory, fetchable via `GET /events/<file>`
     * (added upstream after this plugin's own earlier draft flagged the gap -- see `mixin.ts`'s
     * `tryFetchCrop` and the README's "A real gap this plugin found" section). `null` when the
     * event has no crop of its own (e.g. an `eat` closed with no fresh frame). */
    image: string | null;
    /** The feeder's own nearest-centroid classifier's opinion for this specific track, at
     * capture time, or `null` (identification/naming off, or no confident match). Distinct from
     * `GET /identify`'s always-freshest live opinion (`IdentifyResponse` below) -- this is what
     * the track itself was identified as when it happened. */
    cat: string | null;
    /** The vendor's own per-visit tracking score (`state::TrackEntry::value` on vendor
     * firmware) -- a sum over qualifying frames, not a probability, and unrelated to `score`
     * above. Always `null` on this stack (see the module doc); kept for vendor-firmware parity. */
    total_score: number | null;
    /** Extra frames LibreFeed's vision stack captures bracketing an `eat` close (before/after
     * the bowl visit), same fetch mechanism as `image`. `null` when not captured. Not currently
     * surfaced by this plugin -- see `mixin.ts`. */
    image_before: string | null;
    image_after: string | null;
    /** `true` once any frame during this track reported vomiting behaviour above the feeder's
     * own threshold (LibreFeed-only; sticky for the whole track, like `class: "eat"`). Always
     * `false` unless `/vision`'s `vomit` detector is enabled. */
    vomit: boolean;
}

/** Agent connection settings, read live off the provider's `StorageSettings` on every use so a
 * change in the plugin's Settings UI takes effect without detaching/reattaching the mixin. */
export interface FeederConfig {
    host: string;
    httpPort: number;
    rtspPort: number;
    rtspPath: string;
    secondPassEnabled: boolean;
}

/** Wire shape of `GET /identify` (`agent/src/main.rs`'s `identify_get`): Kibble's own
 * nearest-centroid classifier's current opinion, always freshest for "who was just here" per
 * that handler's own doc comment. `score`/`second_best` are real, first-party cosine-similarity
 * numbers from Kibble's own classifier -- distinct from, and not a substitute for, the vendor's
 * honestly-null detection confidence in `RawDetection`. */
export interface IdentifyResponse {
    cat: string | null;
    score: number | null;
    second_best: { cat: string; score: number } | null;
    crop: string | null;
    source: 'labelled' | 'classifier' | null;
    ts: number | null;
}
