/**
 * JsonEE browser client.
 *
 * Provides apiFetch() — a fetch() wrapper that unwraps the standard
 * JsonEE response envelope and routes _messages to the UI.
 *
 * Message display:
 *   The app should provide a <div id="jsonee_messages"> anywhere in the
 *   document. Messages are appended as Bootstrap-compatible alert divs:
 *     <div class="alert alert-info" role="alert">text</div>
 *
 *   Bootstrap alert class mapping:
 *     debug, info  → alert-info
 *     warn         → alert-warning
 *     error        → alert-danger
 *     critical     → alert-danger
 *
 *   If no #jsonee_messages div is found, falls back to console.* for
 *   all levels and additionally window.alert() for error / critical.
 */

function _bsClass(level) {
  if (level === "warn")                        return "alert-warning";
  if (level === "error" || level === "critical") return "alert-danger";
  return "alert-info";
}

function _consoleFn(level) {
  if (level === "error" || level === "critical") return console.error.bind(console);
  if (level === "warn")                          return console.warn.bind(console);
  if (level === "debug")                         return console.debug.bind(console);
  return console.info.bind(console);
}

function _showMessages(messages) {
  if (!messages || messages.length === 0) return;
  const container = document.getElementById("jsonee_messages");
  for (const m of messages) {
    _consoleFn(m.level)(`[jsonee] ${m.text}`, m.detail ?? "");
    if (container) {
      const div = document.createElement("div");
      div.className = `alert ${_bsClass(m.level)}`;
      div.setAttribute("role", "alert");
      div.textContent = m.text;
      if (m.detail) {
        const details = document.createElement("details");
        details.style.marginTop = "4px";
        const summary = document.createElement("summary");
        summary.style.cssText = "cursor:pointer;font-size:0.85em;opacity:0.75;";
        summary.textContent = "details";
        const pre = document.createElement("pre");
        pre.style.cssText = "margin:6px 0 0;font-size:0.8em;white-space:pre-wrap;word-break:break-all;opacity:0.85;";
        pre.textContent = m.detail;
        details.appendChild(summary);
        details.appendChild(pre);
        div.appendChild(details);
      }
      if (m.level != "debug") {
        container.appendChild(div);
      }
    } else if (m.level === "error" || m.level === "critical") {
      window.alert(`${m.level.toUpperCase()}: ${m.text}${m.detail ? "\n\n" + m.detail : ""}`);
    }
  }
}

/**
 * Walk a decoded JSON value and materialise extjson markers into JS natives.
 *   {"$date": "ISO"}  → Date
 *   {"$oid":  "hex"}  → string  (OIDs are opaque in the browser)
 *   {"$long": "n"}    → BigInt  (preserves precision beyond Number.MAX_SAFE_INTEGER)
 * Plain values pass through unchanged, so calling this on any JSON is safe.
 */
export function fromExtJSON(value) {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(fromExtJSON);
  const keys = Object.keys(value);
  if (keys.length === 1) {
    if (keys[0] === "$date") return new Date(value.$date);
    if (keys[0] === "$oid")  return value.$oid;
    if (keys[0] === "$long") return BigInt(value.$long);
  }
  const out = {};
  for (const [k, v] of Object.entries(value)) out[k] = fromExtJSON(v);
  return out;
}

/**
 * Walk a JS value and convert natives to extjson markers.
 *   Date   → {"$date": "ISO-Z"}
 *   BigInt → {"$long": "decimal string"}
 */
export function toExtJSON(value) {
  if (value === null || value === undefined) return value;
  if (value instanceof Date) return { "$date": value.toISOString() };
  if (typeof value === "bigint") return { "$long": value.toString() };
  if (Array.isArray(value)) return value.map(toExtJSON);
  if (typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = toExtJSON(v);
    return out;
  }
  return value;
}

/**
 * Fetch a JsonEE API endpoint and unwrap the response envelope.
 *
 * On success returns envelope.data with extjson markers materialised
 * (Date, BigInt). On failure throws an Error with .status, .detail,
 * and .envelope set. _messages are always shown regardless of status.
 *
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
export async function apiFetch(url, options = {}) {
  const r = await fetch(url, options);
  let envelope;
  try {
    envelope = await r.json();
  } catch (e) {
    throw new Error(r.ok ? `invalid JSON from ${url}` : `HTTP ${r.status} from ${url}`);
  }

  _showMessages(envelope._messages);

  if (!r.ok) {
    const errMsg = (envelope._messages || [])
      .find(m => m.level === "error" || m.level === "critical");
    const text = errMsg ? errMsg.text : `HTTP ${r.status}`;
    throw Object.assign(new Error(text), {
      status: r.status,
      detail: errMsg?.detail,
      envelope,
    });
  }

  return fromExtJSON(envelope.data);
}
