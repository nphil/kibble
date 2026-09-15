// Workaround for a confirmed runtime gap in this exact @scrypted/sdk@0.5.59 + Scrypted server
// combination, live-diagnosed against the actual deployed plugin (not guessed):
//
//   - The SDK's own `dist/src/index.js` is supposed to self-populate `sdk.systemManager` /
//     `sdk.deviceManager` / `sdk.mediaManager` / etc. at module load via
//     `__non_webpack_require__(process.env.SCRYPTED_SDK_MODULE).getScryptedStatic()` (visible
//     directly in that file's source). `typeof __non_webpack_require__` really is `'function'`
//     here and `process.env.SCRYPTED_SDK_MODULE` really is a valid path to the host's own
//     `plugin-remote-worker.js` -- calling that exact expression from this plugin's own code
//     returns a fully populated object (`systemManager`, `deviceManager`, `mediaManager`, ...).
//   - Despite that, `import sdk from '@scrypted/sdk'` (the ES-interop `.default` binding) reads
//     as `undefined` everywhere in this bundle -- built with `concatenateModules: false` (see
//     webpack.nodejs.config.js, needed to work around a *different* bundling bug). The module's
//     own **named** `.sdk` export, fetched via a plain `require('@scrypted/sdk').sdk`, is the
//     real, live object `ScryptedDeviceBase`/`MixinDeviceBase`'s internals themselves mutate and
//     read (they reference `exports.sdk`, never `exports.default`, in the SDK's own source) --
//     confirmed by this exact fix turning a hard crash (`getSettings()` throwing "Cannot read
//     properties of undefined (reading 'deviceManager')") into working `Settings`/`ObjectDetector`
//     calls against the live, deployed plugin.
//
// Every file in this plugin that needs `systemManager`/`mediaManager`/`deviceManager` MUST import
// `sdk` from *this* module, not from `@scrypted/sdk` directly -- the ES-interop `.default` import
// is the broken one.
import type { ScryptedStatic } from '@scrypted/sdk';

interface HostSdkModule {
    getScryptedStatic?: () => Record<string, unknown>;
}

declare const __non_webpack_require__: ((id: string) => HostSdkModule) | undefined;

export const sdk: ScryptedStatic = (require('@scrypted/sdk') as { sdk: ScryptedStatic }).sdk;

function ensureSdkStaticsLoaded(): void {
    if (sdk.deviceManager)
        return;
    const modulePath = process.env.SCRYPTED_SDK_CJS_MODULE || process.env.SCRYPTED_SDK_MODULE;
    if (!modulePath || typeof __non_webpack_require__ === 'undefined') {
        console.error('kibble: cannot self-heal @scrypted/sdk statics: no SCRYPTED_SDK_MODULE/__non_webpack_require__ available');
        return;
    }
    const statics = __non_webpack_require__(modulePath).getScryptedStatic?.();
    if (!statics?.deviceManager) {
        console.error('kibble: @scrypted/sdk self-heal found no deviceManager on getScryptedStatic() result');
        return;
    }
    Object.assign(sdk, statics);
    console.log('kibble: self-healed @scrypted/sdk statics (systemManager/deviceManager/mediaManager/...)');
}

ensureSdkStaticsLoaded();
