#!/usr/bin/env node
/**
 * Launch Edge with a debug port, without tripping bot detection.
 *
 *   node launch.mjs [--port 9222] [--profile <dir>] [--url https://...]
 *
 * Two things make this work where Playwright and Puppeteer get blocked:
 *
 * 1. No automation flags. Only --remote-debugging-port is passed, so
 *    navigator.webdriver stays false and no "controlled by automated test
 *    software" banner appears. Bank WAFs (Akamai, PerimeterX) check for those.
 *
 * 2. A dedicated user-data-dir. Edge and Chrome 136+ silently ignore the debug
 *    port on the default profile as an anti-cookie-theft measure, so a distinct
 *    profile is required rather than optional. It also keeps the capture
 *    isolated from the user's everyday browsing.
 *
 * The profile persists between runs, so a bank's "trusted device" cookie
 * survives and MFA is not re-challenged every session.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const EDGE_CANDIDATES = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

function parseArgs(argv) {
  const args = {
    port: 9222,
    profile: path.join(os.homedir(), ".copilot", "browser-profiles", "finance-recorder"),
    url: "about:blank",
  };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--port") { args.port = Number(value); i += 1; }
    else if (key === "--profile") { args.profile = value; i += 1; }
    else if (key === "--url") { args.url = value; i += 1; }
  }
  return args;
}

function findEdge() {
  for (const candidate of EDGE_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return "msedge";
}

async function isUp(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(1500),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function main() {
  const args = parseArgs(process.argv);

  if (await isUp(args.port)) {
    console.log(`a debuggable browser is already listening on ${args.port}`);
    return;
  }

  fs.mkdirSync(args.profile, { recursive: true });

  const child = spawn(
    findEdge(),
    [
      `--remote-debugging-port=${args.port}`,
      `--user-data-dir=${args.profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      args.url,
    ],
    { detached: true, stdio: "ignore" },
  );
  child.unref();

  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((r) => setTimeout(r, 500));
    if (await isUp(args.port)) {
      const version = await (await fetch(`http://127.0.0.1:${args.port}/json/version`)).json();
      console.log(`${version.Browser} listening on ${args.port}`);
      console.log(`profile: ${args.profile}`);
      console.log("\nThis is a separate profile, so sign in to the bank in this window.");
      console.log("Your everyday Edge is untouched.");
      return;
    }
  }

  console.error(
    `Edge did not expose ${args.port}.\n` +
    `On Edge 136+ the port is ignored on the default profile; a distinct ` +
    `--user-data-dir is required, which this script already passes.`,
  );
  process.exit(1);
}

main();
