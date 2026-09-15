// Wire shape of one entry from `GET /events` / `GET /events/stream?since=N` on the Kibble agent
// (kibbled), exactly as produced by `agent/src/ai.rs`'s `Detection::to_json()`. Keep this in sync
// with that function's field list, not with what would be convenient here -- the whole point of
// this plugin is to report what the agent actually says, not a nicer-looking guess.
//
// `score`, `box` and `pet_id` are *always* `null` today: that data lives only inside a private
// vendor message queue (`ctrl`'s own `/msg_dispatch_1`) that `docs/24-onboard-ai.md` documents in
// full but that Kibble deliberately does not tap (a POSIX mqueue has exactly one reader, and
// stealing `ctrl`'s messages would be a real behavioural change to a vendor process). `class`,
// `image` (a filename, see below) and `cat` are real.
export interface RawDetection {
    seq: number;
    ts: number;
    class: string;
    score: number | null;
    box: [number, number, number, number] | null;
    pet_id: number | null;
    /**
     * A filename under the agent's own `/opt/kibble/events/` directory -- NOT a URL and NOT
     * fetchable over HTTP. `agent/src/main.rs` never wires a route that serves this directory
     * (confirmed by reading its full route table); only `class: "face"` crops are *also* written
     * to `faces::PENDING_DIR`, which the agent's `/faces/current`/`/faces/pending/<name>` GETs do
     * serve. See `KibbleDetectionFeed`'s doc comment for exactly how this plugin works around that
     * real gap instead of pretending the field is directly usable.
     */
    image: string | null;
    /** Kibble's own nearest-centroid classifier's opinion at capture time, or `null`. */
    cat: string | null;
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
