/* Local, read-only interactions. All appliance changes remain unavailable. */
(() => {
  "use strict";

  const root = document.documentElement;
  const themeKey = "smolofis.console.theme";
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  let snapshot = null;
  let timer;
  let activeRequest = null;
  const text = (selector, value) => {
    document.querySelectorAll(selector).forEach((element) => {
      const next = String(value);
      if (element.textContent !== next) element.textContent = next;
    });
  };
  const feedback = (message) => text("#action-feedback", message);

  try {
    const saved = localStorage.getItem(themeKey);
    if (["system", "light", "dark"].includes(saved)) preference = saved;
  } catch { /* Appearance still works when browser storage is unavailable. */ }

  function applyTheme() {
    if (preference === "system") delete root.dataset.theme;
    else root.dataset.theme = preference;
    const dark = preference === "dark" || (preference === "system" && media.matches);
    document.querySelectorAll("[data-toggle-theme]").forEach((button) => {
      button.setAttribute("aria-label", dark ? "Switch to light appearance" : "Switch to dark appearance");
      button.querySelector("use")?.setAttribute("href", dark ? "#icon-sun" : "#icon-moon");
    });
    document.querySelectorAll('input[name="theme"]').forEach((input) => {
      input.checked = input.value === preference;
    });
  }

  function chooseTheme(next) {
    preference = next;
    try { localStorage.setItem(themeKey, next); } catch { /* Browser-local preference only. */ }
    applyTheme();
  }

  document.querySelectorAll("[data-toggle-theme]").forEach((button) => {
    button.disabled = false;
    button.addEventListener("click", () => {
      const dark = preference === "dark" || (preference === "system" && media.matches);
      chooseTheme(dark ? "light" : "dark");
    });
  });
  document.querySelectorAll('input[name="theme"]').forEach((input) => {
    input.addEventListener("change", () => chooseTheme(input.value));
  });
  document.querySelector(".theme-options")?.removeAttribute("disabled");
  media.addEventListener("change", applyTheme);
  applyTheme();

  const phases = {
    ready: ["Core services are running", "Git hosting and deployment tools are responding.", "Online"],
    initializing: ["Getting the office ready", "Core services are starting. Their status will update automatically.", "Starting"],
    degraded: ["A service needs attention", "One or more core services are not responding. Check their details to find out more.", "Needs attention"],
    live: ["You're running from USB", "Install SmolOfis to a disk to start Git hosting and deployment tools.", "Live mode"],
  };
  const healthLabels = { online: "Online", offline: "Unavailable", pending: "Starting" };
  const ids = ["docker", "gitea", "coolify"];

  function validState(state) {
    return state && Object.hasOwn(phases, state.phase)
      && Array.isArray(state.services) && state.services.length === ids.length
      && ids.every((id) => state.services.filter((service) => service.id === id
        && Object.hasOwn(healthLabels, service.health) && typeof service.detail === "string").length === 1)
      && state.metrics && typeof state.metrics.hostname === "string"
      && ["cpu_percent", "mem_used", "mem_total", "disk_used", "disk_total", "uptime_secs"]
        .every((key) => Number.isFinite(state.metrics[key]) && state.metrics[key] >= 0)
      && Number.isFinite(state.updated_at) && state.updated_at > 0;
  }

  function capacity(used, total) {
    return total > 0 ? `${(used / 1073741824).toFixed(1)} / ${(total / 1073741824).toFixed(1)} GiB` : "Checking…";
  }

  function uptime(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds / 3600) % 24;
    const minutes = Math.floor(seconds / 60) % 60;
    if (days) return `${days}d ${hours}h ${minutes}m`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m ${Math.floor(seconds) % 60}s`;
  }

  function meter(name, value) {
    const element = document.querySelector(`[data-meter="${name}"]`);
    if (!element) return;
    const percent = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
    element.value = percent;
    element.textContent = `${Math.round(percent)}%`;
    element.dataset.level = percent >= 90 ? "critical" : percent >= 75 ? "warning" : "normal";
  }

  function lastChecked() {
    if (!snapshot) return;
    const age = Math.max(0, Math.floor(Date.now() / 1000 - snapshot.updated_at));
    const label = age < 10 ? "Checked just now" : age < 60 ? `Checked ${age}s ago` : `Checked ${Math.floor(age / 60)}m ago`;
    text("[data-last-check]", label);
  }

  function render(state) {
    snapshot = state;
    const metrics = state.metrics;
    const sampled = metrics.hostname.length > 0;
    const [title, detail, label] = phases[state.phase];
    document.body.dataset.phase = state.phase;
    delete document.body.dataset.connection;
    text("[data-phase-title]", title);
    text("[data-phase-detail]", detail);
    text("[data-phase-label]", label);
    text("[data-online]", state.services.filter((service) => service.health === "online").length);
    text("[data-total]", state.services.length);
    text("[data-hostname]", sampled ? metrics.hostname : "SmolOfis");
    text("[data-cpu]", sampled ? `${metrics.cpu_percent.toFixed(1)}%` : "Checking…");
    text("[data-memory]", capacity(metrics.mem_used, metrics.mem_total));
    text("[data-disk]", capacity(metrics.disk_used, metrics.disk_total));
    text("[data-uptime]", sampled ? uptime(metrics.uptime_secs) : "Checking…");
    text("[data-tailscale-ip]", metrics.tailscale_ip || "Not detected");
    text("[data-image-version]", state.panel?.image_version || "Not reported");
    text("[data-boot-mode]", state.panel?.boot_slot || (state.phase === "live" ? "USB live image" : "Development / live image"));
    meter("cpu", metrics.cpu_percent);
    meter("memory", metrics.mem_total > 0 ? 100 * metrics.mem_used / metrics.mem_total : 0);
    meter("disk", metrics.disk_total > 0 ? 100 * metrics.disk_used / metrics.disk_total : 0);
    for (const id of ids) {
      const service = state.services.find((entry) => entry.id === id);
      const nodes = document.querySelectorAll(`[data-service="${id}"]`);
      nodes.forEach((node) => {
        node.dataset.health = service.health;
        const status = node.querySelector("[data-service-status]");
        if (status) status.textContent = healthLabels[service.health];
        const detailNode = node.querySelector("[data-service-detail]");
        if (detailNode) detailNode.textContent = service.detail;
        const latency = node.querySelector("[data-service-latency]");
        if (latency) latency.textContent = Number.isFinite(service.latency_ms) ? `${service.latency_ms} ms` : "Not measured";
      });
      document.querySelectorAll(`[data-launch="${id}"]`).forEach((link) => {
        const port = state.links?.[`${id}_port`];
        const validPort = Number.isInteger(port) && port > 0 && port <= 65535;
        link.hidden = service.health !== "online" || !validPort;
        if (validPort) {
          const url = new URL(window.location.href);
          url.protocol = "http:";
          url.port = String(port);
          url.pathname = "/";
          url.search = "";
          url.hash = "";
          link.href = url.href;
        }
      });
      document.querySelectorAll(`[data-launch-unavailable="${id}"]`).forEach((node) => {
        node.hidden = service.health === "online";
        node.textContent = healthLabels[service.health];
      });
    }
    lastChecked();
  }

  function busy(value) {
    document.querySelectorAll("[data-refresh]").forEach((button) => {
      button.disabled = value;
      button.setAttribute("aria-busy", String(value));
    });
  }

  async function poll(manual = false) {
    clearTimeout(timer);
    if (activeRequest || document.visibilityState === "hidden") return;
    const request = new AbortController();
    activeRequest = request;
    busy(true);
    const timeout = setTimeout(() => request.abort(), 8000);
    try {
      const response = await fetch("/api/state", { cache: "no-store", credentials: "same-origin", signal: request.signal });
      if (!response.ok) throw new Error("State unavailable");
      const state = await response.json();
      if (!validState(state)) throw new Error("Incomplete state");
      render(state);
      document.querySelector("#connection-error").hidden = true;
      if (manual) feedback("Service status updated.");
    } catch {
      if (document.visibilityState !== "hidden" && !["hidden", "navigation"].includes(request.signal.reason)) {
        document.querySelector("#connection-error").hidden = false;
        document.body.dataset.connection = "lost";
        text("[data-phase-label]", "Connection lost");
        if (manual) feedback("Could not reach the server. Showing the last successful check.");
      }
    } finally {
      clearTimeout(timeout);
      activeRequest = null;
      busy(false);
      lastChecked();
      if (document.visibilityState !== "hidden") timer = setTimeout(poll, 3000);
    }
  }

  document.querySelectorAll("[data-refresh]").forEach((button) => {
    button.disabled = false;
    button.addEventListener("click", () => poll(true));
  });

  document.querySelectorAll("[data-copy-service]").forEach((button) => {
    button.disabled = false;
    button.addEventListener("click", async () => {
      const row = button.closest("[data-service]");
      const name = row.querySelector(".service-name strong").textContent;
      const lines = [
        `SmolOfis · ${document.querySelector("[data-hostname]").textContent}`,
        `${name}: ${row.querySelector("[data-service-status]").textContent}`,
        row.querySelector("[data-service-detail]").textContent,
        `Response time: ${row.querySelector("[data-service-latency]").textContent}`,
        snapshot ? `Last check: ${new Date(snapshot.updated_at * 1000).toISOString()}` : "Last check: page load",
        document.body.dataset.connection === "lost" ? "Server connection lost; these are the last known values." : "",
      ].filter(Boolean).join("\n");
      let result = row.querySelector(".copy-result");
      if (!result) {
        result = document.createElement("span");
        result.className = "copy-result";
        result.setAttribute("aria-hidden", "true");
        row.querySelector(".service-actions").append(result);
      }
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(lines);
        result.textContent = "Copied";
        row.querySelector("[data-copy-fallback]").hidden = true;
        feedback(`${name} diagnostics copied.`);
      } catch {
        const fallback = row.querySelector("[data-copy-fallback]");
        fallback.value = lines;
        fallback.hidden = false;
        fallback.focus();
        fallback.select();
        result.textContent = "Select and copy below";
        feedback("Clipboard access is unavailable. Select and copy the diagnostics below.");
      }
    });
  });

  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (document.visibilityState === "hidden") activeRequest?.abort("hidden");
    else poll();
  });
  window.addEventListener("pagehide", () => { clearTimeout(timer); activeRequest?.abort("navigation"); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) poll(); });
  poll();
})();
