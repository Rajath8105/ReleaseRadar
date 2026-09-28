"use strict";

let state = null;
let loading = false;
const expanded = new Set();
const $ = id => document.getElementById(id);

const escapeHtml = value => String(value ?? "").replace(
  /[&<>"']/g,
  c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[c]
);

const shortSha = sha => escapeHtml((sha || "").slice(0, 10));

function badge(value) {
  const css = value.toLowerCase().replaceAll(" ", "-");
  return `<span class="badge ${escapeHtml(css)}">â— ${escapeHtml(value)}</span>`;
}

function link(url, label) {
  try {
    const parsed = new URL(url);

    if (!["https:", "http:"].includes(parsed.protocol))
      return escapeHtml(label);

    return `<a href="${escapeHtml(parsed.href)}"
      target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch {
    return escapeHtml(label);
  }
}

function range(values) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? String(min) : `${min}â€“${max}`;
}

function drift(service, kind) {
  const items = service.comparisons.filter(x => x.kind === kind);

  if (service.observations.some(x => x.error))
    return badge("Unknown");

  if (!items.length) {
    return `<span class="muted">${
      kind === "Release" && service.comparisons.length
        ? "Not tracked"
        : "Unavailable"
    }</span>`;
  }

  if (items.some(x => x.state === "Unknown"))
    return badge("Unknown");

  return `
    <span class="drift">${range(items.map(x => x.ahead))}</span>
    <span class="muted">not live</span>
    <div class="sub">
      ${range(items.map(x => x.behind))} production-only
    </div>`;
}

function comparisonHtml(item) {
  return `
    <article class="comparison">
      <div class="comparison-head">
        <strong>${escapeHtml(item.kind)}</strong>
        ${badge(item.state)}
      </div>

      <p>Branch: <code>${escapeHtml(item.branch)}</code></p>

      <p>
        <code>${shortSha(item.productionSha)}</code>
        <span class="muted">production â†’</span>
        <code>${shortSha(item.headSha) || "unknown"}</code>
        <span class="muted">branch HEAD</span>
      </p>

      ${
        item.error
          ? `<p style="color:var(--red)">${escapeHtml(item.error)}</p>`
          : `<p><strong>${item.ahead}</strong> commits not live Â·
               <strong>${item.behind}</strong> production-only</p>`
      }

      ${
        item.url
          ? `<p>${link(item.url, "Open GitHub comparison â†—")}</p>`
          : ""
      }

      ${
        item.commits.length
          ? `<p class="muted">Commit sample, not a complete changelog:</p>
             <ul>${item.commits.map(c =>
               `<li>${link(c.url, c.message)}</li>`
             ).join("")}</ul>`
          : ""
      }
    </article>`;
}

function detailsHtml(service) {
  return `
    ${
      service.error
        ? `<div class="notice error">${escapeHtml(service.error)}</div>`
        : ""
    }

    ${
      service.warnings.length
        ? `<div class="notice"><ul>${service.warnings.map(w =>
            `<li>${escapeHtml(w)}</li>`
          ).join("")}</ul></div>`
        : ""
    }

    <div class="detail-grid">
      ${service.comparisons.map(comparisonHtml).join("")}
    </div>

    <div class="evidence">
      <strong>Production evidence</strong>
      <p>
        Deployment: <code>${escapeHtml(service.deployment)}</code><br>
        Desired replicas: ${service.desiredReplicas} Â·
        Observed pods: ${service.observedPods} Â·
        Ready application containers: ${service.readyContainers}
      </p>

      ${service.observations.map(item => `
        <p>
          <strong>${escapeHtml(item.pod)}</strong> Â·
          ${escapeHtml(item.container)}<br>
          Image: <code>${escapeHtml(item.image)}</code><br>
          Runtime imageID: <code>${escapeHtml(item.imageId || "Unavailable")}</code><br>
          Source SHA suffix: <code>${escapeHtml(item.shortSha || "Unknown")}</code>
          ${
            item.error
              ? `<br><span style="color:var(--red)">${escapeHtml(item.error)}</span>`
              : ""
          }
        </p>
      `).join("")}
    </div>`;
}

function renderRows() {
  if (!state) return;

  const services = state.data.services;
  const search = $("search").value.toLowerCase().trim();
  const filter = $("filter").value;

  const visible = services.filter(service => {
    const text = [
      service.name, service.repository, service.deployment, service.namespace
    ].join(" ").toLowerCase();

    return text.includes(search) &&
      (!filter ||
        (filter === "Mixed versions" ? service.mixed : service.state === filter));
  });

  $("results").textContent =
    `Showing ${visible.length} of ${services.length} configured services`;

  $("rows").innerHTML = visible.length
    ? visible.map(service => {
        const tags = [...new Set(service.observations.map(x => x.imageTag))];
        const open = expanded.has(service.id);

        return `
          <tr class="main-row">
            <td>
              <div class="service-name">${escapeHtml(service.name)}</div>
              <div class="sub">${link(service.repositoryUrl, service.repository)}</div>
              ${
                service.warnings.length
                  ? `<div class="sub">${service.warnings.length} operational notice(s)</div>`
                  : ""
              }
            </td>
            <td>
              ${
                tags.length
                  ? tags.map(tag =>
                      `<code class="image-tag">${escapeHtml(tag || "Unresolved tag")}</code>`
                    ).join("<br>")
                  : '<span class="muted">No live version</span>'
              }
              <div class="sub">${service.readyContainers} ready application container(s)</div>
            </td>
            <td>${drift(service, "Trunk")}</td>
            <td>${drift(service, "Release")}</td>
            <td>
              ${badge(service.state)}
              ${
                service.mixed
                  ? '<br><span class="badge mixed">â— Mixed versions</span>'
                  : ""
              }
            </td>
            <td>
              <button class="inspect"
                data-id="${escapeHtml(service.id)}"
                aria-expanded="${open}">
                ${open ? "Hide â†‘" : "Inspect â†“"}
              </button>
            </td>
          </tr>
          ${
            open
              ? `<tr><td colspan="6" class="detail-cell">${detailsHtml(service)}</td></tr>`
              : ""
          }`;
      }).join("")
    : '<tr><td colspan="6" class="empty">No matching services.</td></tr>';
}

function render() {
  if (!state) return;

  const data = state.data;
  const services = data.services;

  $("mode").textContent = `${data.mode.toUpperCase()} DATA`;
  $("context").textContent = data.context || "Not yet observed";
  $("namespace").textContent = data.namespace || "Not yet observed";
  $("discovered").textContent = data.discoveredDeployments;
  $("count").textContent = `${services.length} services`;

  const metricData = [
    ["Configured services", services.length, "Explicit service-to-repository mappings", "var(--blue)"],
    ["Aligned", services.filter(x => x.state === "Aligned").length, "Across configured comparisons", "var(--green)"],
    ["Changes not live", services.filter(x =>
      x.comparisons.some(c => c.ahead !== null && c.ahead > 0)
    ).length, "Services with branch-only commits", "var(--amber)"],
    ["Mixed versions", services.filter(x => x.mixed).length, "Multiple image identities or source SHAs", "var(--purple)"],
    ["Needs attention", services.filter(x =>
      ["Unknown", "Diverged", "Production ahead", "No live version", "Missing workload"]
        .includes(x.state)
    ).length, "History conflict, missing runtime, or uncertainty", "var(--red)"]
  ];

  $("metrics").innerHTML = metricData.map(([label, value, hint, color]) => `
    <article class="metric" style="--accent:${color}">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
      <div class="metric-hint">${hint}</div>
    </article>
  `).join("");

  let notices = "";

  if (data.error)
    notices += `<div class="notice error">${escapeHtml(data.error)}</div>`;

  if (state.stale)
    notices += '<div class="notice error">Inventory is stale or no successful observation exists. Do not treat displayed results as current release approval.</div>';

  if (data.coverageWarnings.length) {
    notices += `
      <details class="notice">
        <summary>${data.coverageWarnings.length} discovery / coverage notice(s)</summary>
        <ul>${data.coverageWarnings.slice(0, 100).map(w =>
          `<li>${escapeHtml(w)}</li>`
        ).join("")}</ul>
        ${
          data.coverageWarnings.length > 100
            ? "<p>Showing the first 100 notices.</p>"
            : ""
        }
      </details>`;
  }

  $("notices").innerHTML = notices;
  $("refresh").disabled = state.isScanning;

  $("refresh").textContent =
    state.isScanning ? "â†» Scanningâ€¦" : "â†» Refresh inventory";

  $("scan-info").textContent = state.isScanning
    ? "Reading Kubernetes and GitHub"
    : `Scheduled every ${state.intervalSeconds}s Â· UI refreshes every 10s`;

  $("updated").textContent = data.observedAtUtc
    ? `Inventory observed ${new Date(data.observedAtUtc).toLocaleString()}`
    : "Waiting for first observationâ€¦";

  renderRows();
}

async function load() {
  if (loading) return;
  loading = true;

  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");

    state = await response.json();
    render();
  } catch {
    $("notices").innerHTML = `
      <div class="notice error">
        Backend unavailable. Any displayed results may be outdated.
        Check that dotnet is still running.
      </div>`;

    $("refresh").disabled = false;
  } finally {
    loading = false;
  }
}

$("search").addEventListener("input", renderRows);
$("filter").addEventListener("change", renderRows);

$("rows").addEventListener("click", event => {
  const button = event.target.closest("button[data-id]");
  if (!button) return;

  const id = button.dataset.id;
  expanded.has(id) ? expanded.delete(id) : expanded.add(id);
  renderRows();
});

$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;

  try {
    const response = await fetch("/api/scan", { method: "POST" });
    const result = await response.json();
    $("action-message").textContent = result.message || "Refresh requested.";
  } catch {
    $("action-message").textContent = "Could not contact the refresh API.";
  } finally {
    await load();
    setTimeout(() => { $("action-message").textContent = ""; }, 6000);
  }
});

load();
setInterval(load, 10000);
