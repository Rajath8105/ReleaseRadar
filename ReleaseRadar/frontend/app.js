"use strict";

let snapshot = null;
let loading = false;
const expanded = new Set();
const $ = id => document.getElementById(id);

const escapeHtml = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[character]
);

const stateClass = value => value.toLowerCase().replaceAll(" ", "-");
const shortSha = value => escapeHtml((value || "").slice(0, 7));

function badge(state) {
  return `<span class="badge ${stateClass(state)}">● ${escapeHtml(state)}</span>`;
}

function link(url, label) {
  try {
    const parsed = new URL(url);

    if (!["https:", "http:"].includes(parsed.protocol)) {
      return escapeHtml(label);
    }

    return `<a href="${escapeHtml(parsed.href)}"
               target="_blank"
               rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch {
    return escapeHtml(label);
  }
}

function range(values) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? String(min) : `${min}–${max}`;
}

function drift(repo, kind) {
  const comparisons = repo.comparisons.filter(x => x.kind === kind);

  if (!comparisons.length) {
    return `<span class="muted">${
      repo.state === "Unknown" ? "Unavailable" : "Not tracked"
    }</span>`;
  }

  if (comparisons.some(x => x.state === "Unknown")) {
    return badge("Unknown");
  }

  return `
    <div>
      <span class="drift-value">${range(comparisons.map(x => x.ahead))}</span>
      <span class="drift-label">not live</span>
    </div>
    <div class="sub">
      ${range(comparisons.map(x => x.behind))} production-only
    </div>`;
}

function comparisonHtml(item) {
  return `
    <div class="comparison">
      <div class="comparison-header">
        <strong>${escapeHtml(item.kind)}</strong>
        ${badge(item.state)}
      </div>

      <p><span class="muted">Branch:</span> <code>${escapeHtml(item.branch)}</code></p>

      <p>
        <code>${shortSha(item.productionSha)}</code>
        <span class="muted"> production → </span>
        <code>${shortSha(item.headSha) || "unknown"}</code>
        <span class="muted"> branch HEAD</span>
      </p>

      ${
        item.error
          ? `<p style="color:var(--red)">${escapeHtml(item.error)}</p>`
          : `<p>
               <strong>${item.ahead}</strong> commits not in production ·
               <strong>${item.behind}</strong> production-only commits
             </p>`
      }

      ${item.url ? `<p>${link(item.url, "Open GitHub comparison ↗")}</p>` : ""}

      ${
        item.commits.length
          ? `<p class="muted">Commit sample — not a complete changelog</p>
             <ul>${item.commits.map(commit =>
               `<li>${link(commit.url, commit.message)}</li>`
             ).join("")}</ul>`
          : ""
      }
    </div>`;
}

function detailsHtml(repo) {
  return `
    ${repo.error ? `<div class="notice error">${escapeHtml(repo.error)}</div>` : ""}

    <div class="detail-grid">
      ${repo.comparisons.map(comparisonHtml).join("")}
    </div>

    <div class="evidence">
      <strong>Observed production instances</strong>
      ${repo.deployments.map(item => `
        <p>
          <code>${shortSha(item.sha)}</code> ·
          ${escapeHtml(item.namespace)}/${escapeHtml(item.workload)} ·
          ${escapeHtml(item.container)}
          <br>
          ${escapeHtml(item.evidence)} · ${escapeHtml(item.imageDigest)}
        </p>
      `).join("")}
    </div>`;
}

function renderMetrics(repos) {
  const metrics = [
    {
      label: "Production repos",
      value: repos.length,
      hint: "Within configured discovery scope",
      color: "var(--blue)",
      icon: "▦"
    },
    {
      label: "Fully aligned",
      value: repos.filter(x => x.state === "Aligned").length,
      hint: "Across tracked comparisons",
      color: "var(--green)",
      icon: "✓"
    },
    {
      label: "Changes not live",
      value: repos.filter(x =>
        x.comparisons.some(c => c.state !== "Unknown" && c.ahead > 0)
      ).length,
      hint: "Repos with branch-only commits",
      color: "var(--amber)",
      icon: "↗"
    },
    {
      label: "Mixed rollouts",
      value: repos.filter(x => x.mixed).length,
      hint: "Multiple production versions",
      color: "var(--purple)",
      icon: "◐"
    },
    {
      label: "Needs attention",
      value: repos.filter(x =>
        ["Diverged", "Production ahead", "Unknown"].includes(x.state)
      ).length,
      hint: "History conflict or uncertainty",
      color: "var(--red)",
      icon: "!"
    }
  ];

  $("metrics").innerHTML = metrics.map(item => `
    <article class="metric" style="--accent:${item.color}">
      <div class="metric-top">
        <span>${item.label}</span>
        <span class="metric-icon">${item.icon}</span>
      </div>
      <div class="metric-value">${item.value}</div>
      <div class="metric-hint">${item.hint}</div>
    </article>
  `).join("");
}

function renderRows() {
  if (!snapshot) return;

  const repos = snapshot.data.repositories;
  const query = $("search").value.trim().toLowerCase();
  const filter = $("filter").value;

  const visible = repos.filter(repo => {
    const text = [
      repo.repo,
      repo.defaultBranch,
      ...repo.deployments.map(x => x.namespace),
      ...repo.comparisons.map(x => x.branch)
    ].join(" ").toLowerCase();

    const matchesState =
      !filter ||
      (filter === "Mixed rollout" ? repo.mixed : repo.state === filter);

    return text.includes(query) && matchesState;
  });

  $("results-count").textContent =
    `Showing ${visible.length} of ${repos.length} repositories`;

  if (!visible.length) {
    $("rows").innerHTML = `
      <tr><td colspan="6" class="empty">
        No matching repositories. An empty inventory does not prove alignment.
      </td></tr>`;
    return;
  }

  $("rows").innerHTML = visible.map(repo => {
    const shas = [...new Set(repo.deployments.map(x => x.sha))];
    const isExpanded = expanded.has(repo.repo);
    const splitAt = repo.repo.indexOf("/");
    const owner = repo.repo.slice(0, splitAt);
    const name = repo.repo.slice(splitAt + 1);

    return `
      <tr class="repo-row">
        <td>
          <div class="repo-title">
            <span class="repo-icon">⌘</span>
            <div>
              <div class="repo-name">${link(repo.url, name)}</div>
              <div class="sub">
                ${escapeHtml(owner)} · ${escapeHtml(repo.defaultBranch || "unknown")}
              </div>
            </div>
          </div>
        </td>
        <td>
          ${shas.map(sha => `<code class="sha">${shortSha(sha)}</code>`).join("<br>")}
          <div class="sub">${repo.deployments.length} container instance(s)</div>
        </td>
        <td>${drift(repo, "Trunk")}</td>
        <td>${drift(repo, "Release")}</td>
        <td>
          ${badge(repo.state)}
          ${repo.mixed ? '<br><span class="badge mixed">◐ Mixed rollout</span>' : ""}
        </td>
        <td>
          <button class="inspect"
            data-repo="${escapeHtml(repo.repo)}"
            aria-expanded="${isExpanded}">
            ${isExpanded ? "Hide ↑" : "Inspect ↓"}
          </button>
        </td>
      </tr>
      ${
        isExpanded
          ? `<tr><td colspan="6" class="detail-cell">${detailsHtml(repo)}</td></tr>`
          : ""
      }`;
  }).join("");
}

function render() {
  if (!snapshot) return;

  const data = snapshot.data;
  $("mode").textContent = data.mode.toUpperCase();
  $("source").textContent = data.source || "Waiting for inventory";
  $("repo-count").textContent = data.repositories.length;

  renderMetrics(data.repositories);
  renderRows();

  let notices = "";

  if (data.error) {
    notices += `<div class="notice error">${escapeHtml(data.error)}</div>`;
  }

  if (snapshot.stale) {
    notices += `
      <div class="notice error">
        Data is stale or no successful inventory has been observed.
        Do not interpret this dashboard as current release approval.
      </div>`;
  }

  if (data.warnings.length) {
    notices += `
      <details class="notice">
        <summary>
          ${data.warnings.length} inventory / coverage notice(s) — click to inspect
        </summary>
        <ul>
          ${data.warnings.slice(0, 100).map(x =>
            `<li>${escapeHtml(x)}</li>`
          ).join("")}
        </ul>
        ${data.warnings.length > 100 ? "<p>Showing the first 100 notices.</p>" : ""}
      </details>`;
  }

  $("notices").innerHTML = notices;
  $("refresh").disabled = snapshot.isScanning;
  $("refresh").textContent =
    snapshot.isScanning ? "↻ Scanning..." : "↻ Refresh inventory";

  $("scan-status").textContent =
    snapshot.isScanning ? "Checking production and GitHub" : "Automatic background scanning enabled";

  $("updated").textContent = data.observedAtUtc
    ? `Inventory observed: ${new Date(data.observedAtUtc).toLocaleString()}`
    : "Waiting for first production observation…";
}

async function load() {
  if (loading) return;
  loading = true;

  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");

    snapshot = await response.json();
    render();
  } catch {
    $("notices").innerHTML = `
      <div class="notice error">
        Cannot reach the backend. Displayed results may be outdated.
        Check that the .NET application is running.
      </div>`;

    $("scan-status").textContent = "Backend unavailable";
    $("refresh").disabled = false;
  } finally {
    loading = false;
  }
}

$("search").addEventListener("input", renderRows);
$("filter").addEventListener("change", renderRows);

$("rows").addEventListener("click", event => {
  const button = event.target.closest("button[data-repo]");
  if (!button) return;

  const repo = button.dataset.repo;

  if (expanded.has(repo)) expanded.delete(repo);
  else expanded.add(repo);

  renderRows();
});

$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;
  $("action-message").textContent = "";

  try {
    const response = await fetch("/api/scan", { method: "POST" });
    const body = await response.json();

    $("action-message").textContent = body.message || "Refresh requested.";
  } catch {
    $("action-message").textContent = "Could not reach the refresh API.";
  } finally {
    await load();
    setTimeout(() => { $("action-message").textContent = ""; }, 5000);
  }
});

load();
setInterval(load, 10000);
