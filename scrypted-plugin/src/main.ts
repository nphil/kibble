// Plugin entry point: a MixinProvider that attaches ObjectDetector + Intercom to the feeder
// camera (see mixin.ts), plus the plugin-wide Settings (feeder host/ports, second-pass toggle,
// and a live intercom negotiation self-test -- see README for how to read its results).
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
    Camera, MixinProvider, ScryptedDeviceType,
    Setting, Settings, SettingValue, VideoCamera, WritableDeviceState,
} from '@scrypted/sdk';
import { ScryptedDeviceBase, ScryptedInterface } from '@scrypted/sdk';
import { sleep } from './agentClient';
import { KibbleFeederMixin } from './mixin';
import { RtspBackchannelClient } from './rtspBackchannel';
import './sdkFix';
import { FeederConfig } from './types';

const SELF_TEST_SILENCE_FRAME_COUNT = 25; // ~500ms of 20ms PCMU frames
const SELF_TEST_SILENCE_BYTE = 0xff; // G.711 mu-law's "silence" code

const DEFAULTS: Record<string, string> = {
    feederHost: '192.168.4.85',
    feederHttpPort: '8765',
    feederRtspPort: '8554',
    feederRtspPath: 'sub',
    secondPassEnabled: 'true',
};

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
        title: 'Intercom RTSP mount',
        description: 'Mount ("sub" or "main") the intercom opens its own short-lived RTSP ' +
            'session on to negotiate the ONVIF backchannel. This is a second, transient ' +
            'session for the duration of one call only -- see README for what that costs ' +
            'against the "one video consumer" rule.',
        type: 'string',
        choices: ['sub', 'main'],
    },
    {
        key: 'secondPassEnabled',
        title: 'Second-pass detection',
        description: 'Re-check on-device "face" detections against an installed ONNX/OpenVINO ' +
            'ObjectDetection plugin for a real, off-device class + confidence score.',
        type: 'boolean',
    },
    {
        key: 'testIntercom',
        title: 'Test intercom negotiation',
        description: 'Runs the real DESCRIBE/SETUP/PLAY/TEARDOWN exchange against the feeder, ' +
            'including the documented UDP\u2192461 fallback check, and writes a transcript ' +
            'below. Sends only silence -- harmless even once audio is unblocked.',
        type: 'button',
        console: true,
    },
    {
        key: 'lastIntercomTest',
        title: 'Last intercom test result',
        type: 'textarea',
        readonly: true,
    },
];

class KibbleFeederPlugin extends ScryptedDeviceBase implements MixinProvider, Settings {
    async getSettings(): Promise<Setting[]> {
        return SETTING_DEFS.map(def => ({ ...def, value: this.storage.getItem(def.key!) ?? DEFAULTS[def.key!] }));
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
        if (key === 'testIntercom') {
            this.runIntercomSelfTest()
                .then(summary => this.storage.setItem('lastIntercomTest', summary))
                .catch((e: Error) => this.storage.setItem('lastIntercomTest', `FAILED: ${e.message}`));
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
        return [ScryptedInterface.ObjectDetector, ScryptedInterface.Intercom];
    }

    async getMixin(
        mixinDevice: VideoCamera & Camera, mixinDeviceInterfaces: ScryptedInterface[], mixinDeviceState: WritableDeviceState,
    ): Promise<KibbleFeederMixin> {
        return new KibbleFeederMixin(
            { mixinDevice, mixinDeviceInterfaces, mixinDeviceState, mixinProviderNativeId: this.nativeId },
            () => this.getConfig(),
        );
    }

    async releaseMixin(id: string, mixinDevice: KibbleFeederMixin): Promise<void> {
        mixinDevice.release();
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

    /** Drives the exact same `RtspBackchannelClient` the mixin's `startIntercom` uses, standalone
     * (no camera media required), so "prove the RTSP exchange" has a one-click, repeatable check
     * independent of a live HomeKit/WebRTC call. Every line is also written to the plugin's own
     * console (the `console: true` flag on the button setting opens it automatically). */
    private async runIntercomSelfTest(): Promise<string> {
        const config = this.getConfig();
        const lines: string[] = [];
        const log = (line: string) => {
            lines.push(line);
            this.console.log(`kibble self-test: ${line}`);
        };

        log(`connecting to rtsp://${config.host}:${config.rtspPort}/${config.rtspPath}`);
        const client = new RtspBackchannelClient(config.host, config.rtspPort, config.rtspPath, this.console);
        try {
            await client.connect();
            log('TCP connect ok');

            const options = await client.options();
            log(`OPTIONS -> ${options.code} ${options.reason}`);

            const { response: describe, offered } = await client.describeWithBackchannel();
            log(`DESCRIBE (Require: backchannel) -> ${describe.code} ${describe.reason}; backchannel offered in SDP = ${offered}`);
            if (!offered)
                throw new Error('feeder did not offer a backchannel section');

            const udpSetup = await client.setupBackchannel('udp');
            log(`SETUP trackID=2 (UDP) -> ${udpSetup.code} ${udpSetup.reason} (expect 461 Unsupported Transport)`);
            if (udpSetup.code !== 461)
                throw new Error(`expected 461 for a UDP SETUP, got ${udpSetup.code}`);

            const tcpSetup = await client.setupBackchannel('tcp');
            log(`SETUP trackID=2 (TCP) -> ${tcpSetup.code} ${tcpSetup.reason}; Transport: ${tcpSetup.headers['transport']}`);
            if (tcpSetup.code !== 200)
                throw new Error(`TCP SETUP failed: ${tcpSetup.code}`);

            const play = await client.play();
            log(`PLAY -> ${play.code} ${play.reason}`);
            if (play.code !== 200)
                throw new Error(`PLAY failed: ${play.code}`);

            await client.waitForActivity(1_500);
            const silence = Buffer.alloc(160, SELF_TEST_SILENCE_BYTE);
            for (let i = 0; i < SELF_TEST_SILENCE_FRAME_COUNT; i++) {
                client.sendPcmuFrame(silence);
                await sleep(20);
            }
            const stats = client.stats;
            log(
                `sent ${stats.audioFramesSent} PCMU frames on the backchannel; received ` +
                `${stats.videoFramesReceived} video + ${stats.micFramesReceived} mic frames while ` +
                'playing (proves the session is genuinely live, not just a 200 OK)',
            );

            const teardown = await client.teardown();
            log(`TEARDOWN -> ${teardown?.code} ${teardown?.reason}`);

            log(
                'RESULT: PASS -- ONVIF backchannel negotiation and RTP-over-TCP transport are ' +
                'real and verified live. Audible playback on the feeder itself is separately ' +
                'gated on a vendor start-signal (see README) -- this test cannot and does not ' +
                'prove sound was heard.',
            );
        } catch (e) {
            log(`RESULT: FAIL -- ${(e as Error).message}`);
            throw e;
        } finally {
            client.close();
        }
        return lines.join('\n');
    }
}

export default KibbleFeederPlugin;
