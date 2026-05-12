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
        const small = document.createElement("small");
        small.className = "d-block mt-1";
        small.textContent = m.detail;
        div.appendChild(small);
      }
      container.appendChild(div);
    } else if (m.level === "error" || m.level === "critical") {
      window.alert(`${m.level.toUpperCase()}: ${m.text}${m.detail ? "\n\n" + m.detail : ""}`);
    }
  }
}

/**
 * Fetch a JsonEE API endpoint and unwrap the response envelope.
 *
 * On success returns envelope.data (the payload).
 * On failure throws an Error with .status, .detail, and .envelope set.
 * _messages from the envelope are always shown regardless of status.
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

  return envelope.data;
}
