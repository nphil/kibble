// Adopt the Kibble feeder into Scrypted as an RTSP camera, then hand it to HomeKit the same way
// Nitin's other cameras are set up.
//
// Run it on the Scrypted host (it talks to loopback):
//   docker run --rm --network host -v "$PWD":/w -w /w node:20-slim sh -c \
//     'npm i -s @scrypted/client && node scrypted-adopt.mjs rtsp://192.168.4.85:8554/sub'
//
// Idempotent: if a device named FEEDER_NAME already exists it is reconfigured rather than
// duplicated. Nothing here is Kibble-specific beyond the name and the URL.

import { connectScryptedClient } from "@scrypted/client";

process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

const FEEDER_NAME = "Cat Feeder Camera";
const RTSP_PLUGIN = "@scrypted/rtsp";
const url = process.argv[2] || "rtsp://192.168.4.85:8554/sub";

const sdk = await connectScryptedClient({
  baseUrl: process.env.SCRYPTED_URL || "https://127.0.0.1:10443",
  pluginId: "@scrypted/core",
  username: process.env.SCRYPTED_USER,
  password: process.env.SCRYPTED_PASS,
});
const sm = sdk.systemManager;

function findByName(name) {
  for (const id of Object.keys(sm.getSystemState())) {
    const d = sm.getDeviceById(id);
    if (d?.name === name) return d;
  }
}

let camera = findByName(FEEDER_NAME);
if (!camera) {
  // The generic RTSP plugin is a DeviceCreator: ask it to mint a camera, then configure it.
  const creator = findByName("RTSP Camera Plugin");
  if (!creator) throw new Error("RTSP Camera Plugin not installed");
  const id = await creator.createDevice({ name: FEEDER_NAME });
  console.log(`created device ${id}`);
  camera = sm.getDeviceById(id);
} else {
  console.log(`reusing existing device ${camera.id}`);
}

// The feeder's agent serves one continuous H.264 substream; there is no second stream to pick.
await camera.putSetting("urls", JSON.stringify([url]));
await camera.putSetting("noAudio", "true"); // audio codec in the frame ring is not yet identified

// Prebuffer over TCP is what keeps latency low and keyframes available for HomeKit.
const rebroadcast = findByName("Rebroadcast Plugin");
if (rebroadcast) {
  await sdk.deviceManager.requestRestart?.().catch(() => {});
}

console.log("configured:", FEEDER_NAME, "->", url);
console.log("Next, in the Scrypted UI (or via mixins): enable Rebroadcast, Snapshot and HomeKit,");
console.log("and set HomeKit to 'standalone accessory' as the other cameras are.");
process.exit(0);
