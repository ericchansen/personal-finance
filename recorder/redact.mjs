/**
 * Redaction for captured browser traffic.
 *
 * A recording of a banking session contains live session cookies, bearer
 * tokens and account identifiers. Anything written to disk goes through here
 * first, so a capture is safe to read, diff and paste into a runbook.
 *
 * Kept separate from the recorder so it can be tested directly.
 */

/** Header names whose value authenticates a session. */
export const SECRET_HEADERS = new Set([
  "cookie", "set-cookie", "authorization", "proxy-authorization",
  "x-csrf-token", "x-xsrf-token", "x-api-key", "api-key",
  "x-auth-token", "auth-token", "x-session-token", "x-access-token",
  "x-amz-security-token", "x-goog-api-key", "x-sig", "x-signature",
]);

/** Query-string parameters that carry credentials. */
const SECRET_PARAMS = /token|auth|key|secret|session|sig|signature|password|jwt/i;

/** Bodies worth keeping: this is where an export actually lives. */
export const INTERESTING_TYPES =
  /json|csv|text\/xml|application\/xml|ofx|qfx|text\/plain|spreadsheet|excel|octet-stream/i;

/**
 * Media types that never carry an export. Checked first, because a bank page
 * is mostly images and fonts and `image/svg+xml` would otherwise match the
 * XML rule and drag every icon into the capture.
 */
const NEVER_INTERESTING = /^(image|font|audio|video)\//i;

/** URL shapes that suggest a data export rather than page furniture. */
export const EXPORT_HINTS =
  /download|export|statement|transaction|activity|history|report|ofx|qfx|csv|posted|detail/i;

/** Third-party telemetry and browser internals; never useful, always loud. */
export const NOISE =
  /google|doubleclick|facebook|adobe|analytics|tealium|newrelic|nr-data|sentry|optimizely|akstat|go-mpulse|demdex|quantserve|scorecardresearch|clarity\.ms|^chrome-extension:|^devtools:|^chrome:|^edge:/i;

export function redactHeaders(headers = {}) {
  const clean = {};
  for (const [name, value] of Object.entries(headers)) {
    clean[name] = SECRET_HEADERS.has(name.toLowerCase())
      ? `<redacted:${String(value).length} chars>`
      : value;
  }
  return clean;
}

export function redactUrl(url) {
  try {
    const parsed = new URL(url);
    for (const key of [...parsed.searchParams.keys()]) {
      if (SECRET_PARAMS.test(key)) parsed.searchParams.set(key, "<redacted>");
    }
    return parsed.toString();
  } catch {
    return url;
  }
}

export function redactBody(text) {
  if (typeof text !== "string") return text;
  return text
    .replace(
      /("(?:access_?token|id_?token|refresh_?token|password|passwd|secret|apiKey|api_key|sessionId|session_id)"\s*:\s*")[^"]*"/gi,
      '$1<redacted>"',
    )
    .replace(/\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b/g, "<redacted-jwt>")
    .replace(/\b\d{13,19}\b(?=[^\d]|$)/g, (m) => (isProbableCard(m) ? "<redacted-pan>" : m));
}

/** Luhn check, so account-length digits are only redacted when card-shaped. */
function isProbableCard(digits) {
  let sum = 0;
  let alternate = false;
  for (let i = digits.length - 1; i >= 0; i -= 1) {
    let n = Number(digits[i]);
    if (alternate) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    sum += n;
    alternate = !alternate;
  }
  return sum % 10 === 0;
}

export function isNoise(url) {
  return NOISE.test(url);
}

export function looksLikeExport(url, mimeType, headers = {}) {
  const disposition =
    headers["content-disposition"] || headers["Content-Disposition"] || "";
  // An explicit attachment is authoritative even for a media type.
  if (/attachment/i.test(disposition)) return true;
  if (NEVER_INTERESTING.test(mimeType || "")) return false;
  return INTERESTING_TYPES.test(mimeType || "") || EXPORT_HINTS.test(url);
}
