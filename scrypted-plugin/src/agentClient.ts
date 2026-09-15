// Minimal HTTP client for the Kibble agent's plain, unauthenticated LAN HTTP API
// (`agent/src/main.rs`, `agent/src/http.rs`). No dependency beyond Node's own `http` module --
// the agent is a tiny, from-scratch server and doesn't need anything fancier on this side either.

import * as http from 'http';

/** GETs `path` and parses the body as JSON. Long `timeoutMs` values are expected: `GET
 * /events/stream?since=N` is a deliberate ~25s long-poll (`ai::LONG_POLL_TIMEOUT`). `signal`
 * lets a caller hard-abort a held long-poll immediately (e.g. on mixin release) instead of
 * merely not re-issuing the next one -- a real incident against the live feeder (its HTTP server
 * has limited concurrency and a held long-poll starved Home Assistant's own polling of it) is
 * exactly why this is not optional. */
export function agentGetJson<T>(host: string, port: number, path: string, timeoutMs: number, signal?: AbortSignal): Promise<T> {
    return agentGetBuffer(host, port, path, timeoutMs, signal).then(buf => JSON.parse(buf.toString('utf8')) as T);
}

/** GETs `path` and returns the raw response body (used for JPEG crops). */
export function agentGetBuffer(host: string, port: number, path: string, timeoutMs: number, signal?: AbortSignal): Promise<Buffer> {
    const { promise, resolve, reject } = Promise.withResolvers<Buffer>();
    const req = http.get({ host, port, path, timeout: timeoutMs, signal }, res => {
        const chunks: Buffer[] = [];
        const status = res.statusCode ?? 0;
        if (status < 200 || status >= 300) {
            res.resume();
            reject(new Error(`GET ${path}: HTTP ${status}`));
            return;
        }
        res.on('data', (c: Buffer) => chunks.push(c));
        res.on('end', () => resolve(Buffer.concat(chunks)));
        res.on('error', reject);
    });
    req.on('timeout', () => req.destroy(new Error(`GET ${path}: timed out after ${timeoutMs}ms`)));
    req.on('error', reject);
    return promise;
}

export function sleep(ms: number): Promise<void> {
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, ms);
    return promise;
}
