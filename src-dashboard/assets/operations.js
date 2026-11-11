/* Opt-in administrator diagnostics. Submitted checks are never retried automatically. */
(() => {
  "use strict";
  const panel = document.querySelector("[data-operations]");
  if (!panel) return;
  const run = panel.querySelector("[data-run-identity]");
  const refresh = panel.querySelector("[data-refresh-operations]");
  const notice = panel.querySelector("[data-operation-notice]");
  const history = panel.querySelector("[data-operation-history]");
  const phases = { queued: "Queued", running: "Running", succeeded: "Completed", failed: "Failed", unknown: "Result unknown" };
  const outcomes = {
    identity_verified: "The server matches its saved identity.",
    identity_unavailable: "The saved or current identity could not be read. Ask your server administrator to inspect it.",
    identity_mismatch: "The server identity differs from its saved identity. Ask your server administrator to investigate.",
    interrupted: "This check was interrupted. Its result is unknown; it was not run again automatically.",
  };
  let savedOperations = [];
  let busy = false;
  let uncertain = false;
  let loaded = false;
  let blocked = false;
  function controls() {
    run.disabled = busy || uncertain || !loaded || blocked;
    refresh.disabled = busy;
    panel.setAttribute("aria-busy", String(busy));
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function valid(operation) {
    return operation && /^[a-f0-9]{64}$/i.test(operation.id)
      && operation.kind === "machine_identity_check" && Array.isArray(operation.transitions)
      && operation.transitions.length > 0 && operation.transitions.every(t => Object.hasOwn(phases, t.phase)
        && Number.isFinite(t.timestamp_ms) && !Number.isNaN(new Date(t.timestamp_ms).getTime()))
      && (operation.outcome === null || Object.hasOwn(outcomes, operation.outcome));
  }
  function render(operations) {
    savedOperations = operations;
    const fragment = document.createDocumentFragment();
    for (const operation of operations.slice().reverse()) {
      const last = operation.transitions.at(-1);
      const row = element("details", undefined, "operation-row");
      row.dataset.phase = last.phase;
      const summary = element("summary");
      const heading = element("span", undefined, "operation-summary");
      heading.append(element("strong", "Machine identity check"));
      const time = element("time", new Date(last.timestamp_ms).toLocaleString());
      time.dateTime = new Date(last.timestamp_ms).toISOString();
      heading.append(time);
      const chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      chevron.setAttribute("class", "icon disclosure");
      chevron.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#icon-chevron");
      chevron.append(use);
      summary.append(heading, element("span", phases[last.phase], "operation-status"), chevron);
      row.append(summary);
      const detail = element("div", undefined, "operation-detail");
      detail.append(element("p", outcomes[operation.outcome] || "This check has no confirmed result yet. Refresh history to check again."));
      const metadata = element("dl");
      for (const [label, value] of [["Operation ID", operation.id], ["Administrator ID", operation.actor_guid || "Local administrator"]]) {
        const pair = element("div");
        pair.append(element("dt", label), element("dd", value));
        metadata.append(pair);
      }
      detail.append(metadata);
      row.append(detail);
      fragment.append(row);
    }
    history.replaceChildren(fragment);
    blocked = operations.some(operation => ["queued", "running"].includes(operation.transitions.at(-1).phase));
    loaded = true;
  }
  async function request(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(10000), ...options });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) throw new Error("Your administrator session is unavailable. Sign in again using Account & sign out.");
      throw new Error("The operation service is unavailable. Refresh history when the server is reachable.");
    }
    return response.json();
  }
  async function load() {
    if (busy) return;
    busy = true; controls();
    try {
      const data = await request("/api/operations");
      if (!Array.isArray(data.operations) || data.operations.length > 256 || !data.operations.every(valid)) throw new Error("Operation history could not be read. Ask your server administrator to inspect it.");
      render(data.operations);
      notice.textContent = uncertain ? "History refreshed. Inspect the latest result before reloading this page to run another check. The previous request was not retried."
        : blocked ? "A check is unfinished. Refresh history to inspect its status; interrupted checks require local administrator recovery."
        : data.operations.length ? "History is up to date. Expand an operation for its result and reference ID." : "No operations recorded yet. Run an identity check to get started.";
    } catch (error) {
      loaded = false;
      notice.textContent = `${error.message} Previously loaded results, if any, are still shown.`;
    } finally { busy = false; controls(); }
  }
  run.addEventListener("click", async () => {
    if (busy || uncertain || !loaded || blocked) return;
    busy = true; controls();
    notice.textContent = "Checking the server identity…";
    try {
      const operation = await request("/api/operations/identity-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf: panel.dataset.csrf }) });
      if (!valid(operation)) throw new Error("The server returned an unreadable result.");
      render([...savedOperations.filter(item => item.id !== operation.id), operation]);
      notice.textContent = outcomes[operation.outcome] || "The check was accepted. Refresh history for its result.";
    } catch (error) {
      uncertain = true;
      notice.textContent = `${error.message} The check may have been recorded. Refresh history before reloading this page to run another check. It will not be retried automatically.`;
    } finally { busy = false; controls(); }
  });
  refresh.addEventListener("click", load);
  load();
})();
