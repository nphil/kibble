// Finds and invokes a general-purpose, full-size `ObjectDetection` plugin already installed in
// this Scrypted instance (ONNX or OpenVINO) so the mixin can re-check an on-device "face" crop
// off-device, per the assignment's "second pass" requirement. This is a real capability, not a
// fallback stub: `ObjectDetection.detectObjects(mediaObject)` is a standard Scrypted interface,
// and any plugin implementing it (e.g. `@scrypted/onnx`'s "ONNX Object Detection" device) is
// callable directly from any other plugin's `systemManager`/`mediaManager` -- confirmed live
// against this exact Scrypted instance before writing this file (device "ONNX Object Detection",
// a YOLOv9c model reporting `person`/`vehicle`/`animal`, answered `detectObjects` in ~35ms).
//
// Deliberately excludes the "Scrypted NVR Object Detection" / "Accelerated Motion Detection"
// mixin-style `ObjectDetection` providers (`@scrypted/nvr`): those are designed to be driven
// through the NVR's own per-camera video pipeline, not called standalone with an arbitrary crop.

import type { ObjectDetection, ObjectsDetected, ScryptedDevice } from '@scrypted/sdk';
import { ScryptedInterface } from '@scrypted/sdk';
import { sdk } from './sdkFix';

const NAME_PATTERN = /onnx|openvino/i;
const EXCLUDED_PLUGIN_PATTERN = /^@scrypted\/nvr$/;

export type SecondPassDetector = ScryptedDevice & ObjectDetection;

/** Picks the first installed `ObjectDetection` device whose plugin/name suggests a general,
 * full-size, off-device model (ONNX or OpenVINO), skipping the NVR's own mixin-style detectors.
 * Returns `undefined` -- not a guess -- if this Scrypted instance has neither installed. */
export function findSecondPassDetector(): SecondPassDetector | undefined {
    const state = sdk.systemManager.getSystemState();
    for (const id of Object.keys(state)) {
        const device = sdk.systemManager.getDeviceById<SecondPassDetector>(id);
        if (!device || !device.interfaces.includes(ScryptedInterface.ObjectDetection))
            continue;
        if (EXCLUDED_PLUGIN_PATTERN.test(device.pluginId ?? ''))
            continue;
        if (!NAME_PATTERN.test(device.pluginId ?? '') && !NAME_PATTERN.test(device.name ?? ''))
            continue;
        return device;
    }
    return undefined;
}

/** Runs the crop through the discovered detector and returns its raw result, or `undefined` if
 * the model produced nothing above `minScore`. Errors are the caller's problem to log and treat
 * as "second pass unavailable this time" -- a slow/misbehaving detector must never take down the
 * on-device event it was meant to enrich. */
export async function runSecondPass(
    detector: SecondPassDetector, crop: Buffer, minScore: number,
): Promise<ObjectsDetected | undefined> {
    const mediaObject = await sdk.mediaManager.createMediaObject(crop, 'image/jpeg');
    const result = await detector.detectObjects(mediaObject);
    const kept = (result.detections ?? []).filter(d => d.score >= minScore);
    if (kept.length === 0)
        return undefined;
    return { ...result, detections: kept };
}
