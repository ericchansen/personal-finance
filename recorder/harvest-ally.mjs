#!/usr/bin/env node
/**
 * Ally Bank transaction harvester.
 *
 * Pulls every Ally account's transaction CSV in a single command, naming each
 * file by account metadata so the user never has to rename three identically-
 * named transactions.csv files by hand.
 *
 *   node harvest-ally.mjs --out <dir> [--from YYYY-MM-DD] [--to YYYY-MM-DD]
 *                         [--port 9222] [--dry-run]
 *
 * HOW IT WORKS
 *
 * Ally's download UI lets the user pick a preset date range and downloads a
 * file called "transactions.csv" with NO account identifier inside. This
 * script replaces that manual loop by:
 *
 *   1. Connecting to a live Ally session in the recorder browser via CDP.
 *   2. Capturing the `authorization` + `cif` headers by passively monitoring
 *      XHR traffic to secure.ally.com, then triggering a small fetch from the
 *      page's own JS context to kick off the first request.
 *   3. Discovering accounts via the accounts API.
 *   4. Downloading the CSV endpoint once per account with a caller-specified
 *      (or default 24-month) date range.
 *   5. Naming each file from account metadata rather than the Content-
 *      Disposition header, since every file has the same "transactions.csv"
 *      name from Ally.
 *
 * CREDENTIALS
 *
 * Tokens are never written to disk or stdout. They live in memory only.
 * Account numbers are masked to last-4 in all output.
 *
 * ALLY'S TERMS
 *
 * Ally's terms prohibit automated access. This is a productivity tool for a
 * single human account holder, not an unattended scraper. The user must log in
 * manually and re-run when the session expires.
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const DEFAULT_PORT = 9222;
const ALLY_HOST = "secure.ally.com";
// Two years of history in one pull — the API accepts any range.
const DEFAULT_MONTHS = 24;

// ─── CLI ────────────────────────────────────────────────────────────────────

function parseArgs(argv) {
  const today = new Date();
  const fromDefault = new Date(today);
  fromDefault.setMonth(fromDefault.getMonth() - DEFAULT_MONTHS);

  const args = {
    out: null,
    port: DEFAULT_PORT,
    from: formatDate(fromDefault),
    to: formatDate(today),
    dryRun: false,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const val = argv[i + 1];
    if (key === "--out")   { args.out = val; i += 1; }
    else if (key === "--port")  { args.port = Number(val); i += 1; }
    else if (key === "--from")  { args.from = val; i += 1; }
    else if (key === "--to")    { args.to = val; i += 1; }
    else if (key === "--dry-run") { args.dryRun = true; }
  }

  if (!args.out) {
    console.error(
      "usage: node harvest-ally.mjs --out <dir> [--from YYYY-MM-DD] " +
      "[--to YYYY-MM-DD] [--port 9222] [--dry-run]",
    );
    process.exit(1);
  }
  return args;
}

// ─── CDP (reused pattern from record.mjs) ───────────────────────────────────

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
      }, 30_000);
      this.socket.send(JSON.stringify(payload));
    });
  }
}

// ─── CREDENTIAL CAPTURE ─────────────────────────────────────────────────────

/**
 * Find the Ally tab in the recorder browser. We need an existing authenticated
 * page context; if there is none, we cannot proceed.
 */
async function findAllyTab(port) {
  const res = await fetch(`http://127.0.0.1:${port}/json`);
  const tabs = await res.json();
  // Prefer the most recently focused Ally page, fall back to any Ally page.
  return tabs.find(
    (t) => t.type === "page" && t.url && t.url.includes(ALLY_HOST),
  ) ?? null;
}

/**
 * Obtain the `authorization` token and `cif` customer-id that Ally's XHR calls
 * need.
 *
 * Ally's Angular SPA attaches `authorization` and `cif` via its own HTTP
 * interceptors, which only fire on Angular's HttpClient calls — not on raw
 * fetch() from the console context. So we cannot inject a trigger fetch.
 *
 * The only reliable strategy is passive interception: enable the CDP Network
 * domain and wait for the SPA to make a real authenticated API call when the
 * user clicks on an account. We navigate to the account details page to help
 * the SPA start loading data, then ask the user to click if it still doesn't
 * fire within a short window.
 *
 * We never ask the user to copy-paste anything.
 */
async function captureCredentials(cdp, tabSessionId, tabUrl, timeoutMs = 60_000) {
  // Enable the Network domain FIRST — events arrive asynchronously so the
  // listener must be active before the page makes any request.
  await cdp.send("Network.enable", {}, tabSessionId).catch(() => {});

  return new Promise((resolve, reject) => {
    let resolved = false;
    let promptedUser = false;

    const done = (creds) => {
      if (resolved) return;
      resolved = true;
      clearTimeout(timer);
      clearTimeout(promptTimer);
      resolve(creds);
    };

    const timer = setTimeout(() => {
      if (resolved) return;
      resolved = true;
      reject(new Error(
        "No authenticated Ally API call observed.\n\n" +
        "Ally's app only sends auth headers when you click on account data.\n" +
        "Please:\n" +
        "  1. Sign in at secure.ally.com in the recorder browser\n" +
        "  2. Click on any account to open its transaction list\n" +
        "  3. Re-run this command",
      ));
    }, timeoutMs);

    // After 8 seconds with no result, prompt the user to click.
    // This keeps the UX immediate when the user IS on an active page, and
    // gives them instructions when the page has loaded from cache.
    const promptTimer = setTimeout(() => {
      if (resolved) return;
      promptedUser = true;
      console.log(
        "\n  → No API call yet. Please click on an account in the browser to " +
        "trigger one.\n  Waiting up to " + Math.round((timeoutMs - 8000) / 1000) + " more seconds…",
      );
    }, 8_000);

    // Intercept any XHR to secure.ally.com that carries both auth headers.
    cdp.on((msg) => {
      if (msg.method !== "Network.requestWillBeSent") return;
      const { request } = msg.params;
      if (!request.url.includes(ALLY_HOST)) return;

      // Ally's SPA sends `authorization` and `cif` on every API call.
      const auth = request.headers["authorization"] || request.headers["Authorization"];
      const cif  = request.headers["cif"]           || request.headers["Cif"];
      if (!auth || !cif) return;

      if (promptedUser) process.stdout.write("\n");
      done({ authorization: auth, cif });
    });

    // Navigate to the account details page (if we're not already on one).
    // This often causes the SPA to fire its data-loading API calls without
    // requiring the user to click.
    const isAccountPage = tabUrl && tabUrl.includes("/account/");
    if (isAccountPage) {
      cdp.send("Page.reload", {}, tabSessionId).catch(() => {});
    } else {
      cdp.send("Page.navigate",
        { url: `https://${ALLY_HOST}/dashboard` },
        tabSessionId,
      ).catch(() => {});
    }
  });
}

// ─── ACCOUNT DISCOVERY ──────────────────────────────────────────────────────

/**
 * List Ally accounts. Returns an array of:
 *   { token, accountId, last4, name, type }
 *
 * We call the accounts endpoint; if that fails we try the transactions/search
 * endpoint which also returns account metadata.
 */
async function discoverAccounts(credentials) {
  const { authorization, cif } = credentials;
  const headers = {
    authorization,
    cif,
    Referer: `https://${ALLY_HOST}/`,
    Accept: "application/json",
  };

  // Primary: dedicated accounts endpoint.
  const acctRes = await fetch(
    `https://${ALLY_HOST}/acs/v2/customers/${cif}/accounts`,
    { headers },
  );

  if (acctRes.status === 401 || acctRes.status === 403) {
    throw new Error(
      "Ally session expired — sign in again at secure.ally.com and re-run.",
    );
  }

  if (!acctRes.ok) {
    throw new Error(`Accounts API returned ${acctRes.status} — ${await acctRes.text()}`);
  }

  const data = await acctRes.json();

  // The accounts response shape from /acs/v2/customers/{cif}/accounts
  // wraps accounts in an `accounts` array; each entry has accountNumber,
  // accountType, nickName / description, and accountNumberPvtEncrypt.
  const raw = Array.isArray(data) ? data
    : (data.accounts ?? data.account ?? data.data ?? []);

  if (!raw.length) {
    throw new Error("No accounts returned from Ally — are you signed in?");
  }

  return raw.map((acct) => ({
    // The opaque per-account token used in the CSV endpoint URL.
    token: acct.accountNumberPvtEncrypt,
    accountId: acct.accountId ?? acct.id,
    // Mask account number to last-4 — never log the full number.
    last4: String(acct.accountNumber ?? "").slice(-4),
    name: acct.nickName ?? acct.description ?? acct.accountType ?? "Account",
    type: acct.accountType ?? "Unknown",
  })).filter((a) => a.token);
}

// ─── CSV DOWNLOAD ────────────────────────────────────────────────────────────

/**
 * Download the CSV for one account. Returns the raw CSV text.
 *
 * The endpoint accepts arbitrary date ranges — unlike the UI which only offers
 * presets. This is the key finding documented in docs/runbooks/ally.md: one
 * call covers the full history.
 */
async function downloadCsv(credentials, accountToken, fromDate, toDate) {
  const { authorization, cif } = credentials;
  const url = new URL(
    `/acs/v1/bank-accounts/transactions/${accountToken}/csv`,
    `https://${ALLY_HOST}`,
  );
  url.searchParams.set("fromDate", fromDate);
  url.searchParams.set("toDate", toDate);
  url.searchParams.set("status", "Posted");

  const res = await fetch(url.toString(), {
    headers: {
      authorization,
      cif,
      Referer: `https://${ALLY_HOST}/`,
      Accept: "text/csv, */*",
    },
  });

  if (res.status === 401 || res.status === 403) {
    throw new Error(
      "Ally session expired — sign in again at secure.ally.com and re-run.",
    );
  }

  if (!res.ok) {
    throw new Error(`CSV download returned ${res.status} for account …${accountToken.slice(-4)}`);
  }

  return res.text();
}

// ─── CSV PARSING / FILENAME ──────────────────────────────────────────────────

/**
 * Parse the Ally CSV to find the actual date range present in the file.
 *
 * We derive dates from the content rather than from the requested range
 * because the API may return fewer rows than requested (e.g. account opened
 * recently, or no activity at the far end of the range).
 *
 * Ally CSV shape (note leading spaces on header fields 2+):
 *   Date, Time, Amount, Type, Description
 */
export function extractDateRange(csvText) {
  const lines = csvText.split("\n").map((l) => l.trim()).filter(Boolean);
  // Skip header row.
  const dataLines = lines.slice(1);
  if (!dataLines.length) return { firstDate: null, lastDate: null };

  const dates = dataLines
    .map((line) => {
      // Date is the first field, unquoted, in M/D/YYYY format.
      const dateStr = line.split(",")[0]?.trim();
      if (!dateStr) return null;
      const parsed = new Date(dateStr);
      return isNaN(parsed.getTime()) ? null : parsed;
    })
    .filter(Boolean)
    .sort((a, b) => a - b);

  if (!dates.length) return { firstDate: null, lastDate: null };

  return {
    firstDate: formatDate(dates[0]),
    lastDate: formatDate(dates[dates.length - 1]),
  };
}

/**
 * Build the output filename following the repo convention:
 *   DocumentType - Institution AccountName (last4) - firstDate to lastDate.csv
 *
 * Example:
 *   Transactions - Ally Checking (1234) - 2024-01-05 to 2025-08-26.csv
 */
export function buildFilename(account, firstDate, lastDate) {
  // Sanitize account name: strip characters that are invalid in filenames.
  const safeName = account.name.replace(/[<>:"/\\|?*]/g, "").trim();
  const dateRange = (firstDate && lastDate)
    ? `${firstDate} to ${lastDate}`
    : "no-transactions";
  return `Transactions - Ally ${safeName} (${account.last4}) - ${dateRange}.csv`;
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

function formatDate(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function printSummary(results) {
  console.log("\n┌─────────────────────────────────────────────────────┐");
  console.log("│  Harvest summary                                    │");
  console.log("├─────────────────────────────────────────────────────┤");
  for (const r of results) {
    const status = r.error ? `FAILED: ${r.error}` : r.filename;
    console.log(`│  …${r.last4}  ${r.name.padEnd(16)}  ${status}`);
  }
  console.log("└─────────────────────────────────────────────────────┘");
}

// ─── MAIN ────────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);

  // Verify the recorder browser is up.
  const base = `http://127.0.0.1:${args.port}`;
  let version;
  try {
    version = await (await fetch(`${base}/json/version`)).json();
  } catch {
    console.error(
      `No browser on ${base}.\n` +
      `Start the recorder browser first:\n  node recorder/launch.mjs`,
    );
    process.exit(1);
  }

  console.log(`Connected to ${version.Browser}`);

  // Find a live Ally tab.
  const tab = await findAllyTab(args.port);
  if (!tab) {
    console.error(
      `No Ally tab found in the recorder browser.\n` +
      `Open ${ALLY_HOST} in the recorder browser and sign in, then re-run.`,
    );
    process.exit(1);
  }

  console.log(`Found Ally tab: ${tab.url}`);

  // Open a CDP session to that specific tab.
  const cdp = new Cdp(tab.webSocketDebuggerUrl);
  await cdp.ready;

  // Attach to the tab and enable the Network domain.
  const { sessionId: tabSessionId } = await cdp.send("Target.attachToTarget", {
    targetId: tab.id,
    flatten: true,
  });

  console.log("Capturing credentials from live session…");
  console.log(
    "(If this hangs, navigate to any Ally account page to trigger an API call.)",
  );

  let credentials;
  try {
    credentials = await captureCredentials(cdp, tabSessionId, tab.url);
  } catch (e) {
    console.error(`\n${e.message}`);
    process.exit(1);
  }

  console.log("Session credentials captured. Discovering accounts…");

  let accounts;
  try {
    accounts = await discoverAccounts(credentials);
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }

  console.log(`Found ${accounts.length} account(s):`);
  for (const a of accounts) {
    console.log(`  ${a.type.padEnd(12)} …${a.last4}  ${a.name}`);
  }

  if (args.dryRun) {
    console.log("\n--dry-run: no files written.");
    process.exit(0);
  }

  fs.mkdirSync(args.out, { recursive: true });

  const results = [];

  for (const account of accounts) {
    process.stdout.write(`  Downloading …${account.last4} ${account.name}… `);
    try {
      const csv = await downloadCsv(credentials, account.token, args.from, args.to);
      const { firstDate, lastDate } = extractDateRange(csv);
      const filename = buildFilename(account, firstDate, lastDate);
      const outPath = path.join(args.out, filename);
      fs.writeFileSync(outPath, csv, "utf8");
      const lines = csv.split("\n").filter((l) => l.trim()).length - 1; // minus header
      process.stdout.write(`✓ ${lines} rows → ${filename}\n`);
      results.push({ ...account, filename, error: null });
    } catch (e) {
      process.stdout.write(`✗ ${e.message}\n`);
      results.push({ ...account, filename: null, error: e.message });
    }
  }

  printSummary(results);

  const failed = results.filter((r) => r.error);
  if (failed.length) {
    console.error(`\n${failed.length} account(s) failed.`);
    process.exit(1);
  }
}

// Only run main() when executed directly (not when imported by tests).
// In ESM, `import.meta.url` is the file:// URL of THIS module; process.argv[1]
// is the path to the entry-point file. We compare normalised forms.
const isMain = process.argv[1] &&
  import.meta.url === new URL(`file:///${process.argv[1].replace(/\\/g, "/")}`).href;

if (isMain) {
  main().catch((e) => {
    console.error("harvest-ally failed:", e.message);
    process.exit(1);
  });
}
