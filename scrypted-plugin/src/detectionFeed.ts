// Polls the agent's real detection feed (`GET /events` -- `agent/src/ai.rs`) and hands each new
// `RawDetection` to a callback.
//
// Deliberately short-polls the plain, immediate `GET /events` snapshot on a gap, rather than
// holding the agent's own `GET /events/stream?since=N` long-poll (a ~25s server-side hold) open
// continuously. A live incident against the real feeder proved why: `kibbled`'s HTTP server has
// limited concurrency, and one held long-poll connection starved Home Assistant's own polling of
// the same device, making every Kibble entity go `unavailable` for the whole house. `GET /events`
// itself is instant (an in-memory snapshot, no waiting -- `ai::Feed::snapshot_json`), so no single
// request from this feed should ever hold the server for more than a fraction of a second,
// regardless of the `AbortController` hard-abort on `stop()` below (belt and suspenders, not the
// primary fix: the primary fix is simply never asking the server to hold the line in the first
// place).

import { agentGetJson, sleep } from './agentClient';
import { RawDetection } from './types';

/** How often to re-poll `GET /events`. The agent's own detection cadence is 1s
 * (`ai::POLL_INTERVAL`), so this is already generous headroom before matching it, while staying
 * far short of anything that could look like holding a connection open. */
const POLL_GAP_MS = 5_000;
/** `GET /events` is an instant in-memory read; this is a sanity ceiling, not an expected wait. */
const REQUEST_TIMEOUT_MS = 5_000;
const RETRY_DELAY_MS = 10_000;

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
     * replay history that happened before this mixin was attached) and starts the poll loop.
     * Baseline failures are logged and treated as "start from 0" rather than fatal: the feeder
     * may simply be rebooting. */
    async start(): Promise<void> {
        try {
            const initial = await this.fetchEvents();
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

    /** Immediately aborts any in-flight request and stops re-polling. Given every request this
     * feed makes is a short, instant `GET /events` (never the long-poll stream), there should
     * rarely be anything in flight to abort -- this is a hard guarantee regardless. */
    stop(): void {
        this.stopped = true;
        this.abortController.abort();
    }

    private fetchEvents(): Promise<RawDetection[]> {
        return agentGetJson<RawDetection[]>(this.host, this.port, '/events', REQUEST_TIMEOUT_MS, this.abortController.signal);
    }

    private async loop(): Promise<void> {
        while (!this.stopped) {
            await sleep(POLL_GAP_MS);
            if (this.stopped)
                return;
            try {
                const snapshot = await this.fetchEvents();
                if (this.stopped)
                    return;
                const fresh = snapshot.filter(d => d.seq > this.since);
                if (fresh.length === 0)
                    continue;
                for (const d of fresh)
                    this.since = Math.max(this.since, d.seq);
                this.onDetections(fresh);
            } catch (e) {
                if (this.stopped)
                    return;
                this.console.warn(`kibble: GET /events error (${(e as Error).message}), retrying in ${RETRY_DELAY_MS}ms`);
                await sleep(RETRY_DELAY_MS);
            }
        }
    }
}
