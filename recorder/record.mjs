#!/usr/bin/env node
/**
 * Passive CDP network recorder.
 *
 * Watches a real browser session and records everything it does, so a manual
 * export can be studied and eventually replayed without a human.
 *
 *   node record.mjs --out <dir> [--minutes 20] [--port 9222]
 *
 * Passive by design. It never clicks, types or navigates: the human drives,
 * the recorder observes. Banks run bot detection that automation trips, and
 * driving someone's authenticated banking session unattended is not something
 * to do casually.
 *
 * CAPTURES EVERYTHING. No request is dropped and no body is skipped by default.
 * Filtering while recording risks losing the one call that matters — an export
 * served from an unexpected CDN host, or returned with a surprising content
 * type, would vanish with no way to get it back. Requests are *classified*
 * instead (noise, export candidate) so analysis can narrow afterwards. A
 * superset can always be filtered later; a missed request cannot be un-missed.
 *
 * SECRETS: captures contain live cookies and bearer tokens. Everything is
 * redacted before it touches disk, and output belongs outside the repository.
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import {
  isNoise,
  looksLikeExport,
  redactBody,
  redactHeaders,
  redactUrl,
} from "./redact.mjs";

const DEFAULT_PORT = 9222;
const MEDIA = /^(image|font|audio|video)\//i;

function parseArgs(argv) {
  const args = {
    port: DEFAULT_PORT,
    minutes: 20,
    out: null,
    // Generous: a full CSV export should never be truncated. Only a guard
    // against a single pathological response eating all memory.
    maxBody: 25_000_000,
    skipMedia: false,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--out") { args.out = value; i += 1; }
    else if (key === "--minutes") { args.minutes = Number(value); i += 1; }
    else if (key === "--port") { args.port = Number(value); i += 1; }
    else if (key === "--max-body") { args.maxBody = Number(value); i += 1; }
    else if (key === "--skip-media") { args.skipMedia = true; }
  }
  if (!args.out) {
    console.error(
      "usage: node record.mjs --out <dir> [--minutes 20] [--port 9222] [--skip-media]",
    );
    process.exit(1);
  }
  return args;
}

class Cdp {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = [];
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", () => resolve());
      this.socket.addEventListener("error", (e) =>
        reject(new Error(String(e.message || e.type))));
    });
    this.socket.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== undefined) {
        const entry = this.pending.get(msg.id);
        if (entry) {
          this.pending.delete(msg.id);
          msg.error ? entry.reject(new Error(msg.error.message)) : entry.resolve(msg.result);
        }
        return;
      }
      for (const fn of this.listeners) fn(msg);
    });
  }

  on(fn) { this.listeners.push(fn); }

  send(method, params = {}, sessionId) {
    const id = ++this.nextId;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`${method} timed out`));
      }, 20000);
      this.socket.send(JSON.stringify(payload));
    });
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const base = `http://127.0.0.1:${args.port}`;

  let version;
  try {
    version = await (await fetch(`${base}/json/version`)).json();
  } catch {
    console.error(`No browser on ${base}. Start one with launch.mjs first.`);
    process.exit(1);
  }
  console.log(`connected to ${version.Browser}`);

  const cdp = new Cdp(version.webSocketDebuggerUrl);
  await cdp.ready;

  fs.mkdirSync(args.out, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const streamPath = path.join(args.out, `recording-${stamp}.jsonl`);
  const summaryPath = path.join(args.out, `recording-${stamp}.json`);
  const stopPath = path.join(args.out, "STOP");

  // Append each request the moment it completes. Buffering the whole session
  // in memory and writing at exit lost a 1,677-request capture to a hard kill:
  // on Windows a signal is not delivered, the process is simply terminated, so
  // an exit handler is not a durable place to put the only write.
  const stream = fs.createWriteStream(streamPath, { flags: "a" });

  const requests = new Map();
  let written = 0;
  let bodies = 0;
  let pendingBodies = 0;

  const flush = (entry) => {
    stream.write(`${JSON.stringify(entry)}\n`);
    written += 1;
    if (entry.responseBody !== undefined) bodies += 1;
    process.stdout.write(`\r  ${written} requests written, ${bodies} bodies   `);
  };

  cdp.on(async (msg) => {
    const { method, params, sessionId } = msg;

    // Attach to every target, including tabs and workers created later, so a
    // download that pops a new window is still recorded.
    if (method === "Target.attachedToTarget") {
      const sid = params.sessionId;
      try {
        await cdp.send("Network.enable", { maxResourceBufferSize: 100_000_000 }, sid);
        await cdp.send("Page.enable", {}, sid);
        await cdp.send("Runtime.runIfWaitingForDebugger", {}, sid);
      } catch { /* target may already be gone */ }
      return;
    }

    if (method === "Network.requestWillBeSent") {
      const { request, requestId, timestamp, type, documentURL, initiator } = params;
      requests.set(requestId, {
        sessionId,
        url: redactUrl(request.url),
        method: request.method,
        resourceType: type,
        documentUrl: redactUrl(documentURL || ""),
        initiator: initiator?.type,
        timestamp,
        requestHeaders: redactHeaders(request.headers),
        postData: redactBody(request.postData),
        // Classification, never a filter. Analysis narrows using these.
        noise: isNoise(request.url),
      });
      return;
    }

    if (method === "Network.loadingFailed") {
      const entry = requests.get(params.requestId);
      if (entry) {
        entry.failed = params.errorText;
        requests.delete(params.requestId);
        flush(entry);
      }
      return;
    }

    if (method === "Network.responseReceived") {
      const entry = requests.get(params.requestId);
      if (!entry) return;
      const { response } = params;
      entry.status = response.status;
      entry.mimeType = response.mimeType;
      entry.responseHeaders = redactHeaders(response.headers);
      entry.remoteAddress = response.remoteIPAddress;
      entry.exportCandidate = looksLikeExport(entry.url, response.mimeType, response.headers);

      if (args.skipMedia && MEDIA.test(response.mimeType || "")) {
        entry.bodySkipped = "media";
        requests.delete(params.requestId);
        flush(entry);
        return;
      }

      // Bodies are only retrievable for a short window after the response, so
      // fetch immediately rather than at the end of the session.
      pendingBodies += 1;
      setTimeout(async () => {
        try {
          const body = await cdp.send(
            "Network.getResponseBody",
            { requestId: params.requestId },
            entry.sessionId,
          );
          let text = body.base64Encoded
            ? Buffer.from(body.body, "base64").toString("utf8")
            : body.body;
          entry.bodyBytes = text ? text.length : 0;
          entry.base64Encoded = Boolean(body.base64Encoded);
          if (text && text.length > args.maxBody) {
            text = `${text.slice(0, args.maxBody)}\n...[truncated ${text.length - args.maxBody} chars]`;
            entry.bodyTruncated = true;
          }
          entry.responseBody = redactBody(text);
        } catch {
          // Evicted from the buffer, or a target that closed mid-flight.
          entry.bodySkipped = "unavailable";
        }
        pendingBodies -= 1;
        requests.delete(params.requestId);
        flush(entry);
      }, 250);
    }
  });

  await cdp.send("Target.setDiscoverTargets", { discover: true });
  await cdp.send("Target.setAutoAttach", {
    autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
  });

  console.log(`streaming to ${streamPath}`);
  console.log(`stop with:  New-Item "${stopPath}"   (or Ctrl+C)\n`);

  let stopped = false;
  const stop = async () => {
    if (stopped) return;
    stopped = true;

    // Let in-flight body fetches land rather than losing them.
    for (let i = 0; i < 20 && pendingBodies > 0; i += 1) {
      await new Promise((r) => setTimeout(r, 250));
    }
    await new Promise((r) => stream.end(r));

    // The stream is the source of truth; the summary is a convenience built
    // from it, so a crash costs at most the summary.
    const all = fs
      .readFileSync(streamPath, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
    const candidates = all.filter((r) => r.exportCandidate && !r.noise);
    const hosts = [...new Set(all.map((r) => {
      try { return new URL(r.url).host; } catch { return "(unparsed)"; }
    }))].sort();

    fs.writeFileSync(summaryPath, JSON.stringify({
      recordedAt: new Date().toISOString(),
      requestCount: all.length,
      bodiesCaptured: all.filter((r) => r.responseBody !== undefined).length,
      exportCandidates: candidates.length,
      hosts,
      requests: all,
    }, null, 2));

    try { fs.unlinkSync(stopPath); } catch { /* not present */ }
    console.log(`\n\n${all.length} requests across ${hosts.length} hosts`);
    console.log(`${candidates.length} export candidates`);
    console.log(`wrote ${summaryPath}`);
    process.exit(0);
  };

  // A sentinel file is the reliable stop on Windows, where signals are not
  // delivered to another process and a kill is always abrupt.
  const watcher = setInterval(() => {
    if (fs.existsSync(stopPath)) stop();
  }, 1000);
  watcher.unref?.();

  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  setTimeout(stop, args.minutes * 60 * 1000);
}

main().catch((e) => {
  console.error("recorder failed:", e.message);
  process.exit(1);
});
