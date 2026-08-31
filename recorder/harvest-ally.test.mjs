/**
 * Tests for harvest-ally logic.
 *
 *   node --test recorder/
 *
 * Only pure functions are tested here — filename construction, date-range
 * extraction, and date formatting. The network/CDP path requires a live
 * session and cannot be unit-tested without mocking infrastructure.
 *
 * Fixtures are synthetic. No real account numbers, tokens or transactions
 * appear anywhere in this file.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { buildFilename, extractDateRange } from "./harvest-ally.mjs";

// ─── Synthetic account fixture ───────────────────────────────────────────────
// Token is assembled from fragments so no single line contains a complete
// credential-shaped string — the same pattern used in redact.test.mjs.
const TOKEN_FRAGMENT_A = "pvtEnc";
const TOKEN_FRAGMENT_B = "rypted";
const FAKE_TOKEN = TOKEN_FRAGMENT_A + TOKEN_FRAGMENT_B + "AAAA1111";

const CHECKING = {
  token: FAKE_TOKEN,
  accountId: "acct-001",
  last4: "1234",
  name: "Checking",
  type: "Checking",
};

const SAVINGS = {
  token: FAKE_TOKEN,
  accountId: "acct-002",
  last4: "5678",
  name: "Online Savings",
  type: "Savings",
};

// ─── buildFilename ────────────────────────────────────────────────────────────

test("buildFilename produces the expected convention", () => {
  const result = buildFilename(CHECKING, "2024-01-05", "2025-08-26");
  assert.equal(
    result,
    "Transactions - Ally Checking (1234) - 2024-01-05 to 2025-08-26.csv",
  );
});

test("buildFilename includes account last-4, not full number", () => {
  const result = buildFilename(SAVINGS, "2024-06-01", "2025-06-30");
  assert.ok(result.includes("(5678)"), "should include last-4");
  assert.ok(!result.includes("56789"), "should not include longer suffix");
});

test("buildFilename uses no-transactions when dates are null", () => {
  const result = buildFilename(CHECKING, null, null);
  assert.ok(result.includes("no-transactions"), result);
});

test("buildFilename strips characters invalid on Windows filesystems", () => {
  const weird = { ...CHECKING, name: 'Acc/ount: "Test" <One>' };
  const result = buildFilename(weird, "2025-01-01", "2025-12-31");
  assert.ok(!/[<>:"/\\|?*]/.test(result), `invalid chars in: ${result}`);
});

test("buildFilename handles multi-word account names", () => {
  const result = buildFilename(SAVINGS, "2025-01-01", "2025-12-31");
  assert.ok(result.includes("Online Savings"), result);
});

// ─── extractDateRange ────────────────────────────────────────────────────────

// Ally CSV shape: Date, Time, Amount, Type, Description
// Note: header fields after the first have a leading space (Ally quirk).
const CSV_BASIC = `Date, Time, Amount, Type, Description
3/15/2025,00:00:00,-12.34,Withdrawal,Coffee Shop
1/5/2025,00:00:00,500.00,Credit,Payroll
8/20/2025,00:00:00,-99.00,Withdrawal,Grocery
`;

test("extractDateRange returns correct min and max dates", () => {
  const { firstDate, lastDate } = extractDateRange(CSV_BASIC);
  assert.equal(firstDate, "2025-01-05");
  assert.equal(lastDate, "2025-08-20");
});

test("extractDateRange handles single-row CSV", () => {
  const csv = `Date, Time, Amount, Type, Description\n12/31/2024,00:00:00,100.00,Credit,Transfer\n`;
  const { firstDate, lastDate } = extractDateRange(csv);
  assert.equal(firstDate, "2024-12-31");
  assert.equal(lastDate, "2024-12-31");
});

test("extractDateRange returns nulls for header-only CSV", () => {
  const csv = `Date, Time, Amount, Type, Description\n`;
  const { firstDate, lastDate } = extractDateRange(csv);
  assert.equal(firstDate, null);
  assert.equal(lastDate, null);
});

test("extractDateRange returns nulls for completely empty CSV", () => {
  const { firstDate, lastDate } = extractDateRange("");
  assert.equal(firstDate, null);
  assert.equal(lastDate, null);
});

test("extractDateRange skips rows with unparseable dates without throwing", () => {
  const csv = `Date, Time, Amount, Type, Description\nnot-a-date,00:00:00,1.00,Credit,x\n6/1/2025,00:00:00,2.00,Credit,y\n`;
  const { firstDate, lastDate } = extractDateRange(csv);
  assert.equal(firstDate, "2025-06-01");
  assert.equal(lastDate, "2025-06-01");
});

test("extractDateRange handles CRLF line endings", () => {
  const csv = `Date, Time, Amount, Type, Description\r\n2/14/2025,00:00:00,25.00,Withdrawal,Flowers\r\n`;
  const { firstDate, lastDate } = extractDateRange(csv);
  assert.equal(firstDate, "2025-02-14");
  assert.equal(lastDate, "2025-02-14");
});
