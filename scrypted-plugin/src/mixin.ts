// The mixin device itself: attached to the feeder camera (device 238, "Plant Room Feeder
// Camera"), it adds `ObjectDetector` (backed by the agent's real `/events` feed, honestly
// incomplete where the vendor's own data is unreachable -- see `types.ts`) and `Intercom`
// (backed by a direct, transient RTSP connection to the agent's ONVIF-style backchannel --
// `rtspBackchannel.ts`).

import type {
    Camera, FFmpegInput, Intercom, MediaObject, MixinDeviceOptions,
    ObjectDetectionResult, ObjectDetectionTypes, ObjectDetector, ObjectsDetected,
    VideoCamera,
} from '@scrypted/sdk';
import { MixinDeviceBase, ScryptedInterface, ScryptedMimeTypes } from '@scrypted/sdk';
import { sdk } from './sdkFix';
import * as child_process from 'child_process';
import { agentGetBuffer, agentGetJson } from './agentClient';
import { KibbleDetectionFeed } from './detectionFeed';
import { RtspBackchannelClient } from './rtspBackchannel';
import { findSecondPassDetector, runSecondPass, SecondPassDetector } from './secondPass';
import { FeederConfig, IdentifyResponse, RawDetection } from './types';

/** Matches the agent's own `ai::MAX_EVENTS` -- no reason to cache more crops than the agent
 * itself remembers detections for. */
const MAX_CACHED_CROPS = 50;
const SECOND_PASS_MIN_SCORE = 0.2;
/** 20 ms of L16/16000 (16-bit signed big-endian PCM at the feeder's native 16 kHz): 320 samples,
 * 2 bytes each. Wideband end to end -- no G.711 companding or 8 kHz band-limit between the
 * caller's Opus and the feeder's AAC encoder. `agent/src/backchannel.rs`'s `PT_L16_16K`. */
const L16_FRAME_BYTES = 640;

/** `ObjectDetectionResult.score` is a required field in the SDK's own type, but the vendor's
 * confidence is genuinely unreachable (see `types.ts`). Building the honest, incomplete object
 * against this type and casting once at the point it's handed to the SDK -- rather than
 * inventing a 0/1/NaN placeholder -- is what keeps "omit what does not exist" true at the level
 * of the actual emitted object, not just in a comment. */
type HonestDetectionResult = Omit<ObjectDetectionResult, 'score'> & { score?: number };

export class KibbleFeederMixin extends MixinDeviceBase<VideoCamera & Camera> implements ObjectDetector, Intercom {
    private feed: KibbleDetectionFeed;
    private crops = new Map<string, Buffer>();
    private cropOrder: string[] = [];
    private secondPassDetector?: SecondPassDetector;
    private intercomClient?: RtspBackchannelClient;
    private intercomFfmpeg?: child_process.ChildProcess;

    constructor(options: MixinDeviceOptions<VideoCamera & Camera>, private getConfig: () => FeederConfig) {
        super(options);
        const config = this.getConfig();
        this.feed = new KibbleDetectionFeed(
            config.host, config.httpPort,
            detections => this.onDetections(detections),
            this.console,
        );
        this.feed.start().catch(e => this.console.error('kibble: detection feed failed to start:', e));
    }

    // ---- ObjectDetector ----

    async getDetectionInput(detectionId: string): Promise<MediaObject> {
        const crop = this.crops.get(detectionId);
        if (!crop)
            throw new Error(`kibble: no cached crop for ${detectionId} -- this detection's own "image" field was null (see types.ts)`);
        return this.createMediaObject(crop, 'image/jpeg');
    }

    async getObjectTypes(): Promise<ObjectDetectionTypes> {
        const classes = new Set(['face', 'visit', 'eat']);
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

    // ---- Intercom: direct-to-device ONVIF-style RTSP backchannel ----
    // Scrypted's own Rebroadcast plugin only ever reads from the feeder, so returning audio has
    // to open its own short-lived RTSP session straight to `kibbled` -- see rtspBackchannel.ts
    // and the README for exactly what that costs against the "one video consumer" rule.

    async startIntercom(media: MediaObject): Promise<void> {
        await this.stopIntercom();
        const config = this.getConfig();
        const ffmpegInput = await sdk.mediaManager.convertMediaObjectToJSON<FFmpegInput>(media, ScryptedMimeTypes.FFmpegInput);

        const client = new RtspBackchannelClient(config.host, config.rtspPort, config.rtspPath, this.console);
        await client.connect();
        const { offered } = await client.describeWithBackchannel();
        if (!offered) {
            client.close();
            throw new Error('kibble: feeder did not offer an ONVIF backchannel section in its DESCRIBE response');
        }
        const setup = await client.setupBackchannel('tcp');
        if (setup.code !== 200) {
            client.close();
            throw new Error(`kibble: SETUP trackID=2 (TCP) failed: ${setup.code} ${setup.reason}`);
        }
        const play = await client.play();
        if (play.code !== 200) {
            client.close();
            throw new Error(`kibble: PLAY failed: ${play.code} ${play.reason}`);
        }
        this.intercomClient = client;
        this.console.log('kibble: intercom backchannel negotiated and playing (RTP/AVP/TCP, interleaved 4-5, L16/16000)');

        const ffmpegPath = await sdk.mediaManager.getFFmpegPath();
        const inputArgs = ffmpegInput.inputArguments?.length ? ffmpegInput.inputArguments : ['-i', ffmpegInput.url!];
        // Low-latency flags: no input probing/analysis delay, flush every packet.
        const args = [
            '-fflags', 'nobuffer', '-flags', 'low_delay', '-probesize', '32', '-analyzeduration', '0',
            ...inputArgs, '-vn', '-acodec', 'pcm_s16be', '-ar', '16000', '-ac', '1', '-f', 's16be', '-flush_packets', '1', 'pipe:1',
        ];
        this.console.log(`kibble: intercom ffmpeg: ${ffmpegPath} ${args.join(' ')}`);
        const proc = child_process.spawn(ffmpegPath, args, { stdio: ['ignore', 'pipe', 'pipe'] });
        this.intercomFfmpeg = proc;
        proc.stderr?.resume(); // ffmpeg logs to stderr even on a clean run; nothing here is actionable.
        proc.on('exit', code => this.console.log(`kibble: intercom ffmpeg exited (code ${code})`));

        let pending: Buffer = Buffer.alloc(0);
        proc.stdout?.on('data', (chunk: Buffer) => {
            pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
            while (pending.length >= L16_FRAME_BYTES) {
                client.sendL16Frame(pending.subarray(0, L16_FRAME_BYTES));
                pending = pending.subarray(L16_FRAME_BYTES);
            }
        });
    }

    async stopIntercom(): Promise<void> {
        this.intercomFfmpeg?.kill('SIGTERM');
        this.intercomFfmpeg = undefined;
        if (this.intercomClient) {
            const client = this.intercomClient;
            this.intercomClient = undefined;
            await client.teardown().catch(e => this.console.warn('kibble: intercom teardown failed:', e));
            client.close();
        }
    }

    override release(): void {
        this.feed.stop();
        this.stopIntercom().catch(e => this.console.warn('kibble: stopIntercom during release failed:', e));
        super.release();
    }

    // ---- on-device detection pipeline ----

    private onDetections(detections: RawDetection[]): void {
        for (const raw of detections)
            this.handleOne(raw).catch(e => this.console.error(`kibble: failed to process detection seq=${raw.seq}:`, e));
    }

    private async handleOne(raw: RawDetection): Promise<void> {
        const detectionId = `kibble-${raw.seq}`;
        const config = this.getConfig();

        const onDevice: HonestDetectionResult = { className: raw.class };
        const crop = raw.image ? await this.tryFetchCrop(config.host, config.httpPort, raw.image) : undefined;
        if (crop)
            this.cacheCrop(detectionId, crop);

        if (raw.class === 'face') {
            const identify = await this.tryIdentify(config.host, config.httpPort);
            if (identify?.cat && identify.cat !== 'unknown') {
                onDevice.label = identify.cat;
                if (identify.score !== null)
                    onDevice.labelScore = identify.score;
            }
        } else if (raw.cat) {
            // LibreFeed's `visit`/`eat` rows carry the track's own identification directly (no
            // separate `/identify` round trip, unlike the vendor `face` path above) -- `null`
            // whenever naming is off or nothing was matched, per `types.ts`. Mirrored as-is,
            // never invented.
            onDevice.label = raw.cat;
            if (raw.score !== null)
                onDevice.labelScore = raw.score;
        }

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

    /** `RawDetection.image` names a file under the agent's `EVENTS_DIR`, served directly via
     * `GET /events/<file>` for every class (added upstream after this plugin's own README
     * flagged the gap -- see the "Agent-side TODO" section's history for the prior face-only
     * `/faces/current` workaround this replaced). Events with no image at all (`image: null`)
     * simply get no crop; `getDetectionInput` for those honestly throws instead of guessing. */
    private async tryFetchCrop(host: string, port: number, image: string): Promise<Buffer | undefined> {
        try {
            return await agentGetBuffer(host, port, `/events/${image}`, 5_000);
        } catch (e) {
            this.console.warn(`kibble: GET /events/${image} fetch failed: ${(e as Error).message}`);
            return undefined;
        }
    }

    private async tryIdentify(host: string, port: number): Promise<IdentifyResponse | undefined> {
        try {
            return await agentGetJson<IdentifyResponse>(host, port, '/identify', 5_000);
        } catch (e) {
            this.console.warn(`kibble: /identify fetch failed: ${(e as Error).message}`);
            return undefined;
        }
    }

    private async trySecondPass(crop: Buffer): Promise<ObjectDetectionResult[]> {
        this.secondPassDetector ??= findSecondPassDetector();
        if (!this.secondPassDetector) {
            this.console.warn('kibble: no ONNX/OpenVINO ObjectDetection plugin installed -- second pass skipped, see README');
            return [];
        }
        try {
            const result = await runSecondPass(this.secondPassDetector, crop, SECOND_PASS_MIN_SCORE);
            return result?.detections ?? [];
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
