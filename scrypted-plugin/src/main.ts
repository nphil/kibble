// Plugin entry point: a MixinProvider that attaches ObjectDetector to the feeder camera (see
// mixin.ts), plus the plugin-wide Settings (feeder host/ports, second-pass toggle).
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
    Camera, MixinProvider, ScryptedDeviceType,
    Setting, Settings, SettingValue, VideoCamera, WritableDeviceState,
} from '@scrypted/sdk';
import { ScryptedDeviceBase, ScryptedInterface } from '@scrypted/sdk';
import { KibbleFeederMixin } from './mixin';
import './sdkFix';
import { FeederConfig } from './types';


const DEFAULTS: Record<string, string> = {
    feederHost: '192.168.1.85', // moved networks once already -- this is why it's a setting, not a constant
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
];

class KibbleFeederPlugin extends ScryptedDeviceBase implements MixinProvider, Settings {
    async getSettings(): Promise<Setting[]> {
        return SETTING_DEFS.map(def => ({ ...def, value: this.storage.getItem(def.key!) ?? DEFAULTS[def.key!] }));
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
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

}

export default KibbleFeederPlugin;
