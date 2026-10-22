/* Market view: IHSG charts, signals, crash risk, episodes, themes (docs/market_risk_plan.md, component 10).
 *
 * Plain SVG built with DOM calls; every label goes in through textContent. The range
 * row above the charts scopes every chart and its table view at once.
 */
(function () {
  const SVG = "http://www.w3.org/2000/svg";
  const RANGES = [
    ["3M", 63],
    ["6M", 126],
    ["1Y", 252],
    ["3Y", 756],
    ["All", Infinity],
  ];
  const REGIME_LABELS = { calm: "Calm", elevated: "Elevated", stressed: "Stressed", crisis: "Crisis" };
  const LEVEL_LABELS = { none: "Normal", elevated: "Elevated", stressed: "Stressed", crisis: "Crisis", no_data: "No data" };
  const SIGNAL_LABELS = {
    drawdown: "IHSG drawdown",
    vol_ratio: "Volatility",
    foreign_sell_streak: "Foreign selling",
    usdidr_change_20d: "Rupiah",
    decline_share: "Decliners",
    sector_drawdown: "Worst sector",
    sector_breadth: "Sectors falling",
    exchange_notices: "Exchange notices",
    active_themes: "Risk themes",
    fundamental_breadth: "Filer stress",
  };
  const KIND_LABELS = { burst: "Burst", new_pairing: "New pairing", unclassified: "Unclassified", anomaly: "Market anomaly" };
  const MARGIN = { top: 14, right: 70, bottom: 28, left: 56 };

  let view = null;
  let range = 252;
  let resizeObserver = null;

  function render(root, payload) {
    view = payload;
    root.textContent = "";
    if (!payload || !payload.has_data) {
      root.append(emptyState());
      return;
    }
    try {
      const saved = window.localStorage.getItem("stockTriage.marketRange");
      if (saved && RANGES.some(([, days]) => String(days) === saved)) range = saved === "Infinity" ? Infinity : Number(saved);
    } catch (_) {
      // Storage blocked: keep the default range.
    }
    const frame = el("div", "mktFrame");
    frame.append(rangeRow(), kpiRow(payload));
    const charts = el("div", "mktCharts");
    charts.append(
      chartCard("ihsg", "IHSG", "Close with its 50- and 200-session averages; shading marks the days the market was stressed or in crisis."),
      chartCard("drawdown", "Below the 52-week high", "How far IHSG is under its highest close of the past year, in percent."),
      chartCard("foreign", "Foreign net flow", "Foreign investors' estimated net buying (blue) or selling (red), Rp billion."),
      chartCard("rupiah", "Rupiah (JISDOR)", "Bank Indonesia's reference rate, rupiah per US dollar. Higher is a weaker rupiah."),
      chartCard("risk", "Crash risk", ""),
    );
    frame.append(charts, signalsCard(payload), sectorsCard(payload), riskDetailCard(payload));
    const lower = el("div", "mktTwoCol");
    lower.append(themesCard(payload), findingsCard(payload));
    frame.append(lower, episodesCard(payload));
    const bottom = el("div", "mktTwoCol");
    bottom.append(calendarCard(payload), noticesCard(payload));
    frame.append(bottom);
    root.append(frame);
    drawCharts(root);
    if (resizeObserver) resizeObserver.disconnect();
    let pending = null;
    resizeObserver = new ResizeObserver(() => {
      clearTimeout(pending);
      pending = setTimeout(() => drawCharts(root), 120);
    });
    resizeObserver.observe(charts);
  }

  // --- layout pieces ---------------------------------------------------------

  function emptyState() {
    const box = el("div", "emptyState");
    const strong = el("strong", null, "No market data yet.");
    box.append(strong, document.createTextNode(" Run "), el("code", null, "market-sync --backfill-from 2020-01-02"),
      document.createTextNode(", then "), el("code", null, "market-signals"), document.createTextNode(" and "),
      el("code", null, "crash-risk"), document.createTextNode("."));
    return box;
  }

  function rangeRow() {
    const row = el("div", "mktRangeRow");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "Chart range");
    row.append(el("span", "summaryLabel", "Range"));
    for (const [label, days] of RANGES) {
      const button = el("button", `mktRange${days === range ? " active" : ""}`, label);
      button.type = "button";
      button.setAttribute("aria-pressed", days === range ? "true" : "false");
      button.addEventListener("click", () => {
        range = days;
        try {
          window.localStorage.setItem("stockTriage.marketRange", String(days));
        } catch (_) {
          // Not remembered; the range still applies now.
        }
        row.querySelectorAll(".mktRange").forEach((node) => {
          const on = node === button;
          node.classList.toggle("active", on);
          node.setAttribute("aria-pressed", on ? "true" : "false");
        });
        drawCharts(row.closest(".mktFrame").parentNode);
      });
      row.append(button);
    }
    return row;
  }

  function kpiRow(payload) {
    const latest = payload.latest;
    const state = payload.state;
    const risk = payload.crash_risk;
    const row = el("div", "mktKpis");
    const regime = state ? state.regime : null;
    row.append(
      tile("Market state", REGIME_LABELS[regime] || "No data", state ? `since ${dateLabel(state.since)}` : "", regime ? `regime-${regime}` : ""),
      tile("IHSG", number(latest.close, 2), signed(latest.change_pct, "%", 2) + ` on ${dateLabel(latest.trade_date)}`, "", latest.change_pct),
      tile("Below the 52-week high", `${number(latest.drawdown_pct, 1)}%`, `high ${number(latest.peak, 0)}`),
      riskTile(risk),
      tile("Rupiah", latest.usdidr ? `Rp ${number(latest.usdidr, 0)}` : "—", latest.usdidr_change_20_pct != null ? `${signed(latest.usdidr_change_20_pct, "%", 1)} in 20 sessions` : "", "", latest.usdidr_change_20_pct != null ? -latest.usdidr_change_20_pct : null),
      tile(
        "Foreign flow, 5 sessions",
        latest.foreign_net_5d_bn != null ? `${latest.foreign_net_5d_bn < 0 ? "\u2212" : "+"}Rp ${number(Math.abs(latest.foreign_net_5d_bn) / 1000, 1)} tn` : "—",
        latest.foreign_net_5d_bn != null ? (latest.foreign_net_5d_bn < 0 ? "net selling, estimated" : "net buying, estimated") : "",
        "",
        latest.foreign_net_5d_bn,
      ),
    );
    return row;
  }

  function riskTile(risk) {
    if (!risk || risk.probability == null) return tile("Crash risk", "—", "run crash-risk");
    const definition = risk.definition || {};
    const what = `${number(definition.drawdown_pct, 0)}% fall in ${definition.horizon_sessions} sessions`;
    if (!risk.trustworthy) return tile("Crash risk", "Withheld", `does not beat its base rate yet; ${what}`);
    return tile("Crash risk", `${Math.round(risk.probability * 100)}%`, `base rate ${Math.round((risk.base_rate || 0) * 100)}%; ${what}`);
  }

  function tile(label, value, note, extraClass, direction) {
    const box = el("div", `mktTile ${extraClass || ""}`);
    box.append(el("div", "summaryLabel", label));
    const strong = el("strong", "mktTileValue", value);
    box.append(strong);
    if (note) {
      const meta = el("div", "meta", note);
      if (direction != null && Number.isFinite(direction)) meta.classList.add(direction < 0 ? "mktDown" : "mktUp");
      box.append(meta);
    }
    return box;
  }

  function chartCard(id, title, caption) {
    const card = el("article", "panel mktChartCard");
    card.dataset.chart = id;
    const head = el("div", "mktChartHead");
    const titles = el("div");
    titles.append(el("h3", null, title));
    const cap = el("div", "meta mktCaption", caption);
    titles.append(cap);
    const toggle = el("button", "mktTableToggle", "Table");
    toggle.type = "button";
    toggle.setAttribute("aria-pressed", "false");
    toggle.addEventListener("click", () => {
      const on = toggle.getAttribute("aria-pressed") !== "true";
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
      card.classList.toggle("showTable", on);
    });
    head.append(titles, toggle);
    const legend = el("div", "mktLegend");
    const plot = el("div", "mktPlot");
    const table = el("div", "mktTableWrap");
    card.append(head, legend, plot, table);
    return card;
  }

  // --- charts ----------------------------------------------------------------

  function drawCharts(root) {
    if (!view || !view.has_data) return;
    const daily = view.daily;
    const n = daily.dates.length;
    const start = Number.isFinite(range) ? Math.max(0, n - range) : 0;
    const slice = (values) => values.slice(start);
    const dates = slice(daily.dates);

    drawIhsg(card(root, "ihsg"), dates, slice(daily.close), slice(daily.ma50), slice(daily.ma200), slice(daily.regime));
    drawDrawdown(card(root, "drawdown"), dates, slice(daily.drawdown), slice(daily.regime));
    drawForeign(card(root, "foreign"), dates, slice(daily.foreign_net));
    const rupiah = view.rupiah;
    const fromDate = dates[0];
    const rIndex = rupiah.dates.findIndex((day) => day >= fromDate);
    drawRupiah(card(root, "rupiah"), rupiah.dates.slice(Math.max(0, rIndex)), rupiah.rate.slice(Math.max(0, rIndex)));
    drawRisk(card(root, "risk"), dates, slice(daily.crash_probability), slice(daily.crash_base_rate));
  }

  function card(root, id) {
    return root.querySelector(`[data-chart="${id}"]`);
  }

  function drawIhsg(host, dates, close, ma50, ma200, regimes) {
    setLegend(host, [
      ["line", "mkSeries1", "IHSG close"],
      ["line", "mkContext1", "50-session average"],
      ["line", "mkContext2", "200-session average"],
      ["band", "band-stressed", "Stressed market"],
      ["band", "band-crisis", "Crisis"],
    ]);
    lineChart(host, {
      dates,
      height: 300,
      bands: regimes,
      series: [
        { name: "IHSG", values: close, cls: "mkSeries1", width: 2, endLabel: true, format: (v) => number(v, 0) },
        { name: "50-session average", values: ma50, cls: "mkContext1", width: 1.5, format: (v) => number(v, 0) },
        { name: "200-session average", values: ma200, cls: "mkContext2", width: 1.5, format: (v) => number(v, 0) },
      ],
      yFormat: (v) => compactNumber(v),
      extraTooltip: (i) => (regimes[i] ? `State: ${REGIME_LABELS[regimes[i]] || regimes[i]}` : ""),
    });
    tableView(host, ["Date", "Close", "50-session", "200-session", "State"], dates.map((day, i) => [day, number(close[i], 2), number(ma50[i], 0), number(ma200[i], 0), REGIME_LABELS[regimes[i]] || "—"]));
  }

  function drawDrawdown(host, dates, drawdown, regimes) {
    setLegend(host, []);
    const negative = drawdown.map((v) => (v == null ? null : -v));
    lineChart(host, {
      dates,
      height: 180,
      bands: regimes,
      area: true,
      series: [{ name: "Below the high", values: negative, cls: "mkSeries1", width: 2, endLabel: true, format: (v) => `${number(-v, 1)}%` }],
      yFormat: (v) => `${number(v, 0)}%`,
      refLines: [
        { value: -5, label: "5% elevated" },
        { value: -10, label: "10% stressed" },
        { value: -20, label: "20% crisis" },
      ],
      yMax: 0,
    });
    tableView(host, ["Date", "Below the 52-week high"], dates.map((day, i) => [day, drawdown[i] == null ? "—" : `${number(drawdown[i], 1)}%`]));
  }

  function drawForeign(host, dates, flows) {
    let labels = dates;
    let values = flows;
    let caption = "Daily, Rp billion (estimated from share volumes).";
    if (dates.length > 300) {
      const weeks = new Map();
      dates.forEach((day, i) => {
        const key = weekKey(day);
        const entry = weeks.get(key) || { day, total: 0, count: 0 };
        if (flows[i] != null) {
          entry.total += flows[i];
          entry.count += 1;
        }
        weeks.set(key, entry);
      });
      labels = [...weeks.values()].map((entry) => entry.day);
      values = [...weeks.values()].map((entry) => (entry.count ? entry.total : null));
      caption = "Weekly sums, Rp billion (estimated from share volumes).";
    }
    host.querySelector(".mktCaption").textContent = `Foreign investors' net buying or selling. ${caption}`;
    setLegend(host, [
      ["rect", "mkBuy", "Net buying"],
      ["rect", "mkSell", "Net selling"],
    ]);
    columnChart(host, { dates: labels, values, height: 180, format: (v) => `Rp ${signed(v, "", 0)} bn` });
    tableView(host, ["Date", "Net flow (Rp bn)"], labels.map((day, i) => [day, values[i] == null ? "—" : signed(values[i], "", 0)]));
  }

  function drawRupiah(host, dates, rates) {
    setLegend(host, []);
    lineChart(host, {
      dates,
      height: 180,
      series: [{ name: "USD/IDR", values: rates, cls: "mkSeries1", width: 2, endLabel: true, format: (v) => `Rp ${number(v, 0)}` }],
      yFormat: (v) => compactNumber(v),
    });
    tableView(host, ["Date", "Rupiah per US dollar"], dates.map((day, i) => [day, number(rates[i], 0)]));
  }

  function drawRisk(host, dates, probability, baseRate) {
    const risk = view.crash_risk;
    const caption = host.querySelector(".mktCaption");
    const plot = host.querySelector(".mktPlot");
    const definition = (risk && risk.definition) || {};
    const what = risk ? `a fall of ${number(definition.drawdown_pct, 0)}% or more from a day's close within ${definition.horizon_sessions} sessions` : "";
    if (!risk || !risk.record_trustworthy) {
      // The estimate is shown as a forecast only once it beats the base rate.
      setLegend(host, []);
      caption.textContent = risk
        ? `The chance of ${what}. Withheld: its walk-forward track record does not beat the plain base rate by ${Math.round((definition.min_skill || 0) * 100)}%, so only the past episodes it resembles are shown below.`
        : "Run crash-risk to estimate the chance of a crash, with its track record.";
      plot.textContent = "";
      host.querySelector(".mktTableWrap").textContent = "";
      host.classList.add("mktWithheld");
      return;
    }
    host.classList.remove("mktWithheld");
    caption.textContent = `The chance of ${what}, estimated each day from what was known that day.`;
    setLegend(host, [
      ["line", "mkSeries1", "Estimated chance"],
      ["line", "mkContext1", "Base rate"],
    ]);
    const pct = (values) => values.map((v) => (v == null ? null : v * 100));
    lineChart(host, {
      dates,
      height: 180,
      series: [
        { name: "Estimated chance", values: pct(probability), cls: "mkSeries1", width: 2, endLabel: true, format: (v) => `${number(v, 0)}%` },
        { name: "Base rate", values: pct(baseRate), cls: "mkContext1", width: 1.5, format: (v) => `${number(v, 0)}%` },
      ],
      yFormat: (v) => `${number(v, 0)}%`,
      yMin: 0,
      refLines: [{ value: (definition.alert_probability || 0.4) * 100, label: "warning level" }],
    });
    tableView(host, ["Date", "Estimated chance", "Base rate"], dates.map((day, i) => [day, probability[i] == null ? "—" : `${number(probability[i] * 100, 0)}%`, baseRate[i] == null ? "—" : `${number(baseRate[i] * 100, 0)}%`]));
  }

  function lineChart(host, options) {
    const plot = host.querySelector(".mktPlot");
    plot.textContent = "";
    const width = Math.max(plot.clientWidth || 600, 280);
    const height = options.height;
    const dates = options.dates;
    const n = dates.length;
    if (!n) {
      plot.append(el("div", "meta", "No data in this range."));
      return;
    }
    const margin = { ...MARGIN, right: width < 480 ? 48 : MARGIN.right, left: width < 480 ? 44 : MARGIN.left };
    const inner = { w: width - margin.left - margin.right, h: height - margin.top - margin.bottom };
    const all = options.series.flatMap((series) => series.values.filter((v) => v != null && Number.isFinite(v)));
    (options.refLines || []).forEach((ref) => all.push(ref.value));
    let lo = options.yMin != null ? options.yMin : Math.min(...all);
    let hi = options.yMax != null ? options.yMax : Math.max(...all);
    if (lo === hi) {
      lo -= 1;
      hi += 1;
    }
    const ticks = niceTicks(lo, hi, 4);
    lo = Math.min(lo, ticks[0]);
    hi = Math.max(hi, ticks[ticks.length - 1]);
    const x = (i) => margin.left + (n === 1 ? inner.w / 2 : (i / (n - 1)) * inner.w);
    const y = (v) => margin.top + inner.h - ((v - lo) / (hi - lo)) * inner.h;

    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "mktSvg", role: "img" });
    svg.setAttribute("aria-label", `${host.querySelector("h3").textContent} chart`);
    if (options.bands) drawBands(svg, options.bands, x, margin, inner, n);
    ticks.forEach((tick) => {
      svg.append(svgEl("line", { x1: margin.left, x2: margin.left + inner.w, y1: y(tick), y2: y(tick), class: "mkGrid" }));
      const label = svgEl("text", { x: margin.left - 8, y: y(tick) + 4, class: "mkAxisText", "text-anchor": "end" });
      label.textContent = options.yFormat(tick);
      svg.append(label);
    });
    (options.refLines || []).forEach((ref) => {
      svg.append(svgEl("line", { x1: margin.left, x2: margin.left + inner.w, y1: y(ref.value), y2: y(ref.value), class: "mkRef" }));
      // At the right edge, clear of where most series start.
      const label = svgEl("text", { x: margin.left + inner.w - 4, y: y(ref.value) - 4, class: "mkRefText", "text-anchor": "end" });
      label.textContent = ref.label;
      svg.append(label);
    });
    dateTicks(dates, width < 480 ? 3 : 6).forEach((i) => {
      const label = svgEl("text", { x: x(i), y: height - 8, class: "mkAxisText", "text-anchor": "middle" });
      label.textContent = dateTickLabel(dates, i);
      svg.append(label);
    });
    svg.append(svgEl("line", { x1: margin.left, x2: margin.left + inner.w, y1: margin.top + inner.h, y2: margin.top + inner.h, class: "mkBaseline" }));

    options.series.forEach((series, k) => {
      const path = linePath(series.values, x, y);
      if (!path) return;
      if (options.area && k === 0) {
        const base = y(Math.min(0, hi));
        svg.append(svgEl("path", { d: `${path} ${areaClose(series.values, x, base)}`, class: `mkArea ${series.cls}` }));
      }
      svg.append(svgEl("path", { d: path, class: `mkLine ${series.cls}`, "stroke-width": series.width }));
    });
    // Direct label at the end of the lead series.
    options.series.filter((series) => series.endLabel).forEach((series) => {
      const last = lastIndex(series.values);
      if (last < 0) return;
      svg.append(svgEl("circle", { cx: x(last), cy: y(series.values[last]), r: 4, class: `mkDot ${series.cls}` }));
      const label = svgEl("text", { x: x(last) + 8, y: y(series.values[last]) + 4, class: "mkEndText" });
      label.textContent = series.format(series.values[last]);
      svg.append(label);
    });
    plot.append(svg);
    crosshair(plot, svg, {
      n,
      x,
      margin,
      inner,
      dates,
      rows: (i) =>
        options.series
          .filter((series) => series.values[i] != null)
          .map((series) => ({ cls: series.cls, label: series.name, value: series.format(series.values[i]), y: y(series.values[i]) })),
      extra: options.extraTooltip,
    });
  }

  function columnChart(host, options) {
    const plot = host.querySelector(".mktPlot");
    plot.textContent = "";
    const width = Math.max(plot.clientWidth || 600, 280);
    const height = options.height;
    const { dates, values } = options;
    const n = dates.length;
    if (!n || !values.some((v) => v != null)) {
      plot.append(el("div", "meta", "No foreign-flow data in this range."));
      return;
    }
    const margin = { ...MARGIN, right: 16, left: width < 480 ? 48 : MARGIN.left };
    const inner = { w: width - margin.left - margin.right, h: height - margin.top - margin.bottom };
    const present = values.filter((v) => v != null);
    const ticks = niceTicks(Math.min(0, ...present), Math.max(0, ...present), 4);
    const lo = ticks[0];
    const hi = ticks[ticks.length - 1];
    const y = (v) => margin.top + inner.h - ((v - lo) / (hi - lo)) * inner.h;
    const slot = inner.w / n;
    const barWidth = Math.max(1, Math.min(24, slot - 2));
    const x = (i) => margin.left + slot * i + slot / 2;
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "mktSvg", role: "img" });
    svg.setAttribute("aria-label", "Foreign net flow chart");
    ticks.forEach((tick) => {
      svg.append(svgEl("line", { x1: margin.left, x2: margin.left + inner.w, y1: y(tick), y2: y(tick), class: tick === 0 ? "mkBaseline" : "mkGrid" }));
      const label = svgEl("text", { x: margin.left - 8, y: y(tick) + 4, class: "mkAxisText", "text-anchor": "end" });
      label.textContent = compactNumber(tick);
      svg.append(label);
    });
    values.forEach((value, i) => {
      if (value == null || value === 0) return;
      const top = y(Math.max(0, value));
      const h = Math.max(1, Math.abs(y(value) - y(0)));
      const rect = svgEl("rect", { x: x(i) - barWidth / 2, y: top, width: barWidth, height: h, class: value > 0 ? "mkBuy" : "mkSell" });
      if (barWidth >= 8) rect.setAttribute("rx", 2);
      svg.append(rect);
    });
    dateTicks(dates, width < 480 ? 3 : 6).forEach((i) => {
      const label = svgEl("text", { x: x(i), y: height - 8, class: "mkAxisText", "text-anchor": "middle" });
      label.textContent = dateTickLabel(dates, i);
      svg.append(label);
    });
    plot.append(svg);
    crosshair(plot, svg, {
      n,
      x,
      margin,
      inner,
      dates,
      rows: (i) => (values[i] == null ? [] : [{ cls: values[i] >= 0 ? "mkBuy" : "mkSell", label: values[i] >= 0 ? "Net buying" : "Net selling", value: options.format(values[i]) }]),
    });
  }

  function drawBands(svg, regimes, x, margin, inner, n) {
    const step = n > 1 ? (x(1) - x(0)) / 2 : inner.w / 2;
    let i = 0;
    while (i < n) {
      const regime = regimes[i];
      // Only stressed and crisis are shaded; elevated is a third of all days and would be noise.
      if (!regime || regime === "calm" || regime === "elevated") {
        i += 1;
        continue;
      }
      let j = i;
      while (j + 1 < n && regimes[j + 1] === regime) j += 1;
      const left = Math.max(margin.left, x(i) - step);
      const right = Math.min(margin.left + inner.w, x(j) + step);
      svg.append(svgEl("rect", { x: left, y: margin.top, width: Math.max(1, right - left), height: inner.h, class: `mkBand band-${regime}` }));
      i = j + 1;
    }
  }

  function crosshair(plot, svg, spec) {
    const hair = svgEl("line", { y1: spec.margin.top, y2: spec.margin.top + spec.inner.h, class: "mkHair", visibility: "hidden" });
    const dots = svgEl("g", { visibility: "hidden" });
    svg.append(hair, dots);
    const tip = el("div", "mktTip");
    tip.hidden = true;
    plot.append(tip);
    const overlay = svgEl("rect", {
      x: spec.margin.left,
      y: spec.margin.top,
      width: spec.inner.w,
      height: spec.inner.h,
      class: "mkOverlay",
      tabindex: 0,
    });
    overlay.setAttribute("aria-label", "Move with arrow keys to read values");
    svg.append(overlay);
    let current = spec.n - 1;

    function show(i) {
      current = Math.max(0, Math.min(spec.n - 1, i));
      const px = spec.x(current);
      hair.setAttribute("x1", px);
      hair.setAttribute("x2", px);
      hair.setAttribute("visibility", "visible");
      dots.textContent = "";
      const rows = spec.rows(current);
      rows.filter((row) => row.y != null).forEach((row) => dots.append(svgEl("circle", { cx: px, cy: row.y, r: 4, class: `mkDot ${row.cls}` })));
      dots.setAttribute("visibility", "visible");
      tip.textContent = "";
      tip.append(el("div", "mktTipDate", dateLabel(spec.dates[current])));
      rows.forEach((row) => {
        const line = el("div", "mktTipRow");
        const key = el("span", `mktKey ${row.cls}`);
        line.append(key, el("strong", null, row.value), el("span", "meta", row.label));
        tip.append(line);
      });
      const extra = spec.extra ? spec.extra(current) : "";
      if (extra) tip.append(el("div", "meta", extra));
      tip.hidden = false;
      const plotWidth = plot.clientWidth;
      const left = px + 12 + 180 > plotWidth ? px - 12 - tip.offsetWidth : px + 12;
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${spec.margin.top}px`;
    }

    function hide() {
      hair.setAttribute("visibility", "hidden");
      dots.setAttribute("visibility", "hidden");
      tip.hidden = true;
    }

    overlay.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const scale = box.width / Number(svg.getAttribute("width"));
      const px = (event.clientX - box.left) / scale;
      const ratio = (px - spec.margin.left) / spec.inner.w;
      show(Math.round(ratio * (spec.n - 1)));
    });
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", () => show(current));
    overlay.addEventListener("blur", hide);
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        show(current + (event.key === "ArrowLeft" ? -1 : 1));
      }
    });
  }

  function setLegend(host, items) {
    const legend = host.querySelector(".mktLegend");
    legend.textContent = "";
    items.forEach(([shape, cls, label]) => {
      const item = el("span", "mktLegendItem");
      item.append(el("span", `mktSwatch mktSwatch-${shape} ${cls}`), document.createTextNode(label));
      legend.append(item);
    });
  }

  function tableView(host, headers, rows) {
    const wrap = host.querySelector(".mktTableWrap");
    wrap.textContent = "";
    const shown = rows.slice(-60).reverse();
    const table = el("table", "mktTable");
    const head = el("tr");
    headers.forEach((text) => head.append(el("th", null, text)));
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    shown.forEach((row) => {
      const tr = el("tr");
      row.forEach((cell, k) => tr.append(el("td", k ? "num" : null, k ? String(cell) : dateLabel(cell))));
      body.append(tr);
    });
    table.append(body);
    wrap.append(el("div", "meta", rows.length > 60 ? `The latest 60 of ${rows.length} rows in this range.` : `${rows.length} rows.`), table);
  }

  // --- cards -----------------------------------------------------------------

  function signalsCard(payload) {
    const box = el("article", "panel");
    box.append(el("div", "sectionEyebrow", "Signals"), el("h3", null, "Today's readings"));
    const state = payload.state;
    if (!state) {
      box.append(el("div", "meta", "Run market-signals."));
      return box;
    }
    box.append(el("div", "meta", `As of ${dateLabel(state.trade_date)}. The state is the highest level at least two signals reach; a 20% fall in IHSG is enough on its own.`));
    const list = el("ul", "mktSignalList");
    (state.readings || []).forEach((reading) => {
      const item = el("li");
      const level = reading.level || "no_data";
      item.append(el("span", `levelTag level-${level}`, LEVEL_LABELS[level] || level));
      const text = el("div");
      text.append(el("strong", null, SIGNAL_LABELS[reading.signal_name] || reading.signal_name));
      text.append(el("div", "meta", reading.text || "No reading yet."));
      item.append(text);
      list.append(item);
    });
    box.append(list);
    (state.event_risk || []).forEach((event) => box.append(el("div", "meta", `Event risk: ${event.title} (${dateLabel(event.date)})`)));
    return box;
  }

  function sectorsCard(payload) {
    const box = el("article", "panel");
    box.append(el("div", "sectionEyebrow", "Sectors"), el("h3", null, "IDX-IC sectors, worst first"));
    const rows = payload.sectors || [];
    if (!rows.length) {
      box.append(el("div", "meta", "No sector index data yet."));
      return box;
    }
    const table = el("table", "mktTable mktSectorTable");
    const head = el("tr");
    ["Sector", "Close", "1 day", "20 days", "Below its high", "Last 60 sessions"].forEach((text, k) => head.append(el("th", k ? "num" : null, text)));
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    const worst = Math.max(...rows.map((row) => row.drawdown_pct || 0), 1);
    rows.forEach((row) => {
      const tr = el("tr");
      tr.append(el("td", null, row.label));
      tr.append(el("td", "num", number(row.close, 0)));
      tr.append(cellSigned(row.change_1d_pct));
      tr.append(cellSigned(row.change_20d_pct));
      const dd = el("td", "num mktBarCell");
      const bar = el("span", "mktBar");
      bar.style.width = `${Math.round(((row.drawdown_pct || 0) / worst) * 100)}%`;
      dd.append(bar, el("span", null, `${number(row.drawdown_pct, 1)}%`));
      tr.append(dd);
      const spark = el("td", "num");
      spark.append(sparkline(row.spark || []));
      tr.append(spark);
      body.append(tr);
    });
    table.append(body);
    const scroll = el("div", "tableScroll");
    scroll.append(table);
    box.append(scroll);
    return box;
  }

  function riskDetailCard(payload) {
    const box = el("article", "panel");
    box.append(el("div", "sectionEyebrow", "History rhymes"), el("h3", null, "Track record and the past episodes today resembles"));
    const risk = payload.crash_risk;
    if (!risk) {
      box.append(el("div", "meta", "Run crash-risk after a market backfill."));
      return box;
    }
    const record = risk.track_record || {};
    const facts = el("div", "mktFacts");
    facts.append(
      fact("Days scored", record.scored_days != null ? number(record.scored_days, 0) : "—"),
      fact("Followed by a crash", record.crash_days != null ? number(record.crash_days, 0) : "—"),
      fact("Brier score", record.brier != null ? number(record.brier, 3) : "—", record.base_rate_brier != null ? `base rate ${number(record.base_rate_brier, 3)}` : ""),
      fact("Skill vs base rate", risk.skill != null ? `${Math.round(risk.skill * 100)}%` : "—", risk.record_trustworthy ? "shown as a forecast" : "not enough to show"),
      fact("Warnings that caught a crash", record.hit_rate != null ? `${Math.round(record.hit_rate * 100)}%` : "—"),
      fact("Warnings that were false alarms", record.false_alarm_rate != null ? `${Math.round(record.false_alarm_rate * 100)}%` : "—"),
    );
    box.append(facts);
    const analogs = risk.analogs || [];
    if (!analogs.length) {
      box.append(el("div", "meta", "No closed past episode to compare with yet."));
    } else {
      const list = el("ul", "mktAnalogs");
      analogs.forEach((analog) => {
        const item = el("li");
        item.append(el("strong", null, `${dateLabel(analog.opened_on)} episode`));
        const recover = analog.sessions_to_recover == null ? "had not recovered its high" : `recovered its high ${analog.sessions_to_recover} sessions after the bottom`;
        item.append(
          el(
            "div",
            "meta",
            `Worst state ${REGIME_LABELS[analog.worst_regime] || analog.worst_regime}; fell a further ${number(analog.further_fall_pct, 1)}% over ${analog.sessions_to_trough} sessions, then ${recover}. Similarity distance ${number(analog.distance, 2)}.`,
          ),
        );
        list.append(item);
      });
      box.append(el("div", "summaryLabel", "Closest past episodes"), list);
    }
    const contributions = Object.entries(risk.contributions || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 4);
    if (risk.record_trustworthy && contributions.length) {
      const list = el("ul", "mktContrib");
      contributions.forEach(([name, value]) => list.append(el("li", null, `${(risk.feature_labels || {})[name] || name}: ${value > 0 ? "raises" : "lowers"} the estimate`)));
      box.append(el("div", "summaryLabel", "What moves today's estimate most"), list);
    }
    return box;
  }

  function fact(label, value, note) {
    const box = el("div", "mktFact");
    box.append(el("div", "summaryLabel", label), el("strong", null, value));
    if (note) box.append(el("div", "meta", note));
    return box;
  }

  function themesCard(payload) {
    const box = el("article", "panel");
    const themes = payload.themes || { tracked: 0, active: [] };
    box.append(el("div", "sectionEyebrow", "Risk themes"), el("h3", null, `Active now (${themes.active.length} of ${themes.tracked} tracked)`));
    if (!themes.active.length) {
      box.append(el("div", "meta", themes.tracked ? "No theme has three or more watched items in the last three days. Run watch-sync to refresh." : "Run watch-sync to start watching news and official sources."));
      return box;
    }
    const list = el("ul", "mktThemeList");
    themes.active.forEach((theme) => {
      const item = el("li");
      const head = el("div", "mktThemeHead");
      head.append(el("span", `levelTag severity-${theme.severity}`, theme.severity), el("strong", null, theme.title), el("span", "meta", `${theme.items} items in 3 days`));
      item.append(head);
      const links = el("ul", "mktHeadlines");
      (theme.headlines || []).forEach((headline) => {
        const li = el("li");
        const a = el("a", null, headline.title);
        if (/^https?:\/\//.test(headline.url || "")) a.href = headline.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        li.append(a, el("span", "mktSource", ` · ${headline.source_id}`));
        links.append(li);
      });
      item.append(links);
      list.append(item);
    });
    box.append(list);
    return box;
  }

  function findingsCard(payload) {
    const box = el("article", "panel");
    const findings = payload.findings || [];
    box.append(el("div", "sectionEyebrow", "What nobody is tracking yet"), el("h3", null, `Emerging findings (${findings.length} open)`));
    if (!findings.length) {
      box.append(el("div", "meta", "None open. Bursts of new terms, new names next to Indonesia, and unexplained market moves land here."));
      return box;
    }
    const list = el("ul", "mktFindingList");
    // Unexplained market moves first, then what is new in the news, then unmatched releases.
    const order = { anomaly: 0, burst: 1, new_pairing: 2, unclassified: 3 };
    const sorted = [...findings].sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9));
    sorted.forEach((finding) => {
      const item = el("li");
      const head = el("div", "mktThemeHead");
      head.append(el("span", "levelTag", KIND_LABELS[finding.kind] || finding.kind), el("strong", null, finding.term));
      item.append(head);
      const details = finding.details || {};
      const note = finding.kind === "unclassified" ? "" : details.note || details.example || "";
      const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
      const counts = finding.kind === "burst" ? `${plural(details.count, "headline")}, ${number(finding.burst_ratio, 1)}x usual` : plural(finding.item_count, "item");
      item.append(el("div", "meta", `${counts}; first seen ${dateLabel(finding.first_seen)}${note ? `. ${note}` : ""}`));
      item.append(el("div", "meta mktCommand", `finding-promote ${finding.finding_id} --theme-id … --title … --keywords …   ·   finding-dismiss ${finding.finding_id}`));
      list.append(item);
    });
    box.append(list);
    return box;
  }

  function episodesCard(payload) {
    const box = el("article", "panel");
    box.append(el("div", "sectionEyebrow", "Episodes"), el("h3", null, "Every stressed stretch since the data starts"));
    const episodes = payload.episodes || [];
    if (!episodes.length) {
      box.append(el("div", "meta", "Run crash-risk to find episodes."));
      return box;
    }
    const table = el("table", "mktTable");
    const head = el("tr");
    ["Opened", "Closed", "Worst", "Max fall", "Bottom", "Recovered", "Cause"].forEach((text, k) => head.append(el("th", k === 3 ? "num" : null, text)));
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    episodes.forEach((episode) => {
      const tr = el("tr");
      tr.append(el("td", null, dateLabel(episode.opened_on)));
      tr.append(el("td", null, episode.closed_on ? dateLabel(episode.closed_on) : "open"));
      const worst = el("td");
      worst.append(el("span", `levelTag level-${episode.worst_regime}`, REGIME_LABELS[episode.worst_regime] || episode.worst_regime));
      tr.append(worst);
      tr.append(el("td", "num", episode.max_drawdown_pct != null ? `${number(episode.max_drawdown_pct, 1)}%` : "—"));
      tr.append(el("td", null, episode.trough_date ? dateLabel(episode.trough_date) : "—"));
      tr.append(el("td", null, episode.recovered_on ? dateLabel(episode.recovered_on) : "not yet"));
      tr.append(el("td", null, episode.cause || `episode-note ${episode.episode_id} --cause …`));
      body.append(tr);
    });
    table.append(body);
    const scroll = el("div", "tableScroll");
    scroll.append(table);
    box.append(scroll);
    return box;
  }

  function calendarCard(payload) {
    const box = el("article", "panel");
    box.append(el("div", "sectionEyebrow", "Risk calendar"), el("h3", null, "The next 90 days"));
    const rows = payload.calendar || [];
    if (!rows.length) {
      box.append(el("div", "meta", "No risk dates. Copy config/catalysts.example.yaml to your data folder and run catalysts-sync."));
      return box;
    }
    const list = el("ul", "marketList");
    rows.forEach((row) => {
      const item = el("li");
      item.append(el("span", "mktDate", dateLabel(row.date)), el("span", `levelTag severity-${row.severity}`, row.severity), el("span", null, row.title));
      list.append(item);
    });
    box.append(list);
    return box;
  }

  function noticesCard(payload) {
    const box = el("article", "panel");
    const notices = payload.notices || { recent: [], daily: [] };
    box.append(el("div", "sectionEyebrow", "Exchange notices"), el("h3", null, "Suspensions and unusual market activity, last 10 days"));
    if (!notices.recent.length) {
      box.append(el("div", "meta", "None stored. market-sync fetches them from IDX."));
      return box;
    }
    const counts = { suspend: 0, unsuspend: 0, uma: 0 };
    notices.recent.forEach((notice) => (counts[notice.kind] = (counts[notice.kind] || 0) + 1));
    box.append(el("div", "meta", `${counts.suspend} suspensions, ${counts.unsuspend} lifted, ${counts.uma} UMA notices.`));
    const list = el("ul", "marketList");
    notices.recent.slice(0, 12).forEach((notice) => {
      const item = el("li");
      item.append(el("span", "mktDate", dateLabel(notice.notice_date)));
      const a = el("a", null, notice.title);
      if (/^https?:\/\//.test(notice.url || "")) a.href = notice.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      item.append(a);
      list.append(item);
    });
    box.append(list);
    return box;
  }

  // --- helpers ---------------------------------------------------------------

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function cellSigned(value) {
    const td = el("td", "num", value == null ? "—" : signed(value, "%", 1));
    if (value != null) td.classList.add(value < 0 ? "mktDown" : "mktUp");
    return td;
  }

  function sparkline(values) {
    const w = 90;
    const h = 22;
    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "mktSpark", "aria-hidden": "true" });
    const present = values.filter((v) => v != null);
    if (present.length < 2) return svg;
    const lo = Math.min(...present);
    const hi = Math.max(...present);
    const x = (i) => (i / (values.length - 1)) * (w - 4) + 2;
    const y = (v) => h - 2 - ((v - lo) / (hi - lo || 1)) * (h - 4);
    svg.append(svgEl("path", { d: linePath(values, x, y), class: "mkLine mkContext2", "stroke-width": 1.5 }));
    svg.append(svgEl("circle", { cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 2.5, class: "mkDot mkSeries1" }));
    return svg;
  }

  function linePath(values, x, y) {
    let d = "";
    let pen = false;
    values.forEach((value, i) => {
      if (value == null || !Number.isFinite(value)) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(value).toFixed(1)}`;
      pen = true;
    });
    return d;
  }

  function areaClose(values, x, base) {
    const first = values.findIndex((v) => v != null);
    const last = lastIndex(values);
    if (first < 0) return "";
    return `L${x(last).toFixed(1)},${base.toFixed(1)}L${x(first).toFixed(1)},${base.toFixed(1)}Z`;
  }

  function lastIndex(values) {
    for (let i = values.length - 1; i >= 0; i -= 1) if (values[i] != null && Number.isFinite(values[i])) return i;
    return -1;
  }

  function niceTicks(lo, hi, count) {
    const span = hi - lo || Math.abs(hi) || 1;
    const raw = span / count;
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => span / s <= count + 0.5) || 10 * magnitude;
    const ticks = [];
    for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) ticks.push(Number(v.toFixed(10)));
    if (ticks[ticks.length - 1] < hi) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  function dateTicks(dates, count) {
    const n = dates.length;
    if (n <= 1) return [0];
    const out = [];
    for (let k = 0; k < count; k += 1) out.push(Math.round((k / (count - 1)) * (n - 1)));
    return [...new Set(out)];
  }

  function dateTickLabel(dates, i) {
    const span = (new Date(dates[dates.length - 1]) - new Date(dates[0])) / 864e5;
    const date = new Date(`${dates[i]}T00:00:00`);
    const options = span > 540 ? { month: "short", year: "numeric" } : { day: "numeric", month: "short" };
    return new Intl.DateTimeFormat(undefined, options).format(date);
  }

  function dateLabel(value) {
    if (!value) return "—";
    const date = new Date(`${String(value).slice(0, 10)}T00:00:00`);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" }).format(date);
  }

  function weekKey(day) {
    const date = new Date(`${day}T00:00:00`);
    const monday = new Date(date);
    monday.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    return monday.toISOString().slice(0, 10);
  }

  function number(value, digits) {
    const n = Number(value);
    if (value == null || !Number.isFinite(n)) return "—";
    return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function signed(value, suffix, digits) {
    const n = Number(value);
    if (value == null || !Number.isFinite(n)) return "—";
    const text = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${n < 0 ? "−" : n > 0 ? "+" : ""}${text}${suffix}`;
  }

  function compactNumber(value) {
    const n = Number(value);
    const abs = Math.abs(n);
    const sign = n < 0 ? "−" : "";
    if (abs >= 1e6) return `${sign}${(abs / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 })}M`;
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toLocaleString("en-US", { maximumFractionDigits: 1 })}K`;
    return `${sign}${abs.toLocaleString("en-US", { maximumFractionDigits: 1 })}`;
  }

  window.StockTriageMarket = { render };
})();
