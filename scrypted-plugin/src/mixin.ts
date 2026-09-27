// The mixin device itself: attached to the feeder camera (device 240, "Plant Room Cat Feeder"),
// it adds `ObjectDetector` (backed by the agent's real `/events` feed, honestly incomplete where
// the vendor's own data is unreachable -- see `types.ts`) and registers itself with the plugin
// (`RegisteredCamera`) so the plugin's OWN Settings can trigger `recordTestClip` -- see
// `types.ts`'s `CameraRegistry` doc comment for why the test-clip button lives there, not here.
//
// Two-way audio used to live here too, over the agent's ONVIF-style RTSP backchannel. It moved
// to the `camera-intercom` plugin's `onvif-backchannel` driver, which serves every camera in the
// system from one place; the backchannel is the most generic of its drivers, so the feeder now
// shares code with the rest rather than carrying its own copy. This plugin keeps only what is
// genuinely feeder-specific: the detection feed.
//
// Eating-clip design: `kibble/docs/39-eating-clips.md`. In short -- while a feeder track is open
// AND eating, `handleEating` emits a real-box, `className: 'cat'` detection on every poll, with a
// detectionId stable for that track's whole life. `@apocaliss92/scrypted-events-recorder`'s own
// mixin (configured with `detectionClasses: ['animal']`, `ignoreCameraDetections: true`) listens
// for exactly this and records one clip spanning the meal. Everything else this mixin reports
// (the one-shot `handleOne` path, for NVR smart-search labels) is deliberately boxless or
// filtered to never itself carry an Animal-mapped class, so it can never also trigger a clip --
// see `trySecondPass`'s own doc comment.

import type {
    Camera, MediaObject, MixinDeviceOptions,
    ObjectDetectionResult, ObjectDetectionTypes, ObjectDetector, ObjectsDetected,
    VideoCamera,
} from '@scrypted/sdk';
import { MixinDeviceBase, ScryptedInterface } from '@scrypted/sdk';
import { agentGetBuffer } from './agentClient';
import { KibbleDetectionFeed } from './detectionFeed';
import { findSecondPassDetector, runSecondPass, SecondPassDetector } from './secondPass';
import { CameraRegistry, FeederConfig, RawDetection, RegisteredCamera } from './types';

/** Matches the agent's own `ai::MAX_EVENTS` -- no reason to cache more crops than the agent
 * itself remembers detections for. */
const MAX_CACHED_CROPS = 50;
const SECOND_PASS_MIN_SCORE = 0.2;

/** `ObjectDetectionResult.score` is a required field in the SDK's own type, but the vendor's
 * confidence is genuinely unreachable (see `types.ts`). Building the honest, incomplete object
 * against this type and casting once at the point it's handed to the SDK -- rather than
 * inventing a 0/1/NaN placeholder -- is what keeps "omit what does not exist" true at the level
 * of the actual emitted object, not just in a comment. */
type HonestDetectionResult = Omit<ObjectDetectionResult, 'score'> & { score?: number };

/** The feeder's own main stream, and the frame `RawDetectionSample.box` fractions are relative
 * to. Matches both the feeder's own encoder (`librefeed-media`: "main (1920x1080 H.264 ...)",
 * `librefeed/docs/09-architecture.md` section 2) and what Scrypted's Rebroadcast plugin reports
 * live for this camera's "main" stream (confirmed against the actual running instance, 2026-09-25
 * -- see the eating-clips design doc). Not read dynamically from `getVideoStreamOptions()`: it is
 * a hardware property of the feeder's own encoder, not something that changes at runtime, and a
 * per-poll live lookup would add a round trip for no benefit -- the recorder only ever checks
 * `boundingBox` presence, never its exact geometry. */
const EATING_INPUT_DIMENSIONS: [number, number] = [1920, 1080];

/** Classes that map to Events Recorder's own "Animal" trigger category (its
 * `detectionClasses.ts`, not imported here -- a different plugin's private source, not a
 * dependency this one should take). Looked up in `trySecondPass` to strip any such result, so
 * this mixin's general-purpose, always-on off-device re-check (which runs for every class with a
 * crop, not just "eat") can never itself become a second, uncontrolled Animal trigger source
 * outside `handleEating`'s dedicated eating-only path -- see this file's own header and
 * `kibble/docs/39-eating-clips.md`. Real, low-confidence 'animal'-classed second-pass hits were
 * confirmed live in the day-partitioned events.json files under /NVR/clips/240/events/ before this filter existed. */
const SECOND_PASS_ANIMAL_CLASSES: Record<string, true> = { animal: true, cat: true, dog: true, dog_cat: true };

/** Exported so `main.ts` can quote the same number in the "Record test clip" button's own
 * description (that button lives on the plugin's Settings -- see this file's header). */
export const TEST_CLIP_DURATION_MS = 30_000;
/** Matches the real detection feed's own poll gap, so a "Record test clip" run looks, from the
 * recorder's point of view, exactly like a real meal being polled. */
const TEST_CLIP_POLL_MS = 5_000;
/** A plausible, centered "cat at the bowl" box (roughly the lower-middle of frame) -- there is no
 * real track to read one from during a synthetic test. */
const TEST_CLIP_BOX: [number, number, number, number] = [0.3, 0.35, 0.7, 0.9];

/** `box` is `[x0, y0, x1, y1]` as a FRACTION of `dimensions` (`[0, 1]` each way, per
 * `RawDetectionSample.box`'s own doc comment) -- converts to the SDK's pixel `[x, y, w, h]`.
 * Clamps each fraction into `[0, 1]` first: this is fed straight to the Events Recorder's trigger
 * filter, which only cares that `boundingBox` is present, but a negative width/height would be a
 * lie about what was actually seen. */
function fractionalBoxToPixels(
    box: [number, number, number, number], dimensions: [number, number],
): [number, number, number, number] {
    const [x0, y0, x1, y1] = box.map(n => Math.min(1, Math.max(0, n)));
    const [w, h] = dimensions;
    return [x0 * w, y0 * h, Math.max(0, x1 - x0) * w, Math.max(0, y1 - y0) * h];
}

export class KibbleFeederMixin extends MixinDeviceBase<VideoCamera & Camera> implements ObjectDetector, RegisteredCamera {
    private feed: KibbleDetectionFeed;
    private crops = new Map<string, Buffer>();
    private cropOrder: string[] = [];
    private unavailableImages = new Set<string>();
    private unavailableImageOrder: string[] = [];
    private secondPassDetector?: SecondPassDetector;
    private testClipTimer?: NodeJS.Timeout;
    private testClipDetectionId?: string;

    constructor(
        options: MixinDeviceOptions<VideoCamera & Camera>,
        private getConfig: () => FeederConfig,
        private registry: CameraRegistry,
    ) {
        super(options);
        this.registry.registerCamera(this.id, this);
        const config = this.getConfig();
        this.feed = new KibbleDetectionFeed(
            config.host, config.httpPort,
            (snapshot, fresh) => this.onSnapshot(snapshot, fresh),
            this.console,
        );
        this.feed.start().catch(e => this.console.error('kibble: detection feed failed to start:', e));
    }

    // ---- ObjectDetector ----

    async getDetectionInput(detectionId: string): Promise<MediaObject> {
        const crop = this.crops.get(detectionId);
        if (!crop) {
            // A missing image is normal when the feeder's transient spool asset was already
            // acknowledged by Home Assistant, or the event has no image (including test triggers).
            // Returning no input keeps the detection valid without making the NVR log a failure.
            return undefined as unknown as MediaObject;
        }
        return this.createMediaObject(crop, 'image/jpeg');
    }

    async getObjectTypes(): Promise<ObjectDetectionTypes> {
        const classes = new Set(['face', 'visit', 'eat', 'cat']);
        if (this.getConfig().secondPassEnabled) {
            this.secondPassDetector ??= findSecondPassDetector();
            if (this.secondPassDetector) {
                const model = await this.secondPassDetector.getDetectionModel().catch(() => undefined);
                for (const c of model?.classes ?? [])
                    classes.add(c);
            }
        }
        return { classes: [...classes] };
    }

    override release(): void {
        this.feed.stop();
        if (this.testClipTimer)
            clearTimeout(this.testClipTimer);
        this.registry.unregisterCamera(this.id);
        super.release();
    }

    // ---- on-device detection pipeline ----

    private onSnapshot(snapshot: RawDetection[], fresh: RawDetection[]): void {
        for (const raw of fresh)
            this.handleOne(raw).catch(e => this.console.error(`kibble: failed to process detection seq=${raw.seq}:`, e));
        this.handleEating(snapshot).catch(e => this.console.error('kibble: failed to process eating trigger:', e));
    }

    /** One-shot, per-fresh-track augmentation for NVR smart search: mirrors the track's class as
     * className and optionally re-checks its crop off-device. The feeder no longer exposes identity
     * endpoints; identity belongs to Home Assistant. Deliberately never sets boundingBox on its own
     * result -- see this file's header comment for why only handleEating may trigger the recorder. */
    private async handleOne(raw: RawDetection): Promise<void> {
        const detectionId = `kibble-${raw.seq}`;
        const config = this.getConfig();

        const onDevice: HonestDetectionResult = { className: raw.class };
        const crop = raw.image ? await this.tryFetchCrop(config.host, config.httpPort, raw.image) : undefined;
        if (crop)
            this.cacheCrop(detectionId, crop);

        // Every class the agent reports (`face`/`visit`/`eat`) is feeder-native and always

        // See the module doc comment on `HonestDetectionResult` for why this cast, and only this
        // one field, is missing rather than fabricated.
        const results: ObjectDetectionResult[] = [onDevice as ObjectDetectionResult];
        if (crop && config.secondPassEnabled)
            results.push(...await this.trySecondPass(crop));

        await this.onDeviceEvent(ScryptedInterface.ObjectDetector, {
            detectionId,
            timestamp: raw.ts * 1000,
            detections: results,
        } satisfies ObjectsDetected);
    }

    /** The eating trigger: while a feeder track is open AND eating (`class === 'eat'`, or
     * `eat_start` already set), emits a real-box, `className: 'cat'` detection every poll -- see
     * this file's header and `kibble/docs/39-eating-clips.md`. Uses the track's own `event_id`
     * (stable for its whole life, unlike `seq` -- see `types.ts`) for a detectionId in its own
     * `kibble-eat-` namespace, so it can never collide with `handleOne`'s per-seq one-shot ids. */
    private async handleEating(snapshot: RawDetection[]): Promise<void> {
        const track = snapshot.find(d => d.open && (d.class === 'eat' || d.eat_start !== null));
        if (!track)
            return;

        const sample = track.samples[track.samples.length - 1];
        if (!sample?.box) {
            this.console.warn(`kibble: eating track event_id=${track.event_id} is open with no usable sample box yet, skipping this poll's trigger`);
            return;
        }

        const detectionId = `kibble-eat-${track.event_id}`;
        const config = this.getConfig();

        if (sample.body) {
            const crop = await this.tryFetchCrop(config.host, config.httpPort, sample.body);
            if (crop)
                this.cacheCrop(detectionId, crop);
        }

        const onDevice: ObjectDetectionResult = {
            className: 'cat',
            score: sample.score ?? 0.9,
            boundingBox: fractionalBoxToPixels(sample.box, EATING_INPUT_DIMENSIONS),
        };

        await this.onDeviceEvent(ScryptedInterface.ObjectDetector, {
            detectionId,
            timestamp: Date.now(),
            detections: [onDevice],
            inputDimensions: EATING_INPUT_DIMENSIONS,
        } satisfies ObjectsDetected);
    }

    /** Backs the plugin's "Record test clip" setting button (`main.ts`, via `RegisteredCamera`):
     * emits the same shape of detection `handleEating` does, on the same cadence, for
     * `TEST_CLIP_DURATION_MS`, against a synthetic centered box -- there is no real track to read
     * one from. Called again while already running, it extends the same run (same detectionId)
     * rather than starting a second one. */
    async recordTestClip(): Promise<void> {
        if (this.testClipTimer) {
            this.console.log('kibble: "Record test clip" pressed again while a test was already running -- extending it');
            clearTimeout(this.testClipTimer);
        }
        this.testClipDetectionId ??= `kibble-test-${Date.now()}`;
        const detectionId = this.testClipDetectionId;
        const deadline = Date.now() + TEST_CLIP_DURATION_MS;
        this.console.log(`kibble: "Record test clip" pressed -- emitting synthetic eating detections for ${TEST_CLIP_DURATION_MS / 1000}s (detectionId=${detectionId})`);

        const emitOnce = async (): Promise<void> => {
            await this.onDeviceEvent(ScryptedInterface.ObjectDetector, {
                detectionId,
                timestamp: Date.now(),
                detections: [{
                    className: 'cat',
                    score: 0.95,
                    boundingBox: fractionalBoxToPixels(TEST_CLIP_BOX, EATING_INPUT_DIMENSIONS),
                }],
                inputDimensions: EATING_INPUT_DIMENSIONS,
            } satisfies ObjectsDetected);
            if (Date.now() < deadline) {
                this.testClipTimer = setTimeout(() => void emitOnce(), TEST_CLIP_POLL_MS);
            } else {
                this.console.log('kibble: "Record test clip" finished emitting');
                this.testClipTimer = undefined;
                this.testClipDetectionId = undefined;
            }
        };
        await emitOnce();
    }

    /** RawDetection.image and RawDetectionSample.body are flat asset names served by the feeder's
     * transient spool at GET /events/<name>. Home Assistant archives each asset and then
     * acknowledges it with DELETE /events/<name>, so a 404 means the image is no longer
     * available from the feeder, not a malformed date-prefixed path. Encode it as one URL segment. */
    private async tryFetchCrop(host: string, port: number, image: string): Promise<Buffer | undefined> {
        if (this.unavailableImages.has(image))
            return undefined;
        try {
            return await agentGetBuffer(host, port, '/events/' + encodeURIComponent(image), 5_000);
        } catch (e) {
            const message = e instanceof Error ? e.message : String(e);
            if (message.includes('HTTP 404')) {
                this.rememberUnavailableImage(image);
                return undefined;
            }
            this.console.warn('kibble: GET /events/' + encodeURIComponent(image) + ' fetch failed: ' + message);
            return undefined;
        }
    }

    private rememberUnavailableImage(image: string): void {
        if (this.unavailableImages.has(image))
            return;
        this.unavailableImages.add(image);
        this.unavailableImageOrder.push(image);
        if (this.unavailableImageOrder.length > MAX_CACHED_CROPS) {
            const oldest = this.unavailableImageOrder.shift();
            if (oldest !== undefined)
                this.unavailableImages.delete(oldest);
        }
    }

    /** Runs the crop through an installed general-purpose ONNX/OpenVINO detector for a real,
     * off-device class + confidence score. Strips Animal-mapped results so only the dedicated
     * eating path can trigger the recorder. */
    private async trySecondPass(crop: Buffer): Promise<ObjectDetectionResult[]> {
        this.secondPassDetector ??= findSecondPassDetector();
        if (!this.secondPassDetector) {
            this.console.warn('kibble: no ONNX/OpenVINO ObjectDetection plugin installed -- second pass skipped, see README');
            return [];
        }
        try {
            const result = await runSecondPass(this.secondPassDetector, crop, SECOND_PASS_MIN_SCORE);
            const detections = result?.detections ?? [];
            return detections.filter(d => !SECOND_PASS_ANIMAL_CLASSES[d.className]);
        } catch (e) {
            this.console.warn(`kibble: second-pass detection failed: ${(e as Error).message}`);
            return [];
        }
    }

    private cacheCrop(id: string, bytes: Buffer): void {
        this.crops.set(id, bytes);
        this.cropOrder.push(id);
        while (this.cropOrder.length > MAX_CACHED_CROPS)
            this.crops.delete(this.cropOrder.shift()!);
    }
}
