// Plugin entry point: a MixinProvider that attaches ObjectDetector to the feeder camera (see
// mixin.ts), plus the plugin-wide Settings (feeder host/ports, second-pass toggle, and a "Record
// test clip" button that reaches the live mixin instance via `CameraRegistry`/`RegisteredCamera`
// -- see `types.ts`'s doc comment on why that button lives here and not on the mixin itself),
// plus a public, read-only HTTP endpoint (`clips`) HA polls for the feeder camera's Events
// Recorder clips -- see `onRequest` and `kibble/docs/39-eating-clips.md`.
//
// Two-way audio moved out of this plugin to `camera-intercom`, whose `onvif-backchannel` driver
// speaks the same backchannel this used to implement here, for every camera in the system. Only
// the feeder-specific detection feed remains.
//
// `./sdkFix` self-heals a confirmed runtime gap in this exact @scrypted/sdk@0.5.59 deployment
// (see that file's doc comment) as a side effect of being imported; every file that touches
// `systemManager`/`mediaManager`/`deviceManager` imports the `sdk` object from there, never from
// `@scrypted/sdk` directly.
//
// Settings are hand-rolled against `this.storage` directly rather than via the published
// `@scrypted/sdk/storage-settings` helper: that module's compiled `dist/src/storage-settings.js`
// destructures `const { systemManager } = require('.').default` at its own top level, which runs
// before `./sdkFix` ever gets a chance to run -- merely importing that module throws `Cannot
// destructure property 'systemManager' of ...` and takes the whole bundled plugin down with it.
// None of these settings need `type: 'device'` (the one thing that helper needs `systemManager`
// for), so avoiding it entirely is the cleanest fix.

import type {
    Camera, HttpRequest, HttpRequestHandler, HttpResponse, MixinProvider, ScryptedDeviceType,
    Setting, Settings, SettingValue, VideoCamera, VideoClips, WritableDeviceState,
} from '@scrypted/sdk';
import { ScryptedDeviceBase, ScryptedInterface } from '@scrypted/sdk';
import { KibbleFeederMixin, TEST_CLIP_DURATION_MS } from './mixin';
import { sdk } from './sdkFix';
import { CameraRegistry, FeederConfig, RegisteredCamera } from './types';

const { systemManager } = sdk;

const DEFAULTS: Record<string, string> = {
    feederHost: '192.168.1.85', // moved networks once already -- this is why it's a setting, not a constant
    feederHttpPort: '8765',
    feederRtspPort: '8554',
    feederRtspPath: 'sub',
    secondPassEnabled: 'true',
};

const RECORD_TEST_CLIP_KEY = 'recordTestClip';

const SETTING_DEFS: Setting[] = [
    {
        key: 'feederHost',
        title: 'Feeder host',
        description: 'LAN IP of the Kibble agent (kibbled) running on the feeder itself.',
        type: 'string',
    },
    {
        key: 'feederHttpPort',
        title: 'Feeder HTTP port',
        type: 'number',
    },
    {
        key: 'feederRtspPort',
        title: 'Feeder RTSP port',
        type: 'number',
    },
    {
        key: 'feederRtspPath',
        title: 'Feeder RTSP mount',
        description: 'Mount ("sub" or "main") used when this plugin needs its own RTSP session. '
            + 'Two-way audio -- which is what used to open that session -- now lives in '
            + '@nphil/camera-intercom, so nothing here opens one today; the setting stays '
            + 'because FeederConfig still carries it and the agent\'s mounts are per-install.',
        type: 'string',
        choices: ['sub', 'main'],
    },
    {
        key: 'secondPassEnabled',
        title: 'Second-pass detection',
        description: 'Re-check on-device "face" detections against an installed ONNX/OpenVINO '
            + 'ObjectDetection plugin for a real, off-device class + confidence score.',
        type: 'boolean',
    },
    {
        key: RECORD_TEST_CLIP_KEY,
        title: 'Record test clip',
        description: `Emits synthetic "cat eating" detections for the attached feeder camera (a real bounding box, no cat physically required) for about ${TEST_CLIP_DURATION_MS / 1000}s, so the Events Recorder trigger -> clip pipeline can be verified end to end.`,
        type: 'button',
    },
];

class KibbleFeederPlugin extends ScryptedDeviceBase implements MixinProvider, Settings, HttpRequestHandler, CameraRegistry {
    /** Live feeder camera mixin instances, keyed by camera id, self-registered by
     * `KibbleFeederMixin` (its constructor/`release()`) since `getMixin`/`releaseMixin` here
     * don't hand this plugin the underlying camera's own `id` directly (`mixinDevice` is typed
     * narrowly as `VideoCamera & Camera`). The `clips` webhook and "Record test clip" button both
     * use this to reach "the" feeder camera without hardcoding a device id that can differ across
     * Scrypted installs -- see `types.ts`'s `CameraRegistry` doc comment. */
    private cameras = new Map<string, RegisteredCamera>();

    registerCamera(id: string, camera: RegisteredCamera): void {
        this.cameras.set(id, camera);
    }

    unregisterCamera(id: string): void {
        this.cameras.delete(id);
    }

    async getSettings(): Promise<Setting[]> {
        return SETTING_DEFS.map(def => ({ ...def, value: this.storage.getItem(def.key!) ?? DEFAULTS[def.key!] }));
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
        if (key === RECORD_TEST_CLIP_KEY) {
            const cameras = [...this.cameras.values()];
            if (cameras.length !== 1) {
                this.console.error(`kibble: "Record test clip" expected exactly one attached feeder camera, found ${cameras.length}`);
                return;
            }
            await cameras[0].recordTestClip().catch(e => this.console.error('kibble: recordTestClip failed:', e));
            return;
        }
        if (value === null || value === undefined)
            this.storage.removeItem(key);
        else
            this.storage.setItem(key, String(value));
    }

    async canMixin(type: ScryptedDeviceType | string, interfaces: string[]): Promise<string[] | undefined> {
        if (!interfaces.includes(ScryptedInterface.VideoCamera))
            return undefined;
        return [ScryptedInterface.ObjectDetector];
    }

    async getMixin(
        mixinDevice: VideoCamera & Camera, mixinDeviceInterfaces: ScryptedInterface[], mixinDeviceState: WritableDeviceState,
    ): Promise<KibbleFeederMixin> {
        return new KibbleFeederMixin(
            { mixinDevice, mixinDeviceInterfaces, mixinDeviceState, mixinProviderNativeId: this.nativeId },
            () => this.getConfig(),
            this,
        );
    }

    async releaseMixin(id: string, mixinDevice: KibbleFeederMixin): Promise<void> {
        mixinDevice.release();
    }

    /** `GET clips?start=<ms>&end=<ms>` (public, read-only): clips for the feeder camera that
     * overlap `[start, end]`, sourced from its own `VideoClips` interface --
     * `@apocaliss92/scrypted-events-recorder`'s mixin further down device 240's mixin chain
     * implements it; calling it on the composite device routes through that mixin exactly the
     * way any other interface call does. See `kibble/docs/39-eating-clips.md`. Reachable
     * locally and unauthenticated at
     * `http://<scrypted-host>:11080/endpoint/@nphil/kibble-scrypted/public/clips` (port and
     * path form confirmed live against this exact instance, 2026-09-25). */
    async onRequest(request: HttpRequest, response: HttpResponse): Promise<void> {
        const url = new URL(`http://localhost${request.url}`);
        const route = url.pathname.split('/').filter(Boolean).pop();

        if (route !== 'clips') {
            response.send(`kibble: not found: ${url.pathname}`, { code: 404 });
            return;
        }

        try {
            const cameraIds = [...this.cameras.keys()];
            if (cameraIds.length !== 1) {
                this.console.error(`kibble: clips endpoint expected exactly one camera with the Kibble mixin attached, found ${cameraIds.length}: ${JSON.stringify(cameraIds)}`);
                response.send(
                    `kibble: expected exactly one feeder camera attached, found ${cameraIds.length}`,
                    { code: 500 },
                );
                return;
            }

            const startParam = url.searchParams.get('start');
            const endParam = url.searchParams.get('end');
            const startTime = startParam === null ? undefined : Number(startParam);
            const endTime = endParam === null ? undefined : Number(endParam);
            if ((startTime !== undefined && !Number.isFinite(startTime)) || (endTime !== undefined && !Number.isFinite(endTime))) {
                response.send('kibble: start/end must be milliseconds since epoch', { code: 400 });
                return;
            }

            const camera = systemManager.getDeviceById<VideoClips>(cameraIds[0]);
            const clips = await camera.getVideoClips({ startTime, endTime });
            const body = clips.map(clip => ({
                videoId: clip.videoId,
                startTime: clip.startTime,
                endTime: clip.startTime + (clip.duration ?? 0),
                duration: clip.duration ?? 0,
                detectionClasses: clip.detectionClasses ?? [],
            }));

            response.send(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
        } catch (e) {
            this.console.error('kibble: clips endpoint failed:', e);
            response.send(`kibble: ${(e as Error).message}`, { code: 500 });
        }
    }

    private getConfig(): FeederConfig {
        const get = (key: string) => this.storage.getItem(key) ?? DEFAULTS[key];
        return {
            host: get('feederHost'),
            httpPort: Number(get('feederHttpPort')),
            rtspPort: Number(get('feederRtspPort')),
            rtspPath: get('feederRtspPath'),
            secondPassEnabled: get('secondPassEnabled') === 'true',
        };
    }

}

export default KibbleFeederPlugin;
