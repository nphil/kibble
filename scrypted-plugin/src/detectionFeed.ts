// Long-polls the agent's real detection feed (`GET /events`, `GET /events/stream?since=N` --
// `agent/src/ai.rs`) and hands each new `RawDetection` to a callback. Deliberately dumb: the
// agent already does the debouncing/history-capping (`ai::MAX_EVENTS`, one poll thread watching
// vendor JPEG mtimes), this just keeps one long-poll connection open and retries on failure.
//
// `stop()` hard-aborts whatever request is currently in flight via `AbortController`, rather than
// only preventing the next one -- a real incident against the live feeder (its HTTP server has
// limited concurrency; one held `/events/stream` long-poll starved Home Assistant's own polling
// of the same device, making every Kibble entity go `unavailable`) is exactly why merely flipping
// a `stopped` flag between iterations is not good enough: the held connection has to actually
// close the moment a caller wants this feed gone, not whenever the agent's own ~25s hold expires.

import { agentGetJson, sleep } from './agentClient';
import { RawDetection } from './types';

const STREAM_TIMEOUT_MS = 30_000; // agent's own long-poll caps at ai::LONG_POLL_TIMEOUT (~25s)
const BASELINE_TIMEOUT_MS = 10_000;
const RETRY_DELAY_MS = 5_000;

export class KibbleDetectionFeed {
    private stopped = false;
    private since = 0;
    private abortController = new AbortController();

    constructor(
        private host: string,
        private port: number,
        private onDetections: (detections: RawDetection[]) => void,
        private console: Console,
    ) { }

    /** Establishes a baseline sequence number from `GET /events` (so a plugin restart doesn't
     * replay history that happened before this mixin was attached) and starts the long-poll
     * loop. Baseline failures are logged and treated as "start from 0" rather than fatal: the
     * feeder may simply be rebooting. */
    async start(): Promise<void> {
        try {
            const initial = await agentGetJson<RawDetection[]>(
                this.host, this.port, '/events', BASELINE_TIMEOUT_MS, this.abortController.signal,
            );
            for (const d of initial)
                this.since = Math.max(this.since, d.seq);
            this.console.log(`kibble: baseline GET /events ok, ${initial.length} historical event(s), starting from seq ${this.since}`);
        } catch (e) {
            if (this.stopped)
                return;
            this.console.warn(`kibble: baseline GET /events failed (${(e as Error).message}), starting from seq 0`);
        }
        void this.loop();
    }

    /** Immediately aborts any in-flight request (see module doc) and stops re-polling. */
    stop(): void {
        this.stopped = true;
        this.abortController.abort();
    }

    private async loop(): Promise<void> {
        while (!this.stopped) {
            try {
                const batch = await agentGetJson<RawDetection[]>(
                    this.host, this.port, `/events/stream?since=${this.since}`, STREAM_TIMEOUT_MS,
                    this.abortController.signal,
                );
                if (this.stopped)
                    return;
                if (batch.length === 0)
                    continue; // long-poll timed out with nothing new -- immediately re-poll
                for (const d of batch)
                    this.since = Math.max(this.since, d.seq);
                this.onDetections(batch);
            } catch (e) {
                if (this.stopped)
                    return;
                this.console.warn(`kibble: /events/stream error (${(e as Error).message}), retrying in ${RETRY_DELAY_MS}ms`);
                await sleep(RETRY_DELAY_MS);
            }
        }
    }
}
