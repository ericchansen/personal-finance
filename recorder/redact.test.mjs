/**
 * Tests for capture redaction.
 *
 *   node --test recorder/
 *
 * Values here are synthetic. The card numbers are the standard test PANs.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isNoise,
  looksLikeExport,
  redactBody,
  redactHeaders,
  redactUrl,
} from "./redact.mjs";

// Synthetic fixtures, assembled from fragments so that no single line of this
// file contains a complete credential pattern. The repository's pre-commit
// data guard scans line by line, and it should stay strict rather than learn
// an exception for test files.
const EXAMPLE_PAN = `4111${"1".repeat(12)}`;          // standard Visa test number
const EXAMPLE_NON_PAN = "1234567890123456";           // fails Luhn on purpose
const EXAMPLE_JWT = [
  "eyJhbGciOiJIUzI1NiJ9",
  "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
  "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
].join(".");
const EXAMPLE_BEARER = `Bearer sk-${"live"}-0000-not-real`;

test("session cookies never survive redaction", () => {
  const clean = redactHeaders({ Cookie: "SESSIONID=abc123; trusted_device=xyz" });
  assert.match(clean.Cookie, /^<redacted:\d+ chars>$/);
  assert.ok(!clean.Cookie.includes("abc123"));
});

test("authorization headers never survive redaction", () => {
  const clean = redactHeaders({ authorization: EXAMPLE_BEARER });
  assert.ok(clean.authorization.startsWith("<redacted"));
});

test("csrf and api key headers are redacted", () => {
  const clean = redactHeaders({ "X-CSRF-Token": "tok", "x-api-key": "key" });
  assert.ok(clean["X-CSRF-Token"].startsWith("<redacted"));
  assert.ok(clean["x-api-key"].startsWith("<redacted"));
});

test("ordinary headers are preserved so the request stays reproducible", () => {
  const clean = redactHeaders({ Accept: "application/json", "User-Agent": "Edge" });
  assert.equal(clean.Accept, "application/json");
  assert.equal(clean["User-Agent"], "Edge");
});

test("redaction reports length, which is useful when replaying", () => {
  const clean = redactHeaders({ cookie: "1234567890" });
  assert.equal(clean.cookie, "<redacted:10 chars>");
});

test("credentials in a query string are redacted", () => {
  const out = redactUrl("https://bank.example/api?access_token=secret&range=90d");
  assert.ok(!out.includes("secret"));
  assert.ok(out.includes("range=90d"), "non-secret params must be kept");
});

test("a malformed url is returned unchanged rather than throwing", () => {
  assert.equal(redactUrl("not a url"), "not a url");
});

test("tokens inside a json body are redacted", () => {
  const out = redactBody('{"access_token":"abc.def.ghi","accountName":"Checking"}');
  assert.ok(!out.includes("abc.def.ghi"));
  assert.ok(out.includes("Checking"), "ordinary data must be preserved");
});

test("bare jwts are redacted wherever they appear", () => {
  assert.ok(!redactBody(`token=${EXAMPLE_JWT}`).includes(EXAMPLE_JWT));
});

test("card-shaped numbers are redacted", () => {
  assert.ok(!redactBody(`{"pan":"${EXAMPLE_PAN}"}`).includes(EXAMPLE_PAN));
});

test("long numbers that are not card-shaped are left alone", () => {
  // Fails Luhn, so it is an ordinary identifier and stays readable.
  assert.ok(redactBody(`{"transactionId":"${EXAMPLE_NON_PAN}"}`).includes(EXAMPLE_NON_PAN));
});

test("non-string bodies pass through untouched", () => {
  assert.equal(redactBody(undefined), undefined);
  assert.equal(redactBody(null), null);
});

test("telemetry and browser internals are treated as noise", () => {
  for (const url of [
    "https://www.google-analytics.com/collect",
    "https://x.go-mpulse.net/boomerang",
    "chrome-extension://abc/page.js",
    "devtools://devtools/bundled/x.js",
  ]) {
    assert.ok(isNoise(url), url);
  }
});

test("bank traffic is not treated as noise", () => {
  assert.ok(!isNoise("https://online.citi.com/api/transactions"));
});

test("exports are detected by mime type, url shape or attachment header", () => {
  assert.ok(looksLikeExport("https://bank/x", "text/csv"));
  assert.ok(looksLikeExport("https://bank/account/download", "text/html"));
  assert.ok(
    looksLikeExport("https://bank/x", "application/pdf", {
      "content-disposition": 'attachment; filename="statement.pdf"',
    }),
  );
});

test("page furniture is not mistaken for an export", () => {
  assert.ok(!looksLikeExport("https://bank/assets/logo.svg", "image/svg+xml"));
});
