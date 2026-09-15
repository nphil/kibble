// Adopt the Kibble feeder into Scrypted as an RTSP camera and enable the same mixins Nitin's
// other cameras use (Rebroadcast/prebuffer, Snapshot, HomeKit as a standalone accessory).
//
// Run on the Scrypted host; credentials come from the environment, never from this file:
//   SCRYPTED_USER=… SCRYPTED_PASS=… docker run --rm --network host -e SCRYPTED_USER -e SCRYPTED_PASS \
//     -v "$PWD":/w -w /w node:20-slim sh -c 'npm i -s @scrypted/client >/dev/null && node scrypted-adopt.mjs'
//
// Idempotent: reuses an existing device of the same name.

import { connectScryptedClient } from "@scrypted/client";

process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

const NAME = process.env.KIBBLE_CAMERA_NAME || "Cat Feeder Camera";
const URL = process.env.KIBBLE_RTSP_URL || "rtsp://192.168.4.85:8554/sub";
const WANT_MIXINS = ["Rebroadcast Plugin", "Snapshot Plugin", "HomeKit"];

const sdk = await connectScryptedClient({
  baseUrl: process.env.SCRYPTED_URL || "https://127.0.0.1:10443",
  pluginId: "@scrypted/core",
  username: process.env.SCRYPTED_USER,
  password: process.env.SCRYPTED_PASS,
});
const sm = sdk.systemManager;

const byName = (name) => {
  for (const id of Object.keys(sm.getSystemState())) {
    const d = sm.getDeviceById(id);
    if (d?.name === name) return d;
  }
};

let cam = byName(NAME);
if (!cam) {
  const creator = byName("RTSP Camera Plugin");
  if (!creator) throw new Error("RTSP Camera Plugin is not installed");
  const id = await creator.createDevice({ name: NAME });
  cam = sm.getDeviceById(id);
  console.log(`created ${NAME} as device ${id}`);
} else {
  console.log(`reusing ${NAME} (device ${cam.id})`);
}

// Only one stream exists and it carries no audio yet (the ring's audio codec is unresolved).
await cam.putSetting("urls", JSON.stringify([URL]));
await cam.putSetting("noAudio", "true");

// Enable the same mixins the other cameras have. Mixin ids are what setMixins wants.
const have = new Set(cam.mixins || []);
for (const name of WANT_MIXINS) {
  const m = byName(name);
  if (!m) { console.log(`skip: ${name} not installed`); continue; }
  have.add(m.id);
}
await cam.setMixins([...have]);

// HomeKit: standalone accessory, as Nitin's other cameras are configured.
await new Promise((r) => setTimeout(r, 3000)); // let mixins attach before touching their settings
try { await cam.putSetting("homekit:standalone", "true"); } catch (e) { console.log("homekit:standalone not settable yet:", e.message); }

const settings = await cam.getSettings();
const show = (k) => settings.find((s) => s.key === k)?.value;
console.log("urls          =", show("urls"));
console.log("noAudio       =", show("noAudio"));
console.log("detectedCodec =", show("prebuffer:detectedCodec"));
console.log("detectedRes   =", show("prebuffer:detectedResolution"));
console.log("homekit qr    =", show("homekit:qrCode") ? "present" : "not yet");
console.log("homekit pin   =", show("homekit:pincode") || "not yet");
console.log("mixins        =", (cam.mixins || []).map((id) => sm.getDeviceById(id)?.name).join(" | "));
process.exit(0);
