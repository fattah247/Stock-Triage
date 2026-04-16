(function () {
  const bootstrap = window.__DASHBOARD_BOOTSTRAP__ || {};
  let data = bootstrap.payload || {
    issuers: [],
    market_monitor: {},
    documents: [],
    metrics: [],
    findings: [],
    revisions: [],
    company_snapshots: [],
    sector_rollups: [],
    inbox_rows: [],
    digest_rows: [],
    review_rows: [],
    intelligence_kpis: {},
    backlog_rows: [],
    canary_rows: [],
    source_sync_rows: [],
    action_history: [],
    watchlist: [],
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
    _companyCache: {},        // ticker -> {data, fetchedAt}
    _sectionLoaded: {},       // section -> boolean
    _sectionLoading: {},      // section -> boolean (in-flight guard)
    activeView: "home",       // current view tab
  };

  const actionEndpoint = bootstrap.actionEndpoint || null;
  const snapshotEndpoint = bootstrap.snapshotEndpoint || null;
  const artifactEndpoint = bootstrap.artifactEndpoint || null;
  const reviewEndpoint = "/api/review";
  const companyOverrideEndpoint = "/api/company-override";

  const search = document.getElementById("issuerSearch");
  const viewEyebrow = document.getElementById("viewEyebrow");
  const viewTitle = document.getElementById("viewTitle");
  const viewLead = document.getElementById("viewLead");
  const selectedIssuerCard = document.getElementById("selectedIssuerCard");
  const issuerList = document.getElementById("issuerList");
  const companyOverview = document.getElementById("companyOverview");
  const companyStats = document.getElementById("companyStats");
  const marketMonitor = document.getElementById("marketMonitor");
  const documentList = document.getElementById("documentList");
  const metricList = document.getElementById("metricList");
  const findingList = document.getElementById("findingList");
  const revisionList = document.getElementById("revisionList");
  const snapshotList = document.getElementById("snapshotList");
  const claimsList = document.getElementById("claimsList");
  const sectorExplorer = document.getElementById("sectorExplorer");
  const digestList = document.getElementById("digestList");
  const reviewList = document.getElementById("reviewList");
  const kpiList = document.getElementById("kpiList");
  const watchlistGrid = document.getElementById("watchlistGrid");
  const backlogList = document.getElementById("backlogList");
  const canaryList = document.getElementById("canaryList");
  const syncStatusList = document.getElementById("syncStatusList");
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
    digest: "Build digest",
    canary: "Canary",
    export: "Export snapshot",
    summary: "Summarize",
    parse_one: "Re-parse document",
    extract_one: "Re-extract document",
    rules_one: "Run rules for document",
  };

  // Ops shell visibility is now managed by the view system (data-views="ops").

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
    // Safari is stricter than Chromium here and rejects some combinations of
    // `dateStyle`/`timeStyle` with `timeZoneName`. Fall back progressively so
    // the whole dashboard does not crash during first render.
    const candidates = [
      { dateStyle: "medium", timeStyle: "short", timeZoneName: "short" },
      { dateStyle: "medium", timeStyle: "short" },
      { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" },
    ];
    for (const options of candidates) {
      try {
        return new Intl.DateTimeFormat(undefined, options).format(date);
      } catch (_) {
        // Try the next supported formatter shape.
      }
    }
    return date.toLocaleString();
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

  function monthLabel(month) {
    const names = [
      "Jan", "Feb", "Mar", "Apr", "May", "Jun",
      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    const idx = Number(month) - 1;
    return Number.isInteger(idx) && idx >= 0 && idx < names.length ? names[idx] : String(month || "12");
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

  function statusTag(value) {
    const level = String(value || "").toLowerCase();
    if (["critical", "broken", "failed", "high", "avoid"].includes(level)) return "critical";
    if (["warning", "review_required", "weakening", "open", "snoozed", "medium", "review manually", "watch"].includes(level)) return "warning";
    if (["improving", "ok", "resolved", "stable", "holding", "low", "buy"].includes(level)) return "ok";
    return "info";
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

  function renderSelectedIssuerCard(selectedIssuer, companySnapshot) {
    if (!selectedIssuerCard) return;
    if (!selectedIssuer) {
      selectedIssuerCard.innerHTML = `
        <div class="selectedIssuerEmpty">
          <div class="summaryLabel">Selected Company</div>
          <strong>None yet</strong>
          <div class="meta">Pick one from the left rail or from the inbox to open the company workspace.</div>
        </div>
      `;
      return;
    }

    const snapshotPayload = companySnapshot?.payload || {};
    const topCard = snapshotPayload.top_card || {};
    const reasons = (topCard.reasons || []).slice(0, 2);
    selectedIssuerCard.innerHTML = `
      <div class="selectedIssuerTop">
        <div>
          <div class="summaryLabel">Selected Company</div>
          <strong>${esc(selectedIssuer.ticker)} · ${esc(selectedIssuer.issuer_name || "")}</strong>
        </div>
        <button class="ghostButton miniButton" data-view="company" type="button">Open company</button>
      </div>
      <div class="selectedIssuerMeta">
        <span class="chip">${esc(selectedIssuer.sector || "unknown")} / ${esc(selectedIssuer.subsector || "unknown")}</span>
        ${
          topCard.decision_state
            ? `<span class="chip">Decision ${esc(topCard.decision_state)}</span>`
            : `<span class="chip">No current decision snapshot</span>`
        }
      </div>
      <div class="meta">
        ${
          reasons.length
            ? reasons.map(esc).join(" · ")
            : "Use Company view to inspect filings, findings, claims, and thesis state for this issuer."
        }
      </div>
    `;
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

  function switchView(view) {
    state.activeView = view;
    document.body.dataset.activeView = view;
    // Update tab highlight.
    document.querySelectorAll(".viewTab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.view === view);
    });
    // Show/hide panels based on data-views attribute.
    document.querySelectorAll("[data-views]").forEach((el) => {
      const views = el.dataset.views.split(/\s+/);
      el.classList.toggle("viewVisible", views.includes(view));
    });
    // Auto-select company view when an issuer is picked.
    if (view === "company" && state.selectedTicker) {
      fetchCompanyDetail(state.selectedTicker);
    }
    // Sidebar hint adapts per view.
    const hint = document.getElementById("sidebarHint");
    if (hint) {
      const hints = {
        home: "Start with recent changes, then open one issuer.",
        watchlist: "Star tickers here, then use this view to stay inside your watchlist.",
        company: "Everything below is scoped to the selected issuer only.",
        sector: "Compare the selected issuer against its peers and sector context.",
        review: "Resolve or snooze the human queue here.",
        ops: "Pipeline control panel, backlog, and source health live here.",
      };
      hint.textContent = hints[view] || "";
    }
    if (viewTitle && viewLead && viewEyebrow) {
      const copy = {
        home: {
          eyebrow: "Daily Workflow",
          title: "Triage what changed before you open a company.",
          lead: "Home is the landing screen: recent changes, digest, and KPI health. Use it to choose the next issuer, not to do detailed analysis.",
        },
        watchlist: {
          eyebrow: "Watchlist Mode",
          title: "Stay inside the names you actively care about.",
          lead: "This view uses the same inbox and digest surfaces, but the issuer rail is narrowed to your starred tickers.",
        },
        company: {
          eyebrow: "Company Workspace",
          title: "Review one issuer deeply, with everything scoped to that name.",
          lead: "Decision snapshot, claims, filings, findings, and normalized facts are all gathered here so you do not need to jump between sections.",
        },
        sector: {
          eyebrow: "Comparative Intelligence",
          title: "Compare the selected issuer against peers and sector context.",
          lead: "Use this screen when you want relative position, peer anomalies, or comparability warnings rather than a single-company read.",
        },
        review: {
          eyebrow: "Human Review Loop",
          title: "Work the open queue without wading through the entire dashboard.",
          lead: "This screen is for analyst judgment: revised filings, broken theses, contradictions, and other items that require a human decision.",
        },
        ops: {
          eyebrow: "Operations",
          title: "Run the pipeline and inspect source health in one place.",
          lead: "Use Ops only when you want to change the system state. Day-to-day reading should happen in Home, Company, or Review.",
        },
      };
      const current = copy[view] || copy.home;
      viewEyebrow.textContent = current.eyebrow;
      viewTitle.textContent = current.title;
      viewLead.textContent = current.lead;
    }
    // Trigger lazy-load for sections relevant to the new view.
    if (view === "home" || view === "watchlist") {
      fetchSectionDetail("inbox");
      fetchSectionDetail("digest");
    }
    if (view === "review") fetchSectionDetail("review");
    if (view === "ops") fetchSectionDetail("ops");
  }

  function render() {
    renderCounts();
    if (yearInput && !yearInput.value) {
      yearInput.value = String(new Date().getFullYear());
    }

    const q = (search?.value || "").trim().toLowerCase();
    const watchlist = new Set((data.watchlist || []).map((t) => t.toUpperCase()));
    const issuers = data.issuers.filter((item) => {
      if (state.activeView === "watchlist" && !watchlist.has(item.ticker.toUpperCase())) return false;
      return !q || [item.ticker, item.issuer_name, item.sector, item.subsector].join(" ").toLowerCase().includes(q);
    });
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
    const companySnapshot =
      (data.company_snapshots || []).find((item) => (selectedTicker ? item.ticker === selectedTicker : false)) || null;
    const sectorRollup = companySnapshot
      ? (data.sector_rollups || []).find((item) => {
          const payload = item.payload || {};
          const snapshotPayload = companySnapshot.payload || {};
          return (
            item.rollup_key === "peer_intelligence" &&
            item.sector === (snapshotPayload.sector || selectedIssuer?.sector) &&
            (item.subsector || "") === (snapshotPayload.subsector || selectedIssuer?.subsector || "")
          );
        }) || null
      : null;
    const inboxRows = (data.inbox_rows || []).slice(0, 20);
    const marketData = data.market_monitor || {};
    const sourceSyncRows = data.source_sync_rows || marketData.source_syncs || [];
    const digestRows = data.digest_rows || [];
    const reviewRows = data.review_rows || [];
    const intelligenceKpis = data.intelligence_kpis || {};
    const backlogRows = data.backlog_rows || [];
    const canaryRows = data.canary_rows || [];
    const watchlistCards = (data.company_snapshots || [])
      .filter((item) => watchlist.has(String(item.ticker || "").toUpperCase()))
      .map((item) => {
        const payload = item.payload || {};
        const topCard = payload.top_card || {};
        const reviewCount = reviewRows.filter((row) => row.ticker === item.ticker).length;
        const urgencyScore =
          (topCard.decision_state === "Review manually" ? 100 : 0)
          + (topCard.decision_state === "Avoid" ? 120 : 0)
          + (topCard.decision_state === "Watch" ? 40 : 0)
          + (reviewCount * 10)
          + ((payload.top_card?.negative_factors || []).length * 5);
        return {
          ticker: item.ticker,
          issuer_name: payload.issuer_name || item.ticker,
          decision_state: topCard.decision_state || "Watch",
          review_count: reviewCount,
          why: (payload.decision_panel?.why || topCard.reasons || []).slice(0, 3),
          urgencyScore,
          updated_at: item.updated_at || item.as_of_date,
        };
      })
      .sort((a, b) => (b.urgencyScore - a.urgencyScore) || String(a.ticker).localeCompare(String(b.ticker)));
    renderSelectedIssuerCard(selectedIssuer, companySnapshot);

    if (marketMonitor) {
      const counts = marketData.counts || {};
      const pickRows = marketData.stock_pick_candidates || [];
      const avoidRows = marketData.avoid_candidates || [];
      const riskRows = marketData.highest_risk_changes || [];
      const improvedRows = marketData.most_improved || [];
      const sectorRows = marketData.sector_heatmap || [];
      const lateRows = marketData.late_periodic_rows || [];
      const staleWarning = marketData.staleness_warning;
      marketMonitor.innerHTML = `
        ${
          staleWarning
            ? `<div class="findingCard sev-warning"><div class="findingClaim">Discovery data is stale</div><div class="meta">At least one source has not synced in the last 24 hours. Market monitor counts may be incomplete until discovery runs again.</div></div>`
            : ""
        }
        <div class="statsGrid">
          <div class="stat"><div class="summaryLabel">New since last sync</div><strong>${esc(counts.new_disclosures_since_sync || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Material events since sync</div><strong>${esc(counts.material_event_filings_since_sync || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Potential late periodic</div><strong>${esc(counts.late_periodic_filings || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Stock-pick candidates</div><strong>${esc(counts.stock_pick_candidates || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Avoid candidates</div><strong>${esc(counts.avoid_candidates || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Auditor changes 7d</div><strong>${esc(counts.auditor_changes_7d || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Management changes 7d</div><strong>${esc(counts.management_changes_7d || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Debt restructurings 7d</div><strong>${esc(counts.debt_restructurings_7d || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Rights issues 7d</div><strong>${esc(counts.rights_issues_7d || 0)}</strong></div>
          <div class="stat"><div class="summaryLabel">Legal / regulatory 7d</div><strong>${esc(counts.lawsuits_regulatory_7d || 0)}</strong></div>
        </div>
        <div class="docGroups">
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Stock-pick candidates</h3>
                <div class="meta">Deterministic Buy candidates only. Requires high-confidence Buy stance, improving thesis, no open review queue, and evidence-backed reasons.</div>
              </div>
            </div>
            ${
              pickRows.length
                ? `<div class="findingStack">${pickRows
                    .map(
                      (item) => `
                        <article class="findingCard sev-ok ${item.ticker ? "clickableCard" : ""}" ${
                          item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                        }>
                          <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                          <div class="meta">${esc(item.decision_state || "Buy")} · confidence ${esc(item.confidence || "high")} · score ${esc(item.score || 0)} · evidence ${esc(item.evidence_ref_count || 0)}</div>
                          <div class="meta">${(item.reasons || []).map(esc).join(" · ") || "No deterministic reasons captured."}</div>
                          <div class="meta">${(item.decision_constraints || []).map(esc).join(" · ") || "No immediate constraints."}</div>
                          <div class="meta">${renderTimestamp(item.as_of_date)}</div>
                        </article>
                      `
                    )
                    .join("")}</div>`
                : `<div class="emptyState"><strong>No stock-pick candidates right now.</strong> The list only populates when the deterministic eligibility gates are met.</div>`
            }
          </section>
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Avoid candidates</h3>
                <div class="meta">Deterministic avoid calls only. Requires an Avoid stance with evidence-backed stress such as broken theses, critical signals, or poor credibility.</div>
              </div>
            </div>
            ${
              avoidRows.length
                ? `<div class="findingStack">${avoidRows
                    .map(
                      (item) => `
                        <article class="findingCard sev-critical ${item.ticker ? "clickableCard" : ""}" ${
                          item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                        }>
                          <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                          <div class="meta">${esc(item.decision_state || "Avoid")} · confidence ${esc(item.confidence || "high")} · score ${esc(item.score || 0)} · evidence ${esc(item.evidence_ref_count || 0)}</div>
                          <div class="meta">${(item.reasons || []).map(esc).join(" · ") || "No deterministic reasons captured."}</div>
                          <div class="meta">${(item.decision_constraints || []).map(esc).join(" · ") || "No decision constraints captured."}</div>
                          <div class="meta">${renderTimestamp(item.as_of_date)}</div>
                        </article>
                      `
                    )
                    .join("")}</div>`
                : `<div class="emptyState"><strong>No avoid candidates right now.</strong> The list only populates when the deterministic avoid gates are met.</div>`
            }
          </section>
        </div>
        <div class="docGroups">
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Highest-risk changes in the last 7 days</h3>
                <div class="meta">Ranked from current decision state, review pressure, peer anomalies, and negative factors.</div>
              </div>
            </div>
            ${
              riskRows.length
                ? `<div class="findingStack">${riskRows
                    .map(
                      (item) => `
                        <article class="findingCard sev-${statusTag(item.overall_state)} ${item.ticker ? "clickableCard" : ""}" ${
                          item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                        }>
                          <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                          <div class="meta">${esc(item.decision_state || "Review manually")} · risk ${esc(item.risk_score)}</div>
                          <div class="meta">${(item.reasons || []).map(esc).join(" · ") || "No reasons captured."}</div>
                          <div class="meta">${renderTimestamp(item.as_of_date)}</div>
                        </article>
                      `
                    )
                    .join("")}</div>`
                : `<div class="emptyState"><strong>No ranked risk changes yet.</strong></div>`
            }
          </section>
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Most improved fundamentals</h3>
                <div class="meta">Names with improving thesis state and the strongest positive factor mix.</div>
              </div>
            </div>
            ${
              improvedRows.length
                ? `<div class="findingStack">${improvedRows
                    .map(
                      (item) => `
                        <article class="findingCard sev-ok ${item.ticker ? "clickableCard" : ""}" ${
                          item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                        }>
                          <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                          <div class="meta">${esc(item.decision_state || "Watch")} · improvement ${esc(item.improvement_score)}</div>
                          <div class="meta">${(item.positive_factors || []).map(esc).join(" · ") || "No positive factors captured."}</div>
                          <div class="meta">${renderTimestamp(item.as_of_date)}</div>
                        </article>
                      `
                    )
                    .join("")}</div>`
                : `<div class="emptyState"><strong>No improving-fundamentals ranking yet.</strong></div>`
            }
          </section>
        </div>
        <div class="docGroups">
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Sector heatmap</h3>
                <div class="meta">Disclosure intensity and warning density over the last 7 days.</div>
              </div>
            </div>
            ${
              sectorRows.length
                ? `<table class="metricTable"><thead><tr><th>Sector</th><th>Signal count</th><th>Disclosures</th><th>Material events</th><th>Warnings</th><th>Critical</th></tr></thead><tbody>${sectorRows
                    .map(
                      (item) =>
                        `<tr><td>${esc(item.sector)}</td><td>${esc(item.signal_count_7d || 0)}</td><td>${esc(item.disclosures_7d)}</td><td>${esc(item.material_events_7d)}</td><td>${esc(item.warning_signals_7d)}</td><td>${esc(item.critical_signals_7d)}</td></tr>`
                    )
                    .join("")}</tbody></table>`
                : `<div class="emptyState"><strong>No sector heatmap yet.</strong></div>`
            }
          </section>
          <section class="docGroup">
            <div class="docGroupHeader">
              <div>
                <h3>Potential late / missing periodic filings</h3>
                <div class="meta">Heuristic based on the absence of a recent annual or periodic financial filing.</div>
              </div>
            </div>
            ${
              lateRows.length
                ? `<table class="metricTable"><thead><tr><th>Ticker</th><th>Issuer</th><th>Expected</th><th>Due date</th><th>Latest periodic</th></tr></thead><tbody>${lateRows
                    .map(
                      (item) =>
                        `<tr><td>${esc(item.ticker)}</td><td>${esc(item.issuer_name || "")}</td><td>${esc(item.expected_label || "Periodic filing")}</td><td>${esc(item.expected_due_date || "—")}</td><td>${item.latest_periodic_at ? renderTimestamp(item.latest_periodic_at) : "Never"}</td></tr>`
                    )
                    .join("")}</tbody></table>`
                : `<div class="emptyState"><strong>No potentially late periodic issuers right now.</strong></div>`
            }
          </section>
        </div>
      `;
    }

    issuerList.innerHTML = issuers.length
      ? issuers
          .map(
            (item) => `
              <button class="issuerCard ${item.ticker === state.selectedTicker ? "active" : ""}" type="button" data-ticker="${esc(
                item.ticker
              )}">
                <div class="issuerLine">
                  <div class="issuerTicker">${esc(item.ticker)}</div>
                  <button class="starButton ${watchlist.has(item.ticker.toUpperCase()) ? "starred" : ""}" type="button" data-star-ticker="${esc(item.ticker)}" title="Toggle watchlist">${watchlist.has(item.ticker.toUpperCase()) ? "\u2605" : "\u2606"}</button>
                  <div class="issuerCountBadge">${esc(item.document_count || 0)}</div>
                </div>
                <div class="issuerName">${esc(item.issuer_name)}</div>
                <div class="issuerMeta">
                  <span class="${!item.sector || item.sector === "unknown" ? "sectorMissing" : ""}">${esc(item.sector || "unknown")}</span>
                  <span>${esc(item.latest_document_at || item.last_seen_at || "")}</span>
                </div>
                <div class="issuerCta">${item.ticker === state.selectedTicker ? "Open in company workspace" : "Select issuer"}</div>
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
                <article class="inboxItem ${hasHighlight("inbox", inboxKey(item)) ? "freshRow" : ""} ${item.ticker ? "clickableCard" : ""}" ${
                  item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                }>
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
                    ${item.ticker ? `<button class="ghostButton miniButton" type="button" data-focus-ticker="${esc(item.ticker)}">Open company</button>` : ""}
                    ${item.local_path ? `<a class="chip" href="${localHref(item.local_path)}">open file</a>` : ""}
                    ${item.original_url ? `<a class="chip" href="${esc(item.original_url)}">origin</a>` : ""}
                  </div>
                </article>
              `
            )
            .join("")
        : `<div class="emptyState">No recent cross-issuer activity yet. Run discovery or process a few documents to populate this inbox.</div>`;
    }

    if (kpiList) {
      const overallStates = intelligenceKpis.overall_state_counts || {};
      const thesisStates = intelligenceKpis.thesis_state_counts || {};
      const targets = intelligenceKpis.kpi_targets || {};
      const kpiSeverity = (actual, target) => {
        if (actual == null || target == null) return "info";
        if (actual >= target) return "ok";
        if (actual >= target - 5) return "warning";
        return "critical";
      };
      const formatPct = (value) => (value == null ? "—" : `${Number(value).toFixed(1)}%`);
      const formatTarget = (value) => (value == null ? "" : `target ≥ ${Number(value).toFixed(0)}%`);
      const kpiCard = (label, actual, target, note) => {
        const sev = kpiSeverity(actual, target);
        return `
          <div class="stat sev-${sev}">
            <div class="summaryLabel">${esc(label)}</div>
            <strong>${esc(formatPct(actual))}</strong>
            <div class="meta">${esc(formatTarget(target))}${note ? ` · ${esc(note)}` : ""}</div>
          </div>
        `;
      };
      const countCard = (label, value) => `
        <div class="stat"><div class="summaryLabel">${esc(label)}</div><strong>${esc(value || 0)}</strong></div>
      `;
      kpiList.innerHTML = `
        ${kpiCard(
          "Snapshot freshness",
          intelligenceKpis.snapshot_freshness_pct,
          targets.snapshot_freshness_pct,
          `${esc((intelligenceKpis.snapshot_count || 0) - (intelligenceKpis.stale_snapshots || 0))}/${esc(intelligenceKpis.snapshot_count || 0)} fresh`
        )}
        ${kpiCard(
          "Snapshot completeness",
          intelligenceKpis.snapshot_completeness_pct,
          targets.snapshot_completeness_pct,
          `${esc(intelligenceKpis.complete_snapshots || 0)}/${esc(intelligenceKpis.snapshot_count || 0)} complete`
        )}
        ${kpiCard(
          "Digest validity",
          intelligenceKpis.digest_validity_pct,
          targets.digest_validity_pct,
          `${esc(intelligenceKpis.digest_card_with_reason || 0)}/${esc(intelligenceKpis.digest_card_count || 0)} cited`
        )}
        ${countCard("Open reviews", intelligenceKpis.open_review_items)}
        ${countCard("Critical names", overallStates.critical)}
        ${countCard("Broken theses", thesisStates.broken)}
        ${countCard("Peer anomalies", intelligenceKpis.peer_anomaly_count)}
        ${countCard("Stale snapshots", intelligenceKpis.stale_snapshots)}
      `;
    }

    if (watchlistGrid) {
      watchlistGrid.innerHTML = watchlistCards.length
        ? `<div class="findingStack">${watchlistCards
            .map(
              (item) => `
                <article class="findingCard sev-${statusTag(item.decision_state)} clickableCard" data-focus-ticker="${esc(item.ticker)}">
                  <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                  <div class="meta">${esc(item.decision_state)} · open reviews ${esc(item.review_count)}</div>
                  <div class="meta">${(item.why || []).map(esc).join(" · ") || "No ranked reasons yet."}</div>
                  <div class="meta">${renderTimestamp(item.updated_at)}</div>
                </article>
              `
            )
            .join("")}</div>`
        : `<div class="emptyState"><strong>No watchlist issuers yet.</strong> Star companies in the left rail to make this view useful.</div>`;
    }

    if (digestList) {
      const latestDigest = digestRows[0] || null;
      const cards = latestDigest?.payload?.company_cards || [];
      const sectors = latestDigest?.payload?.sector_moves || [];
      digestList.innerHTML = latestDigest
        ? `
          <div class="meta">Latest digest ${renderTimestamp(latestDigest.updated_at)} · ${esc(latestDigest.scope)} scope</div>
          <div class="findingStack">${cards
            .slice(0, 8)
            .map(
              (item) => `
                <article class="findingCard sev-${statusTag(item.overall_state)} ${item.ticker ? "clickableCard" : ""}" ${
                  item.ticker ? `data-focus-ticker="${esc(item.ticker)}"` : ""
                }>
                  <div class="findingClaim">${esc(item.ticker)} · ${esc(item.issuer_name || "")}</div>
                  <div class="meta">${esc(item.decision_state || "Watch")} · review ${esc(item.review_item_count || 0)} · anomalies ${esc(item.peer_anomaly_count || 0)}</div>
                  <div class="meta">${(item.reasons || []).map(esc).join(" · ")}</div>
                  <div class="meta">Click to open this issuer in the company workspace.</div>
                </article>
              `
            )
            .join("")}</div>
          <div class="docGroups">${sectors
            .slice(0, 6)
            .map(
              (item) => `
                <section class="docGroup">
                  <div class="docGroupHeader">
                    <div>
                      <h3>${esc(item.sector)}</h3>
                      <div class="meta">critical ${esc(item.critical_companies || 0)} · reviews ${esc(item.open_review_items || 0)} · anomalies ${esc(item.peer_anomalies || 0)}</div>
                    </div>
                  </div>
                </section>
              `
            )
            .join("")}</div>
        `
        : `<div class="emptyState"><strong>No daily digest yet.</strong> Run Build Digest from the control panel to persist the daily v3 summary.</div>`;
    }

    if (reviewList) {
      reviewList.innerHTML = reviewRows.length
        ? `<div class="findingStack">${reviewRows
            .map(
              (item) => `
                <article class="findingCard sev-${statusTag(item.priority)}">
                  <div class="findingClaim">${esc(item.ticker || "-")} · ${esc(item.item_type)}</div>
                  <div class="meta">${esc(item.priority)} priority · ${esc(item.status)} · ${esc(item.decision_state || "-")}</div>
                  <div class="meta">${esc(item.rationale || "")}</div>
                  <div class="heroBar">
                    ${item.local_path ? `<a class="chip" href="${localHref(item.local_path)}">artifact</a>` : ""}
                    <button class="miniButton" type="button" data-review-id="${esc(item.review_item_id)}" data-review-status="resolved">resolve</button>
                    <button class="miniButton ghostButton" type="button" data-review-id="${esc(item.review_item_id)}" data-review-status="snoozed">snooze</button>
                    ${
                      item.status !== "open"
                        ? `<button class="miniButton ghostButton" type="button" data-review-id="${esc(item.review_item_id)}" data-review-status="open">reopen</button>`
                        : ""
                    }
                  </div>
                </article>
              `
            )
            .join("")}</div>`
        : `<div class="emptyState"><strong>No open review items.</strong> Revised filings, broken theses, unresolved claims, and high-signal rules will show up here.</div>`;
    }

    if (!selectedIssuer) {
      companyOverview.innerHTML =
        `<div class="emptyState"><strong>Select a company.</strong> Use the left rail or click "focus issuer" from the inbox to move the workspace onto one company.</div>`;
      companyStats.innerHTML = "";
      snapshotList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Decision-state, baseline context, and v3 rationale appear here once an issuer is focused.</div>`;
      claimsList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Claim outcomes and thesis discipline are shown per issuer.</div>`;
      sectorExplorer.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Peer ranks, comparability warnings, and sector leaders appear here per issuer.</div>`;
      documentList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Pick one issuer to review grouped filings here.</div>`;
      metricList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Metrics will appear once the workspace is focused on an issuer.</div>`;
      revisionList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Revision history is shown per issuer.</div>`;
      findingList.innerHTML = `<div class="emptyState"><strong>No company selected.</strong> Findings and summaries are scoped to the active issuer.</div>`;
    } else {
      const analyzedCount = docs.filter((item) => item.process_state === "analyzed").length;
      const failedCount = docs.filter((item) => item.process_state === "failed").length;
      const groupedDocs = groupBy(docs, (item) => item.source_name || "unknown");
      const payload = companySnapshot?.payload || {};
      const topCard = payload.top_card || {};
      const decisionPanel = payload.decision_panel || {};
      const latestFiling = docs[0] || null;

        companyOverview.innerHTML = `
        <div class="sectionEyebrow">Selected Company</div>
        <h2 class="focusTitle">${esc(selectedIssuer.issuer_name)}</h2>
        <div class="focusSubtitle">${esc(selectedIssuer.ticker)} is the active focus. Start with the current stance, then verify it against filings and evidence below.</div>
        ${
          companySnapshot
            ? `<div class="findingStack">
                <article class="findingCard sev-${statusTag(topCard.overall_state)}">
                  <div class="findingClaim">Current view</div>
                  <div class="meta"><strong>Status:</strong> ${esc(decisionPanel.stance || topCard.decision_state || "Watch")} · confidence ${esc(decisionPanel.confidence || topCard.stance_confidence || topCard.confidence || "low")}</div>
                  <div class="meta"><strong>Why:</strong> ${(decisionPanel.why || topCard.reasons || []).map(esc).join(" · ") || "No top-card reasons were derived."}</div>
                  <div class="meta"><strong>What changed:</strong> ${(decisionPanel.what_changed || []).map(esc).join(" · ") || "No ranked delta bullets yet."}</div>
                  <div class="meta"><strong>What to watch next:</strong> ${(decisionPanel.what_to_watch || []).map(esc).join(" · ") || "No follow-up watch items yet."}</div>
                </article>
              </div>`
            : ""
        }
        <div class="heroBar">
          <span class="chip">${esc(selectedIssuer.sector || "unknown")} / ${esc(selectedIssuer.subsector || "unknown")}</span>
          <span class="chip">Last seen ${renderTimestamp(selectedIssuer.last_seen_at || "")}</span>
          <span class="chip">Latest filing ${renderTimestamp(selectedIssuer.latest_document_at || "")}</span>
          ${companySnapshot ? `<span class="chip">FY end ${esc(monthLabel(payload.fiscal_year_end_month || 12))} · ${esc(payload.fiscal_year_end_source || "default")}</span>` : ""}
        </div>
        ${
          companySnapshot
            ? `<div class="heroBar">
                <label class="meta" for="fyeOverrideMonth">Fiscal year override</label>
                <select id="fyeOverrideMonth">${Array.from({ length: 12 }, (_, i) => i + 1)
                  .map((month) => `<option value="${month}" ${Number(payload.fiscal_year_end_month || 12) === month ? "selected" : ""}>${monthLabel(month)}</option>`)
                  .join("")}</select>
                <button class="chip" type="button" data-fye-save="1">Save FY override</button>
                <button class="chip" type="button" data-fye-clear="1">Clear override</button>
              </div>`
            : ""
        }
      `;
      companyStats.innerHTML = `
        <div class="stat"><div class="summaryLabel">Documents</div><strong>${docs.length}</strong></div>
        <div class="stat"><div class="summaryLabel">Findings</div><strong>${findings.length}</strong></div>
        <div class="stat"><div class="summaryLabel">Analyzed</div><strong>${analyzedCount}</strong></div>
        <div class="stat"><div class="summaryLabel">Failed</div><strong>${failedCount}</strong></div>
        <div class="stat"><div class="summaryLabel">Latest filing type</div><strong>${esc(latestFiling?.document_type || payload.latest_document?.document_type || "unknown")}</strong></div>
        <div class="stat"><div class="summaryLabel">Priority</div><strong>${esc(topCard.watchlist_priority || "low")}</strong></div>
      `;
      if (companySnapshot) {
        const baseline = payload.baseline || {};
        const management = payload.management_credibility || {};
        const thesis = (payload.thesis_evaluations || []).slice(0, 3);
        const reviewRequired = (payload.review_required || []).slice(0, 6);
        const filingExpectations = (payload.filing_expectations || []).slice(0, 6);
        const segmentSummary = (payload.segment_summary || []).slice(0, 6);
        const peerComparison = payload.peer_comparison || {};
        const sectorContext = payload.sector_context || {};
        const metricRanks = Object.entries(peerComparison.metric_ranks || {});
        const peerRows = (peerComparison.peer_rows || []).slice(0, 8);
        const leaders = sectorRollup?.payload?.metric_summaries || {};
        snapshotList.innerHTML = `
          <div class="statsGrid">
            <div class="stat"><div class="summaryLabel">Decision</div><strong>${esc(topCard.decision_state || "Watch")}</strong></div>
            <div class="stat"><div class="summaryLabel">Overall State</div><strong>${esc(topCard.overall_state || "unknown")}</strong></div>
            <div class="stat"><div class="summaryLabel">Comparability</div><strong>${esc(topCard.comparability_state || baseline.kind || "none")}</strong></div>
            <div class="stat"><div class="summaryLabel">As Of</div><strong>${renderTimestamp(companySnapshot.as_of_date || companySnapshot.updated_at)}</strong></div>
          </div>
          <div class="findingStack">
            <article class="findingCard">
              <div class="findingClaim">Baseline and review context</div>
              <div class="meta">Baseline: ${esc(baseline.kind || "none")} · ${esc(baseline.reason || "No prior baseline")}</div>
              <div class="meta"><strong>Stance confidence:</strong> ${esc(decisionPanel.confidence || topCard.stance_confidence || topCard.confidence || "low")} · data completeness ${esc(topCard.data_completeness_pct ?? "—")}%</div>
              <div class="meta">Positive factors: ${(topCard.positive_factors || []).map(esc).join(" · ") || "none"}</div>
              <div class="meta">Negative factors: ${(topCard.negative_factors || []).map(esc).join(" · ") || "none"}</div>
              <div class="meta"><strong>Must improve:</strong> ${(decisionPanel.must_improve || []).map(esc).join(" · ") || "No explicit upgrade conditions captured."}</div>
              <div class="meta"><strong>Thesis breakers:</strong> ${(decisionPanel.thesis_breakers || []).map(esc).join(" · ") || "No stop-rule thesis breakers configured."}</div>
              <div class="meta"><strong>Next catalyst:</strong> ${esc(decisionPanel.next_catalyst?.label || "No catalyst estimate")} ${
                decisionPanel.next_catalyst?.window_start
                  ? `· ${esc(decisionPanel.next_catalyst.window_start)} to ${esc(decisionPanel.next_catalyst.window_end || "")}`
                  : ""
              }</div>
              <div class="meta"><strong>Quality flags:</strong> ${(decisionPanel.quality_flags || []).map(esc).join(" · ") || "No explicit quality flags right now."}</div>
            </article>
          </div>
          ${
            reviewRequired.length
              ? `<table class="metricTable"><thead><tr><th>Review Item</th><th>Priority</th><th>Rationale</th></tr></thead><tbody>${reviewRequired
                  .map(
                    (item) =>
                      `<tr><td>${esc(item.item_type)}</td><td>${esc(item.priority)}</td><td>${esc(item.rationale || "")}</td></tr>`
                  )
                  .join("")}</tbody></table>`
              : `<div class="emptyState"><strong>No review-required items in the current snapshot.</strong></div>`
          }
          ${
            filingExpectations.length
              ? `<table class="metricTable"><thead><tr><th>Filing</th><th>State</th><th>Window</th><th>Deadline</th><th>Source</th></tr></thead><tbody>${filingExpectations
                  .map(
                    (item) =>
                      `<tr><td>${esc(item.period_label || item.obligation_type || "")}<div class="meta">${esc(item.period_end_date || "")}</div></td><td>${esc(item.state || "unknown")}</td><td>${esc(item.window_start || "")}</td><td>${esc(item.deadline_at || "")}</td><td>${esc(item.knowledge_source || "")}</td></tr>`
                  )
                  .join("")}</tbody></table>`
              : `<div class="emptyState"><strong>No filing calendar yet.</strong> Run or rerun intelligence for this issuer to materialize explicit filing expectations.</div>`
          }
        `;
        claimsList.innerHTML = `
          <div class="statsGrid">
            <div class="stat"><div class="summaryLabel">Claims</div><strong>${esc(management.total_claims || 0)}</strong></div>
            <div class="stat"><div class="summaryLabel">Achieved</div><strong>${esc(management.achieved || 0)}</strong></div>
            <div class="stat"><div class="summaryLabel">Contradicted</div><strong>${esc(management.contradicted || 0)}</strong></div>
            <div class="stat"><div class="summaryLabel">Unresolved</div><strong>${esc(management.unresolved || 0)}</strong></div>
          </div>
          <div class="findingStack">
            ${(payload.claim_outcomes || [])
              .slice(0, 5)
              .map(
                (item) => `
                  <article class="findingCard sev-${statusTag(item.outcome_status)}">
                    <div class="findingClaim">${esc(item.outcome_status || "Unknown")} claim outcome</div>
                    <div class="meta">${esc(item.rationale || "")}</div>
                  </article>
                `
              )
              .join("") || `<div class="emptyState"><strong>No claim outcomes yet.</strong></div>`}
          </div>
          ${
            thesis.length
              ? `<table class="metricTable"><thead><tr><th>Thesis</th><th>Status</th><th>Score</th></tr></thead><tbody>${thesis
                  .map(
                    (item) =>
                      `<tr><td>${esc(item.thesis_id || "thesis")}</td><td>${esc(item.status || "unknown")}</td><td>${esc(item.score ?? "")}</td></tr>`
                  )
                  .join("")}</tbody></table>`
              : `<div class="emptyState"><strong>No thesis evaluations for this issuer.</strong></div>`
          }
        `;
        sectorExplorer.innerHTML = `
          <div class="statsGrid">
            <div class="stat"><div class="summaryLabel">Peer Set</div><strong>${esc(peerComparison.peer_key || "none")}</strong></div>
            <div class="stat"><div class="summaryLabel">Comparable Peers</div><strong>${esc(peerComparison.comparable_peer_count || 0)}</strong></div>
            <div class="stat"><div class="summaryLabel">Peer Anomalies</div><strong>${esc((peerComparison.anomalies || []).length)}</strong></div>
            <div class="stat"><div class="summaryLabel">Sector Red-Flag Density</div><strong>${esc(sectorContext.heatmap?.red_flag_density ?? sectorRollup?.payload?.heatmap?.red_flag_density ?? "0")}</strong></div>
          </div>
          ${
            (peerComparison.comparability_warnings || []).length
              ? `<div class="findingStack">${(peerComparison.comparability_warnings || [])
                  .slice(0, 5)
                  .map((item) => `<article class="findingCard sev-warning"><div class="findingClaim">Comparability warning</div><div class="meta">${esc(item)}</div></article>`)
                  .join("")}</div>`
              : `<div class="emptyState"><strong>No comparability warnings for this peer set.</strong></div>`
          }
          ${
            metricRanks.length
              ? `<table class="metricTable"><thead><tr><th>Metric</th><th>Rank</th><th>Peer Median</th><th>Gap %</th><th>Anomaly</th></tr></thead><tbody>${metricRanks
                  .map(
                    ([metricName, item]) =>
                      `<tr><td>${esc(item.label || metricName)}</td><td>${esc(item.rank)}/${esc(item.sample_size)}</td><td>${esc(item.peer_median ?? "")}</td><td>${esc(
                        item.relative_gap_pct != null ? Number(item.relative_gap_pct).toFixed(2) : ""
                      )}</td><td>${esc(item.anomaly || "-")}</td></tr>`
                  )
                  .join("")}</tbody></table>`
              : `<div class="emptyState"><strong>No comparable peer metrics yet.</strong></div>`
          }
          ${
            peerRows.length
              ? `<table class="metricTable"><thead><tr><th>Peer</th><th>Revenue Growth</th><th>Op Margin</th><th>Event Density</th><th>Warnings</th></tr></thead><tbody>${peerRows
                  .map(
                    (item) =>
                      `<tr><td>${esc(item.ticker)}<div class="meta">${esc(item.issuer_name || "")}</div></td><td>${esc(item.metrics?.revenue_growth_pct ?? "")}</td><td>${esc(
                        item.metrics?.operating_margin_pct ?? ""
                      )}</td><td>${esc(item.metrics?.event_density ?? "")}</td><td>${esc((item.comparability_warnings || []).join("; ") || "-")}</td></tr>`
                  )
                  .join("")}</tbody></table>`
              : `<div class="emptyState"><strong>No peer table yet.</strong></div>`
          }
          ${
            Object.keys(leaders).length
              ? `<div class="docGroups">${Object.entries(leaders)
                  .slice(0, 3)
                  .map(
                    ([metricName, item]) => `
                      <section class="docGroup">
                        <div class="docGroupHeader">
                          <div>
                            <h3>${esc(item.label || metricName)}</h3>
                            <div class="meta">Median ${esc(item.median ?? "")} · sample ${esc(item.sample_size ?? 0)}</div>
                          </div>
                        </div>
                        <div class="docList">
                          <article class="docItem">
                            <div>
                              <div class="docTitle">Leaders</div>
                              <div class="meta">${(item.leaders || [])
                                .map((leader) => `${leader.ticker} (${leader.value})`)
                                .map(esc)
                                .join(" · ") || "none"}</div>
                            </div>
                          </article>
                          <article class="docItem">
                            <div>
                              <div class="docTitle">Laggards</div>
                              <div class="meta">${(item.laggards || [])
                                .map((laggard) => `${laggard.ticker} (${laggard.value})`)
                                .map(esc)
                                .join(" · ") || "none"}</div>
                            </div>
                          </article>
                        </div>
                      </section>
                    `
                  )
                  .join("")}</div>`
              : `<div class="emptyState"><strong>No sector rollup yet.</strong> Run intelligence to rebuild sector explorer inputs.</div>`
          }
        `;
      } else {
        snapshotList.innerHTML = `<div class="emptyState"><strong>No v3 snapshot yet.</strong> Run intelligence for this issuer to generate the decision snapshot.</div>`;
        claimsList.innerHTML = `<div class="emptyState"><strong>No claim or thesis state yet.</strong> Run intelligence after rules and summary generation.</div>`;
        sectorExplorer.innerHTML = `<div class="emptyState"><strong>No peer or sector explorer yet.</strong> Run intelligence to populate comparative views.</div>`;
      }
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
      metricList.innerHTML = (segmentSummary.length || metrics.length)
        ? `${segmentSummary.length
            ? `<table class="metricTable"><thead><tr><th>Segment</th><th>Revenue</th><th>Profit</th><th>Period End</th></tr></thead><tbody>${segmentSummary
                .map(
                  (item) =>
                    `<tr><td>${esc(item.segment_name)}</td><td>${esc(item.revenue ?? "")}</td><td>${esc(item.profit ?? "")}</td><td>${esc(item.period_end_date ?? "")}</td></tr>`
                )
                .join("")}</tbody></table>`
            : ""
          }<table class="metricTable"><thead><tr><th>Metric</th><th>Value</th><th>Period</th></tr></thead><tbody>${metrics
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

    if (syncStatusList) {
      syncStatusList.innerHTML = sourceSyncRows.length
        ? `<table class="systemTable"><thead><tr><th>Source</th><th>Class</th><th>Last sync</th><th>Status</th></tr></thead><tbody>${sourceSyncRows
            .map(
              (item) =>
                `<tr><td>${esc(item.source_name)}</td><td>${esc(item.source_class || "-")}</td><td>${renderTimestamp(item.updated_at)}</td><td><span class="statusPill sev-${item.stale ? "warning" : "ok"}">${item.stale ? "stale" : "fresh"}</span></td></tr>`
            )
            .join("")}</tbody></table>`
        : `<div class="emptyState"><strong>No discovery sync records yet.</strong> Run discovery to populate source freshness.</div>`;
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
    // Use lite endpoint for polling — only issuers + KPIs + watchlist.
    const response = await fetch("/api/snapshot/lite");
    if (!response.ok) {
      throw new Error(`Snapshot refresh failed: ${response.status}`);
    }
    const lite = await response.json();
    data.issuers = lite.issuers || data.issuers;
    data.market_monitor = lite.market_monitor || data.market_monitor;
    data.source_sync_rows = lite.source_sync_rows || data.source_sync_rows;
    data.intelligence_kpis = lite.intelligence_kpis || data.intelligence_kpis;
    data.watchlist = lite.watchlist || data.watchlist;
    data.action_history = lite.action_history || data.action_history;
    // Invalidate company cache so next selection re-fetches.
    state._companyCache = {};
    render();
  }

  async function fetchCompanyDetail(ticker) {
    if (!ticker) return;
    const cached = state._companyCache[ticker];
    if (cached && Date.now() - cached.fetchedAt < 30000) return;
    try {
      const response = await fetch(`/api/company?ticker=${encodeURIComponent(ticker)}`);
      if (!response.ok) return;
      const detail = await response.json();
      // Merge into data — replace per-ticker slices.
      data.documents = (data.documents || []).filter((d) => d.ticker !== ticker).concat(detail.documents || []);
      data.metrics = (data.metrics || []).filter((m) => m.ticker !== ticker).concat(detail.metrics || []);
      data.findings = (data.findings || []).filter((f) => f.ticker !== ticker).concat(detail.findings || []);
      data.revisions = (data.revisions || []).filter((r) => r.ticker !== ticker).concat(detail.revisions || []);
      if (detail.company_snapshot) {
        data.company_snapshots = (data.company_snapshots || []).filter((s) => s.ticker !== ticker).concat([detail.company_snapshot]);
      }
      if (detail.sector_rollup) {
        const key = detail.sector_rollup.rollup_key;
        const sector = detail.sector_rollup.sector;
        data.sector_rollups = (data.sector_rollups || [])
          .filter((s) => !(s.rollup_key === key && s.sector === sector))
          .concat([detail.sector_rollup]);
      }
      state._companyCache[ticker] = { data: detail, fetchedAt: Date.now() };
      render();
    } catch (_) {
      // Silently fail — data stays stale until next poll.
    }
  }

  async function fetchSectionDetail(section) {
    if (state._sectionLoaded[section] || state._sectionLoading[section]) return;
    state._sectionLoading[section] = true;
    try {
      const response = await fetch(`/api/detail?section=${encodeURIComponent(section)}`);
      if (!response.ok) return;
      const detail = await response.json();
      if (detail.inbox_rows) data.inbox_rows = detail.inbox_rows;
      if (detail.digest_rows) data.digest_rows = detail.digest_rows;
      if (detail.review_rows) data.review_rows = detail.review_rows;
      if (detail.backlog_rows) data.backlog_rows = detail.backlog_rows;
      if (detail.canary_rows) data.canary_rows = detail.canary_rows;
      if (detail.source_sync_rows) data.source_sync_rows = detail.source_sync_rows;
      state._sectionLoaded[section] = true;
      render();
    } catch (_) {
      // Silently fail.
    } finally {
      state._sectionLoading[section] = false;
    }
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
      "digest",
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

  async function updateReviewStatus(reviewItemId, status) {
    const response = await fetch(reviewEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body: new URLSearchParams({
        review_item_id: reviewItemId,
        status,
      }).toString(),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || `Review update failed: ${response.status}`);
    }
    await refreshSnapshot();
    if (actionStatus) {
      actionStatus.textContent = `Review item ${reviewItemId} moved to ${status}.`;
    }
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
    // Invalidate lazy caches so sections re-fetch after pipeline actions.
    state._companyCache = {};
    state._sectionLoaded = {};
    const sequenceActive = isSequenceActive();
    refreshSnapshot()
      .then(() => {
        fetchSectionDetail("inbox");
        fetchSectionDetail("digest");
        fetchSectionDetail("review");
        fetchSectionDetail("ops");
        if (state.selectedTicker) fetchCompanyDetail(state.selectedTicker);
      })
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
    const viewTarget = event.target.closest("[data-view]");
    if (viewTarget) {
      switchView(viewTarget.dataset.view);
      return;
    }

    const starTarget = event.target.closest("[data-star-ticker]");
    if (starTarget) {
      event.stopPropagation();
      const ticker = starTarget.dataset.starTicker || "";
      const isStarred = starTarget.classList.contains("starred");
      const action = isStarred ? "remove" : "add";
      fetch("/api/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: `ticker=${encodeURIComponent(ticker)}&action=${encodeURIComponent(action)}`,
      })
        .then((r) => r.json())
        .then((result) => {
          data.watchlist = result.tickers || [];
          render();
        })
        .catch(() => {});
      return;
    }

    const issuerTarget = event.target.closest("[data-ticker]");
    if (issuerTarget) {
      state.selectedTicker = issuerTarget.dataset.ticker || null;
      render();
      if (state.selectedTicker) {
        fetchCompanyDetail(state.selectedTicker);
        switchView("company");
      }
      return;
    }

    const focusTarget = event.target.closest("[data-focus-ticker]");
    if (focusTarget) {
      state.selectedTicker = focusTarget.dataset.focusTicker || null;
      render();
      if (state.selectedTicker) {
        fetchCompanyDetail(state.selectedTicker);
      }
      switchView("company");
      companyOverview?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const target = event.target.closest("[data-inline-action]");
    if (target) {
      const documentId = target.dataset.documentId || "";
      runAction(target.dataset.inlineAction, { document_id: documentId }).catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      });
      return;
    }

    const saveOverrideTarget = event.target.closest("[data-fye-save]");
    if (saveOverrideTarget && state.selectedTicker) {
      const select = document.getElementById("fyeOverrideMonth");
      const month = select ? select.value : "";
      fetch(companyOverrideEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: `ticker=${encodeURIComponent(state.selectedTicker)}&month=${encodeURIComponent(month)}`,
      })
        .then((r) => r.json())
        .then(() => Promise.all([refreshSnapshot(), fetchCompanyDetail(state.selectedTicker)]))
        .then(() => render())
        .catch((error) => {
          if (actionStatus) actionStatus.textContent = error.message;
        });
      return;
    }

    const clearOverrideTarget = event.target.closest("[data-fye-clear]");
    if (clearOverrideTarget && state.selectedTicker) {
      fetch(companyOverrideEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: `ticker=${encodeURIComponent(state.selectedTicker)}&clear=1`,
      })
        .then((r) => r.json())
        .then(() => Promise.all([refreshSnapshot(), fetchCompanyDetail(state.selectedTicker)]))
        .then(() => render())
        .catch((error) => {
          if (actionStatus) actionStatus.textContent = error.message;
        });
      return;
    }

    const reviewTarget = event.target.closest("[data-review-id]");
    if (reviewTarget) {
      updateReviewStatus(reviewTarget.dataset.reviewId || "", reviewTarget.dataset.reviewStatus || "").catch((error) => {
        if (actionStatus) actionStatus.textContent = error.message;
      });
    }
  });

  startPolling();
  switchView("home");
  render();

  // Lazy-load detail sections after initial render so the page is interactive
  // before heavy queries complete. The initial HTML render bootstraps issuers
  // and KPIs; detail sections fill in asynchronously.
  fetchSectionDetail("inbox");
  fetchSectionDetail("digest");
  fetchSectionDetail("review");
  fetchSectionDetail("ops");
  if (state.selectedTicker) fetchCompanyDetail(state.selectedTicker);
})();
