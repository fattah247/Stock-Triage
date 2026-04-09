(function () {
  const bootstrap = window.__DASHBOARD_BOOTSTRAP__ || {};
  let data = bootstrap.payload || {
    issuers: [],
    documents: [],
    metrics: [],
    findings: [],
    revisions: [],
    inbox_rows: [],
    backlog_rows: [],
    canary_rows: [],
    action_history: [],
  };

  const state = {
    selectedTicker: null,
    activeJobId: null,
    activeJobStartedAt: null,
    pollingHandle: null,
    eventSource: null,
    queuedActions: [],
    queueName: null,
    queueIndex: 0,
    queueTotal: 0,
    highlightedKeys: { inbox: new Map(), documents: new Map(), findings: new Map() },
    lastFailedHistory: null,
  };

  const actionEndpoint = bootstrap.actionEndpoint || null;
  const snapshotEndpoint = bootstrap.snapshotEndpoint || null;
  const artifactEndpoint = bootstrap.artifactEndpoint || null;

  const search = document.getElementById("issuerSearch");
  const issuerList = document.getElementById("issuerList");
  const companyOverview = document.getElementById("companyOverview");
  const companyStats = document.getElementById("companyStats");
  const documentList = document.getElementById("documentList");
  const metricList = document.getElementById("metricList");
  const findingList = document.getElementById("findingList");
  const revisionList = document.getElementById("revisionList");
  const backlogList = document.getElementById("backlogList");
  const canaryList = document.getElementById("canaryList");
  const inboxList = document.getElementById("inboxList");
  const actionStatus = document.getElementById("actionStatus");
  const actionOutput = document.getElementById("actionOutput");
  const actionHistory = document.getElementById("actionHistory");
  const actionErrorPanel = document.getElementById("actionErrorPanel");
  const actionErrorTitle = document.getElementById("actionErrorTitle");
  const actionErrorSummary = document.getElementById("actionErrorSummary");
  const actionErrorDetails = document.getElementById("actionErrorDetails");
  const copyCommandButton = document.getElementById("copyCommandButton");
  const retryActionButton = document.getElementById("retryActionButton");
  const yearInput = document.getElementById("financialYear");
  const limitInput = document.getElementById("stageLimit");
  const refreshButton = document.getElementById("refreshButton");
  const opsShell = document.getElementById("opsShell");
  const issuerCount = document.getElementById("issuerCount");
  const documentCount = document.getElementById("documentCount");
  const findingCount = document.getElementById("findingCount");
  const canaryStatus = document.getElementById("canaryStatus");
  const lastActivity = document.getElementById("lastActivity");
  const actionButtons = Array.from(document.querySelectorAll(".actionButton"));

  const actionLabels = {
    daily_sweep: "Daily sweep",
    discover_company: "Discover companies",
    discover_disclosure: "Discover disclosures",
    discover_financial: "Discover financials",
    download_financial: "Download financials",
    download_disclosure: "Download disclosures",
    parse: "Parse",
    extract: "Extract",
    rules: "Run rules",
    alerts: "Alerts",
    canary: "Canary",
    export: "Export snapshot",
    summary: "Summarize",
    parse_one: "Re-parse document",
    extract_one: "Re-extract document",
    rules_one: "Run rules for document",
  };

  if (opsShell && actionEndpoint && snapshotEndpoint) {
    opsShell.hidden = false;
  }

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function localHref(path) {
    if (!path) return "";
    if (!artifactEndpoint) return esc(path);
    return `${artifactEndpoint}?path=${encodeURIComponent(path)}`;
  }

  function actionLabel(action) {
    return actionLabels[action] || String(action || "").replaceAll("_", " ");
  }

  function absoluteTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZoneName: "short",
    }).format(date);
  }

  function relativeTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const diffMs = date.getTime() - Date.now();
    const diffSeconds = Math.round(diffMs / 1000);
    const ranges = [
      { unit: "day", seconds: 86400 },
      { unit: "hour", seconds: 3600 },
      { unit: "minute", seconds: 60 },
      { unit: "second", seconds: 1 },
    ];
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    for (const range of ranges) {
      if (Math.abs(diffSeconds) >= range.seconds || range.unit === "second") {
        return formatter.format(Math.round(diffSeconds / range.seconds), range.unit);
      }
    }
    return absoluteTime(value);
  }

  function renderTimestamp(value) {
    if (!value) return "";
    return `<span class="timeLabel" title="${esc(absoluteTime(value))}">${esc(relativeTime(value))}</span>`;
  }

  function updateHighlights(previous, next) {
    if (!previous) return;
    const now = Date.now();
    const groups = [
      ["inbox", previous.inbox_rows || [], next.inbox_rows || [], inboxKey],
      ["documents", previous.documents || [], next.documents || [], documentKey],
      ["findings", previous.findings || [], next.findings || [], findingKey],
    ];
    groups.forEach(([name, prevRows, nextRows, keyFn]) => {
      const prevKeys = new Set(prevRows.map(keyFn));
      const bucket = state.highlightedKeys[name];
      nextRows.forEach((item) => {
        const key = keyFn(item);
        if (!prevKeys.has(key)) {
          bucket.set(key, now + 12000);
        }
      });
      for (const [key, expiresAt] of bucket.entries()) {
        if (expiresAt <= now) bucket.delete(key);
      }
    });
  }

  function hasHighlight(group, key) {
    const expiresAt = state.highlightedKeys[group]?.get(key);
    if (!expiresAt) return false;
    if (expiresAt <= Date.now()) {
      state.highlightedKeys[group].delete(key);
      return false;
    }
    return true;
  }

  function inboxKey(item) {
    return [item.kind, item.document_id || "", item.ticker || "", item.happened_at || "", item.title || ""].join("|");
  }

  function documentKey(item) {
    return String(item.document_id || `${item.source_name || ""}|${item.source_record_id || ""}`);
  }

  function findingKey(item) {
    return [item.document_id || "", item.finding_type || "", item.name || "", item.claim || "", item.finished_at || ""].join("|");
  }

  function normalizeSeverity(value, fallback = "info") {
    const level = String(value || "").toLowerCase();
    if (["critical", "warning", "info", "ok"].includes(level)) return level;
    return fallback;
  }

  function findingSeverity(item) {
    const level = normalizeSeverity(item?.name, "");
    if (level) return level;
    if (item?.finding_type === "risk") return "warning";
    if (item?.finding_type === "event") return "info";
    return "warning";
  }

  function ensureSelection(visibleIssuers) {
    if (visibleIssuers.length === 0) {
      state.selectedTicker = null;
      return;
    }
    if (!state.selectedTicker || !visibleIssuers.some((item) => item.ticker === state.selectedTicker)) {
      state.selectedTicker = visibleIssuers[0].ticker;
    }
  }

  function groupBy(items, keyFn) {
    const buckets = new Map();
    items.forEach((item) => {
      const key = keyFn(item);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(item);
    });
    return buckets;
  }

  function renderCounts() {
    if (issuerCount) issuerCount.textContent = String(data.issuers.length);
    if (documentCount) documentCount.textContent = String(data.documents.length);
    if (findingCount) findingCount.textContent = String(data.findings.length);
    if (canaryStatus) {
      const failed = (data.canary_rows || []).filter((item) => item.status === "failed").length;
      const drifted = (data.canary_rows || []).filter((item) => item.drifted).length;
      const level = failed ? "critical" : drifted ? "warning" : "ok";
      canaryStatus.textContent = failed ? `${failed} failed` : drifted ? `${drifted} drifted` : "ok";
      canaryStatus.className = `statusPill sev-${level}`;
    }
    if (lastActivity) {
      const firstInbox = (data.inbox_rows || [])[0];
      lastActivity.textContent = firstInbox
        ? `${firstInbox.kind} · ${firstInbox.ticker || firstInbox.source_name || "system"} · ${relativeTime(firstInbox.happened_at)}`
        : "No recent inbox activity";
      if (firstInbox?.happened_at) {
        lastActivity.title = absoluteTime(firstInbox.happened_at);
      }
    }
  }

  function renderHistory() {
    if (!actionHistory) return;
    const rows = (data.action_history || []).slice(0, 10);
    state.lastFailedHistory = rows.find((item) => item.ok === false) || null;
    actionHistory.innerHTML = rows.length
      ? rows
          .map(
            (item) =>
              `<div class="historyItem ${item.ok === false ? "failed" : ""}"><div><strong>${esc(actionLabel(item.action))}</strong> <span class="meta">${renderTimestamp(
                item.timestamp
              )}</span></div><div class="meta">${esc((item.command || []).join(" "))}</div><div class="meta">exit=${esc(
                item.returncode
              )}${item.duration_ms ? ` · ${esc(Math.max(1, Math.round(item.duration_ms / 1000)))}s` : ""}</div></div>`
          )
          .join("")
      : `<div class="meta">No action has been run from this dashboard yet.</div>`;
    renderActionError();
  }

  function renderActionError() {
    if (!actionErrorPanel || !actionErrorTitle || !actionErrorSummary || !actionErrorDetails) return;
    const item = state.lastFailedHistory;
    if (!item) {
      actionErrorPanel.hidden = true;
      return;
    }
    const stderrLines = String(item.stderr || "").trim().split("\n").filter(Boolean);
    const stdoutLines = String(item.stdout || "").trim().split("\n").filter(Boolean);
    const detailLines = (stderrLines.length ? stderrLines : stdoutLines).slice(-20);
    actionErrorTitle.textContent = `${actionLabel(item.action)} failed`;
    actionErrorSummary.innerHTML = `${renderTimestamp(item.timestamp)} · exit ${esc(item.returncode)} · ${esc(
      (item.command || []).join(" ")
    )}`;
    actionErrorDetails.textContent = detailLines.join("\n") || "No stderr was captured for this failure.";
    actionErrorPanel.hidden = false;
  }

  function setActionButtonsDisabled(disabled) {
    actionButtons.forEach((button) => {
      button.disabled = disabled;
      button.style.opacity = disabled ? "0.65" : "";
      button.style.pointerEvents = disabled ? "none" : "";
    });
  }

  function stopPolling() {
    if (state.pollingHandle) {
      window.clearInterval(state.pollingHandle);
      state.pollingHandle = null;
    }
  }

  function startPolling() {
    stopPolling();
    if (!snapshotEndpoint) return;
    state.pollingHandle = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (state.activeJobId) return;
      refreshSnapshot().catch(() => {});
    }, 15000);
  }

  function render() {
    renderCounts();
    if (yearInput && !yearInput.value) {
      yearInput.value = String(new Date().getFullYear());
    }

    const q = (search?.value || "").trim().toLowerCase();
    const issuers = data.issuers.filter(
      (item) => !q || [item.ticker, item.issuer_name, item.sector, item.subsector].join(" ").toLowerCase().includes(q)
    );
    ensureSelection(issuers);

    const selectedIssuer = issuers.find((item) => item.ticker === state.selectedTicker) || null;
    const selectedTicker = selectedIssuer?.ticker || null;
    const docs = data.documents
      .filter((item) => (selectedTicker ? item.ticker === selectedTicker : false))
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    const metrics = data.metrics
      .filter((item) => (selectedTicker ? item.ticker === selectedTicker : false))
      .sort((a, b) => String(a.metric_name || "").localeCompare(String(b.metric_name || "")));
    const findings = data.findings
      .filter((item) => (selectedTicker ? item.ticker === selectedTicker : false))
      .sort((a, b) => String(b.finished_at || "").localeCompare(String(a.finished_at || "")));
    const revisions = data.revisions
      .filter((item) => (selectedTicker ? item.ticker === selectedTicker : false))
      .sort((a, b) => String(b.latest_revision_at || "").localeCompare(String(a.latest_revision_at || "")));
    const inboxRows = (data.inbox_rows || []).slice(0, 20);
    const backlogRows = data.backlog_rows || [];
    const canaryRows = data.canary_rows || [];

    issuerList.innerHTML = issuers.length
      ? issuers
          .map(
            (item) => `
              <button class="issuerCard ${item.ticker === state.selectedTicker ? "active" : ""}" type="button" data-ticker="${esc(
                item.ticker
              )}">
                <div class="issuerLine">
                  <div class="issuerTicker">${esc(item.ticker)}</div>
                  <div class="issuerCountBadge">${esc(item.document_count || 0)}</div>
                </div>
                <div class="issuerName">${esc(item.issuer_name)}</div>
                <div class="issuerMeta">
                  <span>${esc(item.sector || "unknown")}</span>
                  <span>${esc(item.latest_document_at || item.last_seen_at || "")}</span>
                </div>
              </button>
            `
          )
          .join("")
      : `<div class="emptyState">No issuer matches this search.</div>`;

    if (inboxList) {
      inboxList.className = "inboxList";
      inboxList.innerHTML = inboxRows.length
        ? inboxRows
            .map(
              (item) => `
                <article class="inboxItem ${hasHighlight("inbox", inboxKey(item)) ? "freshRow" : ""}">
                  <div class="inboxKind sev-${esc(item.severity || "info")}">${esc(item.kind || "update")}</div>
                  <div>
                    <div class="inboxTitle">${esc(item.title || item.kind || "Update")}</div>
                    <div class="inboxSummary">${esc(item.summary || "")}</div>
                    <div class="inboxMeta">
                      ${
                        item.ticker
                          ? `<span class="chip">${esc(item.ticker)}</span>`
                          : item.source_name
                            ? `<span class="chip">${esc(item.source_name)}</span>`
                            : ""
                      }
                      ${item.happened_at ? `<span class="chip">${renderTimestamp(item.happened_at)}</span>` : ""}
                    </div>
                  </div>
                  <div class="inboxActions">
                    ${
                      item.ticker
                        ? `<button class="ghostButton miniButton" type="button" data-focus-ticker="${esc(item.ticker)}">focus issuer</button>`
                        : ""
                    }
                    ${item.local_path ? `<a class="chip" href="${localHref(item.local_path)}">open file</a>` : ""}
                    ${item.original_url ? `<a class="chip" href="${esc(item.original_url)}">origin</a>` : ""}
                  </div>
                </article>
              `
            )
            .join("")
        : `<div class="emptyState">No recent cross-issuer activity yet. Run discovery or process a few documents to populate this inbox.</div>`;
    }

    if (!selectedIssuer) {
      companyOverview.innerHTML =
        `<div class="emptyState"><strong>Select a company.</strong> Use the left rail or click "focus issuer" from the inbox to move the workspace onto one company.</div>`;
      companyStats.innerHTML = "";
      documentList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Pick one issuer to review grouped filings here.</div>`;
      metricList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Metrics will appear once the workspace is focused on an issuer.</div>`;
      revisionList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Revision history is shown per issuer.</div>`;
      findingList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Findings and summaries are scoped to the active issuer.</div>`;
    } else {
      const analyzedCount = docs.filter((item) => item.process_state === "analyzed").length;
      const failedCount = docs.filter((item) => item.process_state === "failed").length;
      const groupedDocs = groupBy(docs, (item) => item.source_name || "unknown");

      companyOverview.innerHTML = `
        <div class="sectionEyebrow">Selected Company</div>
        <h2 class="focusTitle">${esc(selectedIssuer.issuer_name)}</h2>
        <div class="focusSubtitle">${esc(
          selectedIssuer.ticker
        )} is the active focus. Documents are grouped below by source so you can move from recent inbox activity into a single issuer without re-scanning the rest of the market.</div>
        <div class="heroBar">
          <span class="chip">${esc(selectedIssuer.sector || "unknown")} / ${esc(selectedIssuer.subsector || "unknown")}</span>
          <span class="chip">Last seen ${renderTimestamp(selectedIssuer.last_seen_at || "")}</span>
          <span class="chip">Latest filing ${renderTimestamp(selectedIssuer.latest_document_at || "")}</span>
        </div>
      `;
      companyStats.innerHTML = `
        <div class="stat"><div class="summaryLabel">Documents</div><strong>${docs.length}</strong></div>
        <div class="stat"><div class="summaryLabel">Findings</div><strong>${findings.length}</strong></div>
        <div class="stat"><div class="summaryLabel">Analyzed</div><strong>${analyzedCount}</strong></div>
        <div class="stat"><div class="summaryLabel">Failed</div><strong>${failedCount}</strong></div>
      `;
      documentList.innerHTML = docs.length
        ? `<div class="docGroups">${Array.from(groupedDocs.entries())
            .map(
              ([groupName, items]) => `
                <section class="docGroup">
                  <div class="docGroupHeader">
                    <div>
                      <h3>${esc(groupName)}</h3>
                      <div class="meta">${items.length} filing${items.length === 1 ? "" : "s"} for ${esc(
                selectedIssuer.ticker
              )}</div>
                    </div>
                  </div>
                  <div class="docList">${items
                    .map(
                      (item) => `
                        <article class="docItem ${hasHighlight("documents", documentKey(item)) ? "freshRow" : ""}">
                          <div>
                            <div class="docTitle">${esc(item.document_type || "document")}<span class="meta"> · ${esc(
                        item.source_record_id || ""
                      )}</span></div>
                            <div class="meta">${renderTimestamp(item.updated_at || item.retrieved_at || "")}</div>
                            <div class="tagRow">
                              <span class="tag">${esc(item.process_state || "unknown")}</span>
                              <span class="tag">${esc(item.triggered_rules || 0)} rules</span>
                              ${
                                item.version_group_id
                                  ? `<span class="tag">rev ${esc(item.version_group_id)}</span>`
                                  : ""
                              }
                              ${
                                item.process_state === "failed"
                                  ? `<span class="tag tagWarn">needs retry</span>`
                                  : ""
                              }
                            </div>
                          </div>
                          <div class="docActions">
                            ${item.local_path ? `<a class="chip" href="${localHref(item.local_path)}">local</a>` : ""}
                            ${item.original_url ? `<a class="chip" href="${esc(item.original_url)}">origin</a>` : ""}
                            ${
                              actionEndpoint && item.document_id
                                ? `<button class="miniButton" type="button" data-document-id="${esc(
                                    item.document_id
                                  )}" data-inline-action="summary">summarize</button>`
                                : ""
                            }
                            ${
                              actionEndpoint && item.document_id
                                ? `<button class="miniButton ghostButton" type="button" data-document-id="${esc(
                                    item.document_id
                                  )}" data-inline-action="parse_one">re-parse</button>`
                                : ""
                            }
                            ${
                              actionEndpoint && item.document_id
                                ? `<button class="miniButton ghostButton" type="button" data-document-id="${esc(
                                    item.document_id
                                  )}" data-inline-action="extract_one">re-extract</button>`
                                : ""
                            }
                            ${
                              actionEndpoint && item.document_id
                                ? `<button class="miniButton ghostButton" type="button" data-document-id="${esc(
                                    item.document_id
                                  )}" data-inline-action="rules_one">run rules</button>`
                                : ""
                            }
                          </div>
                        </article>
                      `
                    )
                    .join("")}</div>
                </section>
              `
            )
            .join("")}</div>`
        : `<div class="emptyState"><strong>No persisted documents yet.</strong> Run discovery and download for this issuer, then the grouped filings will appear here.</div>`;
      metricList.innerHTML = metrics.length
        ? `<table class="metricTable"><thead><tr><th>Metric</th><th>Value</th><th>Period</th></tr></thead><tbody>${metrics
            .slice(0, 80)
            .map(
              (item) =>
                `<tr><td>${esc(item.metric_name)}</td><td>${esc(item.value_normalized)} ${esc(
                  item.currency || ""
                )}</td><td>${esc(item.fiscal_year || "")} ${esc(item.fiscal_period || "")}</td></tr>`
            )
            .join("")}</tbody></table>`
        : `<div class="emptyState"><strong>No normalized metrics yet.</strong> Run parse and extract to populate the fact layer for this issuer.</div>`;
      revisionList.innerHTML = revisions.length
        ? `<table class="metricTable"><thead><tr><th>Source Record</th><th>Revisions</th><th>Latest</th></tr></thead><tbody>${revisions
            .map(
              (item) =>
                `<tr><td>${esc(item.source_record_id)}</td><td>${esc(item.revision_count)}</td><td>${renderTimestamp(
                  item.latest_revision_at
                )}</td></tr>`
            )
            .join("")}</tbody></table>`
        : `<div class="emptyState"><strong>No revision groups yet.</strong> Version history appears once multiple files or revisions exist for the issuer.</div>`;
      findingList.innerHTML = findings.length
        ? `<div class="findingStack">${findings
            .slice(0, 80)
            .map(
              (item) => `
                <article class="findingCard sev-${findingSeverity(item)} ${hasHighlight("findings", findingKey(item)) ? "freshRow" : ""}">
                  <div><span class="severity sev-${findingSeverity(item)}">${esc(item.finding_type)}:${esc(item.name)}</span></div>
                  <div class="findingClaim">${esc(item.claim)}</div>
                  <div class="meta">${esc(item.summary)}</div>
                  <div class="meta">${renderTimestamp(item.finished_at)}</div>
                  <div class="meta">${esc(item.evidence)}</div>
                  <div class="heroBar">
                    ${item.local_path ? `<a class="chip" href="${localHref(item.local_path)}">attachment</a>` : ""}
                    ${item.original_url ? `<a class="chip" href="${esc(item.original_url)}">origin</a>` : ""}
                  </div>
                </article>
              `
            )
            .join("")}</div>`
        : `<div class="emptyState"><strong>No findings yet.</strong> Run rules or summary generation after parsing and extraction to surface issuer-level signals here.</div>`;
    }

    backlogList.innerHTML = backlogRows.length
      ? `<table class="systemTable"><thead><tr><th>Source</th><th>Discovered</th><th>Downloaded</th><th>Parsed</th><th>Extracted</th><th>Analyzed</th><th>Failed</th></tr></thead><tbody>${backlogRows
          .map(
            (item) =>
              `<tr><td>${esc(item.source_name)}</td><td>${esc(item.discovered)}</td><td>${esc(item.downloaded)}</td><td>${esc(
                item.parsed
              )}</td><td>${esc(item.extracted)}</td><td>${esc(item.analyzed)}</td><td>${esc(item.failed)}</td></tr>`
          )
          .join("")}</tbody></table>`
      : `<div class="emptyState"><strong>No backlog snapshot yet.</strong> Run discovery or processing actions to build the queue summary.</div>`;
    canaryList.innerHTML = canaryRows.length
      ? `<table class="systemTable"><thead><tr><th>Source</th><th>Status</th><th>Accessible</th><th>Drifted</th><th>Finished</th></tr></thead><tbody>${canaryRows
          .map((item) => {
            const level = item.status === "failed" ? "critical" : item.drifted ? "warning" : "ok";
            return `<tr><td>${esc(item.source_name)}</td><td><span class="statusPill sev-${level}">${esc(
              item.status
            )}</span></td><td>${esc(item.accessible)}</td><td>${esc(item.drifted)}</td><td>${renderTimestamp(item.finished_at)}</td></tr>`;
          })
          .join("")}</tbody></table>`
      : `<div class="emptyState"><strong>No canary checks yet.</strong> Run canary from the control panel when you want a fresh source-health readout.</div>`;
    renderHistory();
  }

  async function refreshSnapshot() {
    if (!snapshotEndpoint) return;
    const response = await fetch(snapshotEndpoint);
    if (!response.ok) {
      throw new Error(`Snapshot refresh failed: ${response.status}`);
    }
    const nextData = await response.json();
    updateHighlights(data, nextData);
    data = nextData;
    render();
  }

  function isSequenceActive() {
    return Boolean(state.queueName);
  }

  function startDailySweep() {
    if (state.activeJobId || isSequenceActive()) return;
    state.queueName = "Daily sweep";
    state.queuedActions = [
      "discover_disclosure",
      "discover_financial",
      "download_financial",
      "download_disclosure",
      "parse",
      "extract",
      "rules",
      "alerts",
    ];
    state.queueIndex = 0;
    state.queueTotal = state.queuedActions.length;
    if (actionOutput) {
      actionOutput.textContent = "Starting daily sweep.\n";
    }
    if (actionErrorPanel) {
      actionErrorPanel.hidden = true;
    }
    stopPolling();
    setActionButtonsDisabled(true);
    runNextQueuedAction();
  }

  function runNextQueuedAction() {
    const nextAction = state.queuedActions.shift();
    if (!nextAction) return;
    state.queueIndex += 1;
    runAction(nextAction, {}, { preserveOutput: true, disableControls: false }).catch((error) => {
      if (actionStatus) actionStatus.textContent = error.message;
      state.queueName = null;
      state.queueIndex = 0;
      state.queueTotal = 0;
      state.queuedActions = [];
      setActionButtonsDisabled(false);
      startPolling();
    });
  }

  async function runAction(action, overrides = {}, options = {}) {
    if (!actionEndpoint) return;
    if (state.activeJobId) return;
    if (action === "daily_sweep") {
      startDailySweep();
      return;
    }
    const params = new URLSearchParams();
    params.set("action", action);
    params.set("year", yearInput?.value || "");
    params.set("limit", limitInput?.value || "");
    params.set("document_id", "");
    Object.entries(overrides).forEach(([key, value]) => params.set(key, String(value ?? "")));

    if (actionStatus) {
      if (isSequenceActive()) {
        actionStatus.textContent = `${state.queueName} ${state.queueIndex}/${state.queueTotal}: ${actionLabel(action)}...`;
      } else {
        actionStatus.textContent = `Running ${actionLabel(action)}...`;
      }
    }
    if (actionOutput && !options.preserveOutput) {
      actionOutput.textContent = "";
    }
    if (actionErrorPanel) {
      actionErrorPanel.hidden = true;
    }
    if (actionOutput && options.preserveOutput) {
      actionOutput.textContent += `\n== ${actionLabel(action)} ==\n`;
    }
    if (options.disableControls !== false) {
      stopPolling();
      setActionButtonsDisabled(true);
    }
    const response = await fetch(actionEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body: params.toString(),
    });
    const result = await response.json();
    if (!response.ok && response.status !== 202) {
      if (options.disableControls !== false) {
        setActionButtonsDisabled(false);
        startPolling();
      }
      throw new Error(result.error || `Action start failed: ${response.status}`);
    }
    state.activeJobId = result.job_id;
    state.activeJobStartedAt = Date.now();
    connectJobStream(action, result.stream_url, options);
  }

  function connectJobStream(action, streamUrl, options = {}) {
    if (!streamUrl) {
      finishActiveJob(action, false, 1, 0, "Missing stream URL.");
      return;
    }

    if (state.eventSource) {
      state.eventSource.close();
    }
    const source = new EventSource(streamUrl);
    state.eventSource = source;
    let elapsedHandle = window.setInterval(() => {
      if (!actionStatus || !state.activeJobStartedAt) return;
      const elapsed = Math.max(0, Math.floor((Date.now() - state.activeJobStartedAt) / 1000));
      if (isSequenceActive()) {
        actionStatus.textContent = `${state.queueName} ${state.queueIndex}/${state.queueTotal}: ${actionLabel(action)}... ${elapsed}s`;
      } else {
        actionStatus.textContent = `Running ${actionLabel(action)}... ${elapsed}s`;
      }
    }, 1000);

    source.addEventListener("started", (event) => {
      const payload = JSON.parse(event.data);
      if (actionOutput) {
        if (options.preserveOutput) {
          actionOutput.textContent += `${(payload.command || []).join(" ")}\n\n`;
        } else {
          actionOutput.textContent = `${(payload.command || []).join(" ")}\n\n`;
        }
      }
    });

    source.addEventListener("stdout", (event) => {
      const payload = JSON.parse(event.data);
      if (actionOutput) {
        actionOutput.textContent += payload.text || "";
        actionOutput.scrollTop = actionOutput.scrollHeight;
      }
    });

    source.addEventListener("stderr", (event) => {
      const payload = JSON.parse(event.data);
      if (actionOutput) {
        actionOutput.textContent += payload.text || "";
        actionOutput.scrollTop = actionOutput.scrollHeight;
      }
    });

    source.addEventListener("complete", (event) => {
      const payload = JSON.parse(event.data);
      window.clearInterval(elapsedHandle);
      source.close();
      state.eventSource = null;
      finishActiveJob(action, payload.ok, payload.returncode, payload.duration_ms);
    });

    source.onerror = () => {
      window.clearInterval(elapsedHandle);
      source.close();
      state.eventSource = null;
      finishActiveJob(action, false, 1, 0, "Stream disconnected.");
    };
  }

  function finishActiveJob(action, ok, returncode, durationMs, fallbackMessage) {
    state.activeJobId = null;
    state.activeJobStartedAt = null;
    const sequenceActive = isSequenceActive();
    refreshSnapshot()
      .catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      })
      .finally(() => {
        if (sequenceActive) {
          if (ok && state.queuedActions.length) {
            runNextQueuedAction();
            return;
          }
          if (actionStatus) {
            if (fallbackMessage) {
              actionStatus.textContent = fallbackMessage;
            } else if (ok) {
              actionStatus.textContent = `${state.queueName} finished in ${Math.max(1, Math.round((durationMs || 0) / 1000))}s for the final step.`;
            } else {
              actionStatus.textContent = `${state.queueName} stopped during ${actionLabel(action)} (exit ${returncode}).`;
            }
          }
          state.queueName = null;
          state.queueIndex = 0;
          state.queueTotal = 0;
          state.queuedActions = [];
          setActionButtonsDisabled(false);
          startPolling();
          return;
        }

        setActionButtonsDisabled(false);
        if (actionStatus) {
          if (fallbackMessage) {
            actionStatus.textContent = fallbackMessage;
          } else {
            actionStatus.textContent = ok
              ? `${actionLabel(action)} finished in ${Math.max(1, Math.round((durationMs || 0) / 1000))}s.`
              : `${actionLabel(action)} failed (exit ${returncode}).`;
          }
        }
        startPolling();
      });
  }

  if (search) {
    search.addEventListener("input", render);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !state.activeJobId) {
      refreshSnapshot().catch(() => {});
      startPolling();
    } else {
      stopPolling();
    }
  });
  if (refreshButton) {
    refreshButton.addEventListener("click", () =>
      refreshSnapshot().catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      })
    );
  }

  if (copyCommandButton) {
    copyCommandButton.addEventListener("click", async () => {
      const item = state.lastFailedHistory;
      if (!item) return;
      try {
        await navigator.clipboard.writeText((item.command || []).join(" "));
        if (actionStatus) actionStatus.textContent = "Copied failing command.";
      } catch {
        if (actionStatus) actionStatus.textContent = "Clipboard copy failed.";
      }
    });
  }

  if (retryActionButton) {
    retryActionButton.addEventListener("click", () => {
      const item = state.lastFailedHistory;
      if (!item) return;
      runAction(item.action, item.params || {}).catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      });
    });
  }

  document.querySelectorAll(".actionButton").forEach((button) => {
    button.addEventListener("click", () => {
      runAction(button.dataset.action).catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      });
    });
  });

  document.addEventListener("click", (event) => {
    const issuerTarget = event.target.closest("[data-ticker]");
    if (issuerTarget) {
      state.selectedTicker = issuerTarget.dataset.ticker || null;
      render();
      return;
    }

    const focusTarget = event.target.closest("[data-focus-ticker]");
    if (focusTarget) {
      state.selectedTicker = focusTarget.dataset.focusTicker || null;
      render();
      companyOverview?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const target = event.target.closest("[data-inline-action]");
    if (!target) return;
    const documentId = target.dataset.documentId || "";
    runAction(target.dataset.inlineAction, { document_id: documentId }).catch((error) => {
      if (actionStatus) actionStatus.textContent = error.message;
    });
  });

  startPolling();
  render();
})();
