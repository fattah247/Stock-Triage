/* Decision views: Overview, Stocks (screener and stock page) and Portfolio.
 *
 * The numbers come from /api/overview, /api/stocks, /api/stock and /api/portfolio
 * (dashboard/stocks.py). Every label goes in through textContent; charts are
 * plain SVG. Nothing here recommends a trade: it shows the numbers, how they
 * compare, and the warnings, so the decision stays with the reader.
 */
(function () {
  "use strict";

  const SVG = "http://www.w3.org/2000/svg";
  const STATE_LABELS = { calm: "Calm", elevated: "Elevated", stressed: "Stressed", crisis: "Crisis" };
  const SCORE_LABELS = { value: "Value", quality: "Quality", growth: "Growth", momentum: "Momentum", stability: "Stability" };
  const FLAG_LEVEL_LABELS = { risk: "Serious", watch: "Watch", note: "Note" };

  // ---------------------------------------------------------------- helpers

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function isNum(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  const fmt = {
    price(value) {
      if (!isNum(value)) return "–";
      return value.toLocaleString("en-US", { maximumFractionDigits: value < 100 ? 1 : 0 });
    },
    pct(value, digits = 1, signed = true) {
      if (!isNum(value)) return "–";
      const text = `${Math.abs(value).toFixed(digits)}%`;
      if (!signed) return `${value < 0 ? "−" : ""}${text}`;
      return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
    },
    pts(value) {
      if (!isNum(value)) return "–";
      return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(1)} pts`;
    },
    rp(value, signed = false) {
      if (!isNum(value)) return "–";
      const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
      const abs = Math.abs(value);
      const units = [
        [1e12, "tn"],
        [1e9, "bn"],
        [1e6, "m"],
        [1e4, "k"],
      ];
      for (const [size, suffix] of units) {
        if (abs >= size) {
          const scaled = abs / size;
          const digits = scaled >= 100 || suffix === "k" ? 0 : 1;
          return `${sign}Rp ${scaled.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${suffix}`;
        }
      }
      return `${sign}Rp ${abs.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
    },
    x(value) {
      return isNum(value) ? `${value.toFixed(1)}x` : "–";
    },
    num(value, digits = 2) {
      return isNum(value) ? value.toFixed(digits) : "–";
    },
    date(iso) {
      if (!iso) return "–";
      const parsed = new Date(`${String(iso).slice(0, 10)}T00:00:00`);
      if (Number.isNaN(parsed.getTime())) return String(iso);
      return parsed.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
    },
    dayMonth(iso) {
      if (!iso) return "–";
      const parsed = new Date(`${String(iso).slice(0, 10)}T00:00:00`);
      return Number.isNaN(parsed.getTime()) ? String(iso) : parsed.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    },
    month(iso) {
      if (!iso) return "–";
      const parsed = new Date(`${String(iso).slice(0, 7)}-01T00:00:00`);
      return Number.isNaN(parsed.getTime()) ? String(iso) : parsed.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    },
  };

  function signClass(value) {
    if (!isNum(value) || value === 0) return "";
    return value > 0 ? "up" : "down";
  }

  function signed(text, value) {
    return el("span", `num ${signClass(value)}`, text);
  }

  function openStock(ticker) {
    if (window.StockTriageApp && window.StockTriageApp.openStock) window.StockTriageApp.openStock(ticker);
  }

  function tickerLink(ticker, name) {
    const button = el("button", "dTicker", ticker);
    button.type = "button";
    if (name) button.title = name;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openStock(ticker);
    });
    return button;
  }

  function card(title, lead, extraClass) {
    const node = el("section", `dCard${extraClass ? ` ${extraClass}` : ""}`);
    if (title) {
      const head = el("div", "dCardHead");
      head.append(el("h3", "", title));
      if (lead) head.append(el("p", "dLead", lead));
      node.append(head);
    }
    return node;
  }

  function kpi(label, value, sub, valueClass) {
    const node = el("div", "dKpi");
    node.append(el("div", "dKpiLabel", label));
    const main = el("div", `dKpiValue${valueClass ? ` ${valueClass}` : ""}`);
    if (value instanceof Node) main.append(value);
    else main.textContent = value;
    node.append(main);
    if (sub) {
      const subNode = el("div", "dKpiSub");
      if (sub instanceof Node) subNode.append(sub);
      else subNode.textContent = sub;
      node.append(subNode);
    }
    return node;
  }

  function empty(title, body) {
    const node = el("div", "dEmpty");
    node.append(el("strong", "", title));
    if (body) {
      if (body instanceof Node) node.append(body);
      else node.append(el("p", "", body));
    }
    return node;
  }

  function flagPills(flags, limit = 2) {
    const wrap = el("span", "dFlags");
    if (!flags || !flags.length) {
      wrap.append(el("span", "dMuted", "–"));
      return wrap;
    }
    flags.slice(0, limit).forEach((flag) => {
      const pill = el("span", `dFlag lvl-${flag.level}`, flag.short || flag.text);
      pill.title = `${FLAG_LEVEL_LABELS[flag.level] || flag.level}: ${flag.text}`;
      wrap.append(pill);
    });
    if (flags.length > limit) {
      const more = el("span", "dFlagMore", `+${flags.length - limit}`);
      more.title = flags.slice(limit).map((flag) => flag.text).join("\n");
      wrap.append(more);
    }
    return wrap;
  }

  function flagList(flags) {
    if (!flags || !flags.length) return el("p", "dMuted", "No warnings on the numbers available.");
    const list = el("ul", "dFlagList");
    flags.forEach((flag) => {
      const item = el("li", `lvl-${flag.level}`);
      item.append(el("span", "dFlagLevel", FLAG_LEVEL_LABELS[flag.level] || flag.level), el("span", "", flag.text));
      list.append(item);
    });
    return list;
  }

  function scoreMini(scores) {
    const wrap = el("span", "dScoreMini");
    Object.keys(SCORE_LABELS).forEach((key) => {
      const value = scores ? scores[key] : null;
      const bar = el("span", "dScoreMiniBar");
      const fill = el("span", "dScoreMiniFill");
      fill.style.height = isNum(value) ? `${Math.max(6, value)}%` : "0";
      if (!isNum(value)) bar.classList.add("isEmpty");
      bar.append(fill);
      bar.title = `${SCORE_LABELS[key]}: ${isNum(value) ? value : "no data"}`;
      wrap.append(bar);
    });
    return wrap;
  }

  function scoreBars(scores, themes) {
    const wrap = el("div", "dScores");
    Object.keys(SCORE_LABELS).forEach((key) => {
      const value = scores ? scores[key] : null;
      const row = el("div", "dScoreRow");
      row.title = (themes && themes[key]) || "";
      const label = el("div", "dScoreLabel", SCORE_LABELS[key]);
      const track = el("div", "dScoreTrack");
      const fill = el("div", "dScoreFill");
      fill.style.width = isNum(value) ? `${value}%` : "0";
      track.append(fill, el("div", "dScoreMid"));
      const number = el("div", "dScoreNum", isNum(value) ? String(value) : "–");
      row.append(label, track, number);
      if (themes && themes[key]) row.append(el("div", "dScoreHint", themes[key]));
      wrap.append(row);
    });
    wrap.append(el("p", "dFoot", "Each score ranks the stock within its sector from 0 to 100; 50 is the sector's middle."));
    return wrap;
  }

  // A sortable table. columns: [{key, label, render(row) -> Node|string, value(row) -> sortable, align, title}]
  function dataTable(columns, rows, options = {}) {
    const state = { key: options.sortKey || null, dir: options.sortDir || "desc", limit: options.limit || 100 };
    const sorted = (key, dir) => {
      state.key = key;
      state.dir = dir;
      if (options.onSort) options.onSort(key, dir);
    };
    const wrap = el("div", "dTableWrap");
    function draw() {
      wrap.textContent = "";
      const sorted = rows.slice();
      if (state.key) {
        const column = columns.find((col) => col.key === state.key);
        const value = (column && column.value) || ((row) => row[state.key]);
        sorted.sort((a, b) => {
          const va = value(a);
          const vb = value(b);
          const ma = va === null || va === undefined || (typeof va === "number" && !Number.isFinite(va));
          const mb = vb === null || vb === undefined || (typeof vb === "number" && !Number.isFinite(vb));
          if (ma && mb) return 0;
          if (ma) return 1;
          if (mb) return -1;
          const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
          return state.dir === "asc" ? cmp : -cmp;
        });
      }
      const table = el("table", "dTable");
      const head = el("thead");
      const headRow = el("tr");
      columns.forEach((column) => {
        const th = el("th", column.align === "right" ? "num" : "");
        if (column.title) th.title = column.title;
        if (column.sortable === false) {
          th.textContent = column.label;
        } else {
          const button = el("button", "dSort", column.label);
          button.type = "button";
          if (state.key === column.key) {
            th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
            button.append(el("span", "dSortMark", state.dir === "asc" ? " ▲" : " ▼"));
          }
          button.addEventListener("click", () => {
            if (state.key === column.key) sorted(state.key, state.dir === "asc" ? "desc" : "asc");
            else sorted(column.key, column.firstDir || (column.align === "right" ? "desc" : "asc"));
            draw();
          });
          th.append(button);
        }
        headRow.append(th);
      });
      head.append(headRow);
      const body = el("tbody");
      sorted.slice(0, state.limit).forEach((row) => {
        const tr = el("tr");
        if (options.onRow) {
          tr.classList.add("isClickable");
          tr.tabIndex = 0;
          tr.addEventListener("click", () => options.onRow(row));
          tr.addEventListener("keydown", (event) => {
            if (event.key === "Enter") options.onRow(row);
          });
        }
        columns.forEach((column) => {
          const td = el("td", column.align === "right" ? "num" : "");
          const content = column.render ? column.render(row) : row[column.key];
          if (content instanceof Node) td.append(content);
          else td.textContent = content === null || content === undefined ? "–" : String(content);
          tr.append(td);
        });
        body.append(tr);
      });
      table.append(head, body);
      const scroller = el("div", "dTableScroll");
      scroller.append(table);
      wrap.append(scroller);
      if (!sorted.length) wrap.append(el("p", "dMuted dPad", options.empty || "Nothing to show."));
      if (sorted.length > state.limit) {
        const more = el("button", "dMore", `Show ${Math.min(100, sorted.length - state.limit)} more of ${sorted.length - state.limit} left`);
        more.type = "button";
        more.addEventListener("click", () => {
          state.limit += 100;
          draw();
        });
        wrap.append(more);
      }
    }
    draw();
    return wrap;
  }

  // A line chart with an optional second (comparison) line and hover readout. It is
  // drawn at the width it gets on the page, and again when that width changes, so
  // its text stays at its normal size.
  function lineChart(dates, lines, options = {}) {
    const wrap = el("div", "dChartWrap");
    const values = lines.flatMap((line) => line.values.filter(isNum));
    if (!values.length || dates.length < 2) {
      wrap.append(el("p", "dMuted", "Not enough history to chart."));
      return wrap;
    }
    if (options.legend) {
      const legend = el("div", "dLegend");
      lines.forEach((line) => {
        const item = el("span", `dLegendItem ${line.cls || ""}`);
        item.append(el("span", "dSwatch"), el("span", "", line.label));
        legend.append(item);
      });
      wrap.append(legend);
    }
    const holder = el("div", "dChartHolder");
    const readout = el("div", "dReadout");
    readout.textContent = " ";
    wrap.append(holder, readout);
    let drawnWidth = 0;
    const draw = (width) => {
      if (!width || Math.abs(width - drawnWidth) < 2) return;
      drawnWidth = width;
      holder.textContent = "";
      holder.append(chartSvg(dates, lines, values, options, width, readout));
    };
    if (typeof ResizeObserver === "function") {
      new ResizeObserver((entries) => draw(Math.round(entries[0].contentRect.width))).observe(holder);
    } else {
      draw(760);
    }
    return wrap;
  }

  function chartSvg(dates, lines, values, options, width, readout) {
    const height = options.height || 260;
    const margin = { top: 10, right: 58, bottom: 24, left: 4 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const node = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "dChart", role: "img" });
    node.setAttribute("aria-label", options.label || "Chart");
    let min = Math.min(...values);
    let max = Math.max(...values);
    const pad = (max - min) * 0.06 || max * 0.05 || 1;
    min -= pad;
    max += pad;
    const n = dates.length;
    const xAt = (i) => margin.left + (i / (n - 1)) * plotW;
    const yAt = (v) => margin.top + (1 - (v - min) / (max - min)) * plotH;

    if (options.bands) {
      // Shade stretches where bands[i] is truthy (e.g. a stressed or crisis market).
      let start = null;
      options.bands.forEach((on, i) => {
        if (on && start === null) start = i;
        const ends = start !== null && (!on || i === n - 1 || options.bands[i + 1] !== on);
        if (ends) {
          const end = on ? i : i - 1;
          node.append(
            svg("rect", {
              x: xAt(start),
              y: margin.top,
              width: Math.max(1, xAt(end) - xAt(start)),
              height: plotH,
              class: `dBand ${options.bandClass ? options.bandClass(options.bands[start]) : ""}`,
            })
          );
          start = null;
        }
      });
    }
    const ticks = 4;
    for (let t = 0; t <= ticks; t += 1) {
      const v = min + ((max - min) * t) / ticks;
      const y = yAt(v);
      node.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "dGrid" }));
      const label = svg("text", { x: width - margin.right + 6, y: y + 4, class: "dAxis" });
      label.textContent = options.yFormat ? options.yFormat(v) : fmt.price(v);
      node.append(label);
    }
    // Short spans label days, longer ones months; a label is never repeated.
    const spanDays = (new Date(dates[n - 1]) - new Date(dates[0])) / 86400000;
    const tickLabel = spanDays < 240 ? fmt.dayMonth : fmt.month;
    const xTicks = Math.max(1, Math.min(Math.floor(plotW / 110), n - 1));
    let lastLabel = null;
    for (let t = 0; t <= xTicks; t += 1) {
      const i = Math.round((t / xTicks) * (n - 1));
      const text = tickLabel(dates[i]);
      if (text === lastLabel) continue;
      lastLabel = text;
      const anchor = t === 0 ? "start" : t === xTicks ? "end" : "middle";
      const label = svg("text", { x: xAt(i), y: height - 6, class: "dAxis", "text-anchor": anchor });
      label.textContent = text;
      node.append(label);
    }
    lines.forEach((line) => {
      let d = "";
      line.values.forEach((v, i) => {
        if (!isNum(v)) return;
        d += `${d && isNum(line.values[i - 1]) ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`;
      });
      node.append(svg("path", { d, class: `dLine ${line.cls || ""}` }));
    });
    const cursor = svg("line", { y1: margin.top, y2: margin.top + plotH, class: "dCursor", visibility: "hidden" });
    const overlay = svg("rect", { x: margin.left, y: margin.top, width: plotW, height: plotH, class: "dOverlay" });
    node.append(cursor, overlay);
    overlay.addEventListener("mousemove", (event) => {
      const box = node.getBoundingClientRect();
      const x = ((event.clientX - box.left) / box.width) * width;
      const i = Math.max(0, Math.min(n - 1, Math.round(((x - margin.left) / plotW) * (n - 1))));
      cursor.setAttribute("x1", xAt(i));
      cursor.setAttribute("x2", xAt(i));
      cursor.setAttribute("visibility", "visible");
      readout.style.visibility = "visible";
      readout.textContent = [fmt.date(dates[i])]
        .concat(lines.map((line) => `${line.label}: ${(line.format || fmt.price)(line.values[i])}`))
        .join("   ");
    });
    overlay.addEventListener("mouseleave", () => {
      cursor.setAttribute("visibility", "hidden");
      readout.style.visibility = "hidden";
    });
    return node;
  }

  function rangeBar(low, high, value) {
    const wrap = el("div", "dRange");
    if (!isNum(low) || !isNum(high) || high <= low || !isNum(value)) return wrap;
    const track = el("div", "dRangeTrack");
    const marker = el("div", "dRangeMarker");
    marker.style.left = `${((value - low) / (high - low)) * 100}%`;
    track.append(marker);
    const labels = el("div", "dRangeLabels");
    labels.append(el("span", "", `Low ${fmt.price(low)}`), el("span", "", `High ${fmt.price(high)}`));
    wrap.append(track, labels);
    return wrap;
  }

  function stateBadge(regime) {
    return el("span", `dState st-${regime || "none"}`, STATE_LABELS[regime] || "No data");
  }

  function segmented(options, current, onPick, label) {
    const wrap = el("div", "dSegmented");
    wrap.setAttribute("role", "group");
    if (label) wrap.setAttribute("aria-label", label);
    options.forEach(([value, text]) => {
      const button = el("button", value === current ? "active" : "", text);
      button.type = "button";
      button.setAttribute("aria-pressed", value === current ? "true" : "false");
      button.addEventListener("click", () => onPick(value));
      wrap.append(button);
    });
    return wrap;
  }

  // ---------------------------------------------------------------- Overview

  function renderOverview(host, data) {
    host.textContent = "";
    if (!data || !data.as_of) {
      host.append(empty("No market data yet.", "Run market-sync to fetch IDX's daily data, then reload."));
      return;
    }
    const page = el("div", "dPage");
    page.append(marketStrip(data.market, data.as_of, data.stats));

    const grid = el("div", "dGrid2");
    grid.append(portfolioCard(data), watchlistCard(data.watchlist));
    page.append(grid);

    const movers = card("Today's biggest moves", "Stocks trading at least Rp 5 bn a day, by today's change.");
    const moverGrid = el("div", "dGrid2 dTight");
    moverGrid.append(moverList("Up", data.movers.up), moverList("Down", data.movers.down));
    movers.append(moverGrid);

    const sectors = card("Sectors", "Medians of each sector's stocks. P/E leaves out loss-makers.");
    sectors.append(
      dataTable(
        [
          { key: "sector", label: "Sector" },
          { key: "change_1d_pct", label: "Today", align: "right", render: (r) => signed(fmt.pct(r.change_1d_pct), r.change_1d_pct) },
          { key: "ret_1m_pct", label: "1 month", align: "right", render: (r) => signed(fmt.pct(r.ret_1m_pct), r.ret_1m_pct) },
          { key: "ret_1y_pct", label: "1 year", align: "right", render: (r) => signed(fmt.pct(r.ret_1y_pct), r.ret_1y_pct) },
          { key: "pe", label: "P/E", align: "right", render: (r) => fmt.x(r.pe) },
          { key: "pbv", label: "P/BV", align: "right", render: (r) => fmt.x(r.pbv) },
          { key: "roe_pct", label: "ROE", align: "right", render: (r) => fmt.pct(r.roe_pct, 1, false) },
          { key: "market_cap", label: "Market cap", align: "right", render: (r) => fmt.rp(r.market_cap) },
        ],
        data.sectors || [],
        { sortKey: "market_cap", sortDir: "desc" }
      )
    );
    const lower = el("div", "dGrid2");
    lower.append(movers, sectors);
    page.append(lower);
    page.append(comingUp(data.market));
    host.append(page);
  }

  function marketStrip(market, asOf, stats) {
    const node = card(null, null, "dMarketStrip");
    if (!market) {
      node.append(empty("No IHSG data yet.", "Run market-sync."));
      return node;
    }
    const latest = market.latest || {};
    const state = market.state || {};
    const head = el("div", "dStripHead");
    const title = el("div", "dStripTitle");
    title.append(el("span", "dEyebrow", `Market · ${fmt.date(latest.trade_date)} close`));
    const line = el("div", "dStripState");
    line.append(stateBadge(state.regime));
    if (state.since) line.append(el("span", "dMuted", `since ${fmt.date(state.since)}`));
    title.append(line);
    const driver = (state.drivers || [])[0];
    if (driver) title.append(el("p", "dStripWhy", driver.text || driver));
    const open = el("button", "dLinkButton", "Open the market view");
    open.type = "button";
    open.dataset.view = "market";
    title.append(open);
    head.append(title);

    const kpis = el("div", "dKpis");
    kpis.append(
      kpi("IHSG", fmt.price(latest.close), signed(fmt.pct(latest.change_pct), latest.change_pct)),
      kpi("Below 52-week high", fmt.pct(latest.drawdown_pct, 1, false), `high ${fmt.price(latest.peak)}`),
      kpi("Rupiah per US$", fmt.price(latest.usdidr), signed(`${fmt.pct(latest.usdidr_change_20_pct)} in 20 days`, -(latest.usdidr_change_20_pct || 0))),
      kpi("Foreign flow, 5 days", fmt.rp((latest.foreign_net_5d_bn || 0) * 1e9, true), "estimated net", signClass(latest.foreign_net_5d_bn)),
      kpi(
        "Stocks up / down today",
        `${stats ? stats.up : "–"} / ${stats ? stats.down : "–"}`,
        stats ? `${stats.stocks} listed` : null
      )
    );
    head.append(kpis);
    node.append(head);
    if (market.spark && market.spark.close && market.spark.close.length > 2) {
      const bands = market.spark.regime.map((r) => (r === "stressed" || r === "crisis" ? r : null));
      node.append(
        lineChart(market.spark.dates, [{ values: market.spark.close, label: "IHSG", cls: "isPrimary" }], {
          height: 150,
          bands,
          bandClass: (r) => `band-${r}`,
          label: "IHSG over the last six months",
        })
      );
      node.append(el("p", "dFoot", "IHSG over the last six months. Shading marks days the market state was stressed (light) or in crisis (dark)."));
    }
    const risk = market.crash_risk || {};
    const riskLine = el("p", "dFoot");
    riskLine.textContent = risk.trustworthy
      ? `Chance of a 10% fall within three months: ${fmt.pct((risk.probability || 0) * 100, 0, false)} (usually ${fmt.pct((risk.base_rate || 0) * 100, 0, false)}).`
      : "Crash-risk estimate withheld: on past data it does not predict falls better than the long-run average.";
    node.append(riskLine);
    return node;
  }

  function portfolioCard(data) {
    const node = card("Your portfolio", null);
    const p = data.portfolio;
    if (data.portfolio_error) {
      node.append(empty("The holdings file has a problem.", data.portfolio_error));
      return node;
    }
    if (!p) {
      node.append(holdingsHelp(data.portfolio_path));
      return node;
    }
    const kpis = el("div", "dKpis dKpisCompact");
    kpis.append(
      kpi("Total value", fmt.rp(p.total), `cash ${fmt.pct(p.cash_pct, 0, false)}`),
      kpi("Today", signed(fmt.rp(p.day_change, true), p.day_change), signed(fmt.pct(p.day_change_pct), p.day_change_pct)),
      kpi("Gain since bought", signed(fmt.rp(p.gain, true), p.gain), signed(fmt.pct(p.gain_pct), p.gain_pct)),
      kpi("Moves with IHSG", isNum(p.beta) ? `${p.beta.toFixed(2)}x` : "–", "beta, by value")
    );
    node.append(kpis);
    node.append(positionsTable(p.positions, true));
    return node;
  }

  function holdingsHelp(path) {
    const wrap = el("div", "dEmpty");
    wrap.append(el("strong", "", "Add your holdings to see your portfolio here."));
    wrap.append(el("p", "", "Create this file on your computer; it never leaves it:"));
    wrap.append(el("code", "dPath", path || "data/portfolio.yaml"));
    const example = el("pre", "dCode");
    example.textContent = "cash: 25000000\nholdings:\n  - ticker: BBCA\n    lots: 20\n    avg_price: 8750\n  - ticker: ANTM\n    lots: 50\n    avg_price: 1520";
    wrap.append(example);
    wrap.append(el("p", "dMuted", "One lot is 100 shares. Reload the page after saving."));
    return wrap;
  }

  function positionsTable(positions, compact) {
    const columns = [
      { key: "ticker", label: "Stock", render: (r) => tickerCell(r) },
      { key: "value", label: "Value", align: "right", render: (r) => (r.missing ? "not listed" : fmt.rp(r.value)) },
      { key: "weight_pct", label: "Weight", align: "right", render: (r) => fmt.pct(r.weight_pct, 1, false) },
      { key: "change_1d_pct", label: "Today", align: "right", render: (r) => signed(fmt.pct(r.change_1d_pct), r.change_1d_pct) },
      { key: "gain_pct", label: "Gain", align: "right", render: (r) => signed(fmt.pct(r.gain_pct), r.gain_pct) },
      { key: "flags", label: "Warnings", sortable: false, render: (r) => flagPills(r.flags, 1) },
    ];
    if (!compact) {
      columns.splice(
        1,
        0,
        { key: "lots", label: "Lots", align: "right", render: (r) => fmt.price(r.lots) },
        { key: "avg_price", label: "Avg price", align: "right", render: (r) => fmt.price(r.avg_price) },
        { key: "close", label: "Last", align: "right", render: (r) => fmt.price(r.close) }
      );
      columns.splice(
        columns.length - 1,
        0,
        { key: "gain", label: "Gain (Rp)", align: "right", render: (r) => signed(fmt.rp(r.gain, true), r.gain) },
        { key: "below_high_pct", label: "Below 52w high", align: "right", render: (r) => fmt.pct(r.below_high_pct, 0, false) },
        { key: "pe", label: "P/E", align: "right", render: (r) => fmt.x(r.pe) },
        { key: "dividend_yield_pct", label: "Div. yield", align: "right", render: (r) => fmt.pct(r.dividend_yield_pct, 1, false) }
      );
    }
    return dataTable(columns, positions || [], { sortKey: "value", sortDir: "desc", onRow: (r) => openStock(r.ticker), empty: "No holdings." });
  }

  function tickerCell(row) {
    const wrap = el("span", "dTickerCell");
    wrap.append(tickerLink(row.ticker, row.name));
    if (row.name) wrap.append(el("span", "dName", row.name));
    return wrap;
  }

  function watchlistCard(rows) {
    const node = card("Watchlist", "Star a stock on its page to add it.");
    if (!rows || !rows.length) {
      node.append(el("p", "dMuted", "No stocks on the watchlist yet."));
      return node;
    }
    node.append(
      dataTable(
        [
          { key: "ticker", label: "Stock", render: (r) => tickerCell(r) },
          { key: "close", label: "Price", align: "right", render: (r) => (r.missing ? "not listed" : fmt.price(r.close)) },
          { key: "change_1d_pct", label: "Today", align: "right", render: (r) => signed(fmt.pct(r.change_1d_pct), r.change_1d_pct) },
          { key: "ret_1m_pct", label: "1 month", align: "right", render: (r) => signed(fmt.pct(r.ret_1m_pct), r.ret_1m_pct) },
          { key: "flags", label: "Warnings", sortable: false, render: (r) => flagPills(r.flags, 1) },
        ],
        rows,
        { sortKey: "change_1d_pct", sortDir: "desc", onRow: (r) => openStock(r.ticker) }
      )
    );
    return node;
  }

  function moverList(title, rows) {
    const wrap = el("div", "dMovers");
    wrap.append(el("h4", "", title));
    const list = el("ol", "dMoverList");
    (rows || []).forEach((row) => {
      const item = el("li");
      item.append(tickerLink(row.ticker, row.name), el("span", "dName", row.name || ""), signed(fmt.pct(row.change_1d_pct), row.change_1d_pct));
      list.append(item);
    });
    wrap.append(list);
    return wrap;
  }

  function comingUp(market) {
    const node = card("Coming up and in the news", "Scheduled risk dates and the risk themes active in the news.");
    const grid = el("div", "dGrid2 dTight");
    const dates = el("div");
    dates.append(el("h4", "", "Risk dates"));
    const list = el("ul", "dPlainList");
    ((market && market.calendar) || []).forEach((item) => {
      const li = el("li");
      li.append(el("span", "dDate", fmt.date(item.date)), el("span", "", item.title));
      if (item.severity === "high") li.append(el("span", "dTag", "high"));
      list.append(li);
    });
    if (!list.children.length) list.append(el("li", "dMuted", "Nothing scheduled in the next 90 days."));
    dates.append(list);
    const themes = el("div");
    themes.append(el("h4", "", "Active risk themes"));
    const themeList = el("ul", "dPlainList");
    ((market && market.themes) || []).forEach((item) => {
      const li = el("li");
      li.append(el("span", "", item.title), el("span", "dMuted", `${item.items || 0} items`));
      if (item.severity === "high") li.append(el("span", "dTag", "high"));
      themeList.append(li);
    });
    if (!themeList.children.length) themeList.append(el("li", "dMuted", "No risk theme is active."));
    themes.append(themeList);
    grid.append(dates, themes);
    node.append(grid);
    return node;
  }

  // ---------------------------------------------------------------- Stocks (screener)

  const PRESETS = [
    { id: "all", label: "All stocks", hint: "Every listed stock, largest first.", test: () => true },
    {
      id: "quality",
      label: "Quality at a fair price",
      hint: "ROE at least 15%, profitable, P/E at or below its peers' median, debt at most 1x equity (outside financials), trading at least Rp 5 bn a day.",
      test: (r) =>
        r.roe_pct >= 15 &&
        !r.loss_making &&
        isNum(r.pe) &&
        isNum(r.peer_pe) &&
        r.pe <= r.peer_pe &&
        (r.sector === "Financials" || !isNum(r.der) || r.der <= 1) &&
        r.avg_value_20d >= 5e9,
    },
    {
      id: "cheap",
      label: "Cheap against its own past",
      hint: "P/E in the bottom 20% of its range since 2021, profitable, trading at least Rp 1 bn a day.",
      test: (r) => isNum(r.pe_history_pct) && r.pe_history_pct <= 20 && !r.loss_making && r.avg_value_20d >= 1e9,
    },
    {
      id: "dividend",
      label: "Dividend payers",
      hint: "Paid at least 4% of today's price in the past year, paid in three or more years since 2019, profitable, payout at most 100% of earnings, trading at least Rp 1 bn a day.",
      test: (r) =>
        r.dividend_yield_pct >= 4 &&
        r.dividend_years >= 3 &&
        !r.loss_making &&
        (!isNum(r.payout_pct) || r.payout_pct <= 100) &&
        r.avg_value_20d >= 1e9,
    },
    {
      id: "trend",
      label: "Strong trend",
      hint: "Beat IHSG over the past year, above its 200-day average, within 15% of its 52-week high, trading at least Rp 5 bn a day.",
      test: (r) => r.rel_1y_pts > 0 && r.above_ma200 === true && r.below_high_pct <= 15 && r.avg_value_20d >= 5e9,
    },
    {
      id: "fallen",
      label: "Fallen but profitable",
      hint: "At least 30% below its 52-week high, still profitable with ROE of 10% or more, trading at least Rp 5 bn a day.",
      test: (r) => r.below_high_pct >= 30 && !r.loss_making && r.roe_pct >= 10 && r.avg_value_20d >= 5e9,
    },
    {
      id: "flags",
      label: "Serious warnings",
      hint: "Losing money, negative equity, suspended or thinly traded.",
      test: (r) => (r.flags || []).some((flag) => flag.level === "risk"),
    },
    { id: "watchlist", label: "Watchlist", hint: "The stocks you starred.", test: (r, ctx) => ctx.watchlist.has(r.ticker) },
  ];

  const COLUMN_SETS = {
    summary: ["stock", "close", "change_1d_pct", "ret_1y_pct", "market_cap", "pe", "dividend_yield_pct", "roe_pct", "scores", "flags"],
    valuation: ["stock", "close", "pe", "peer_pe", "pe_history_pct", "pbv", "peer_pbv", "earnings_yield_pct", "dividend_yield_pct", "payout_pct", "market_cap"],
    quality: ["stock", "roe_pct", "peer_roe_pct", "npm_pct", "der", "sales_yoy_pct", "profit_yoy_pct", "fs_date"],
    price: ["stock", "change_1d_pct", "ret_1m_pct", "ret_3m_pct", "ret_1y_pct", "rel_1y_pts", "below_high_pct", "volatility_pct", "beta", "avg_value_20d", "foreign_net_20d"],
  };
  const COLUMN_SET_LABELS = [
    ["summary", "Summary"],
    ["valuation", "Valuation"],
    ["quality", "Profit and growth"],
    ["price", "Price and risk"],
  ];

  const COLUMNS = {
    stock: { key: "ticker", label: "Stock", render: (r) => tickerCell(r), value: (r) => r.ticker },
    close: { key: "close", label: "Price", align: "right", render: (r) => fmt.price(r.close) },
    change_1d_pct: { key: "change_1d_pct", label: "Today", align: "right", render: (r) => signed(fmt.pct(r.change_1d_pct), r.change_1d_pct) },
    ret_1m_pct: { key: "ret_1m_pct", label: "1 month", align: "right", render: (r) => signed(fmt.pct(r.ret_1m_pct), r.ret_1m_pct) },
    ret_3m_pct: { key: "ret_3m_pct", label: "3 months", align: "right", render: (r) => signed(fmt.pct(r.ret_3m_pct), r.ret_3m_pct) },
    ret_1y_pct: { key: "ret_1y_pct", label: "1 year", align: "right", render: (r) => signed(fmt.pct(r.ret_1y_pct), r.ret_1y_pct) },
    rel_1y_pts: { key: "rel_1y_pts", label: "vs IHSG, 1y", align: "right", title: "The stock's one-year return minus IHSG's, in percentage points.", render: (r) => signed(fmt.pts(r.rel_1y_pts), r.rel_1y_pts) },
    market_cap: { key: "market_cap", label: "Market cap", align: "right", render: (r) => fmt.rp(r.market_cap) },
    pe: { key: "pe", label: "P/E", align: "right", firstDir: "asc", title: "Price over the last 12 months' earnings per share. Empty when losing money.", render: (r) => (r.loss_making ? el("span", "dMuted", "loss") : fmt.x(r.pe)) },
    peer_pe: { key: "peer_pe", label: "Peers' P/E", align: "right", title: "Median P/E of the same industry (or sector when the industry is small).", render: (r) => fmt.x(r.peer_pe) },
    pe_history_pct: { key: "pe_history_pct", label: "P/E vs its past", align: "right", firstDir: "asc", title: "Where today's P/E sits in its monthly range since 2021: 0% is its cheapest, 100% its dearest.", render: (r) => fmt.pct(r.pe_history_pct, 0, false) },
    pbv: { key: "pbv", label: "P/BV", align: "right", firstDir: "asc", title: "Price over book value (equity) per share.", render: (r) => fmt.x(r.pbv) },
    peer_pbv: { key: "peer_pbv", label: "Peers' P/BV", align: "right", render: (r) => fmt.x(r.peer_pbv) },
    earnings_yield_pct: { key: "earnings_yield_pct", label: "Earnings yield", align: "right", title: "Earnings per share over price: the inverse of P/E.", render: (r) => fmt.pct(r.earnings_yield_pct, 1, false) },
    dividend_yield_pct: { key: "dividend_yield_pct", label: "Div. yield", align: "right", title: "Cash dividends paid per share over the past year, as a share of today's price.", render: (r) => fmt.pct(r.dividend_yield_pct, 1, false) },
    payout_pct: { key: "payout_pct", label: "Payout", align: "right", firstDir: "asc", title: "Dividends over the past year as a share of trailing earnings per share.", render: (r) => fmt.pct(r.payout_pct, 0, false) },
    roe_pct: { key: "roe_pct", label: "ROE", align: "right", title: "Return on equity: profit over shareholders' equity, trailing.", render: (r) => fmt.pct(r.roe_pct, 1, false) },
    peer_roe_pct: { key: "peer_roe_pct", label: "Peers' ROE", align: "right", render: (r) => fmt.pct(r.peer_roe_pct, 1, false) },
    npm_pct: { key: "npm_pct", label: "Net margin", align: "right", render: (r) => fmt.pct(r.npm_pct, 1, false) },
    der: { key: "der", label: "Debt / equity", align: "right", firstDir: "asc", render: (r) => fmt.x(r.der) },
    sales_yoy_pct: { key: "sales_yoy_pct", label: "Sales vs a year ago", align: "right", render: (r) => signed(fmt.pct(r.sales_yoy_pct, 0), r.sales_yoy_pct) },
    profit_yoy_pct: { key: "profit_yoy_pct", label: "Profit vs a year ago", align: "right", render: (r) => signed(fmt.pct(r.profit_yoy_pct, 0), r.profit_yoy_pct) },
    fs_date: { key: "fs_date", label: "Statement", align: "right", render: (r) => fmt.date(r.fs_date) },
    below_high_pct: { key: "below_high_pct", label: "Below 52w high", align: "right", render: (r) => fmt.pct(r.below_high_pct, 0, false) },
    volatility_pct: { key: "volatility_pct", label: "Volatility", align: "right", firstDir: "asc", title: "How much the price swings, annualised, over the last 60 sessions.", render: (r) => fmt.pct(r.volatility_pct, 0, false) },
    beta: { key: "beta", label: "Beta", align: "right", title: "How much the stock tends to move when IHSG moves 1%, over the past year.", render: (r) => fmt.num(r.beta) },
    avg_value_20d: { key: "avg_value_20d", label: "Traded a day", align: "right", title: "Average value traded per day over 20 sessions: how easily you can buy or sell.", render: (r) => fmt.rp(r.avg_value_20d) },
    foreign_net_20d: { key: "foreign_net_20d", label: "Foreign, 20 days", align: "right", title: "Foreign investors' estimated net buying (+) or selling (−) over 20 sessions.", render: (r) => signed(fmt.rp(r.foreign_net_20d, true), r.foreign_net_20d) },
    scores: { key: "scores", label: "Scores", sortable: false, title: "Value, quality, growth, momentum and stability, each ranked within the sector.", render: (r) => scoreMini(r.scores) },
    flags: { key: "flags", label: "Warnings", value: (r) => (r.flags || []).filter((flag) => flag.level !== "note").length, render: (r) => flagPills((r.flags || []).filter((flag) => flag.level !== "note"), 1) },
  };

  const screen = loadScreenPrefs();

  function loadScreenPrefs() {
    const defaults = {
      preset: "all", sector: "", liquidity: 0, sharia: false, hideLoss: false, query: "", columns: "summary",
      sortKey: "market_cap", sortDir: "desc",
    };
    try {
      const saved = JSON.parse(window.localStorage.getItem("stockTriage.screen") || "null");
      return saved && typeof saved === "object" ? { ...defaults, ...saved, query: "" } : defaults;
    } catch (_) {
      return defaults;
    }
  }

  function saveScreenPrefs() {
    try {
      const { query, ...rest } = screen;
      window.localStorage.setItem("stockTriage.screen", JSON.stringify(rest));
    } catch (_) {
      // Not remembered; the filters still apply now.
    }
  }

  function renderStocks(host, data) {
    host.textContent = "";
    if (!data || !data.rows || !data.rows.length) {
      host.append(empty("No stock data yet.", "Run market-sync: it stores every stock's daily numbers and IDX's monthly ratio table."));
      return;
    }
    const page = el("div", "dPage");
    const head = el("div", "dScreenHead");
    const title = el("div");
    title.append(el("h2", "dTitle", "Stocks"), el("p", "dLead", `Every IDX stock with its numbers, as of the ${fmt.date(data.as_of)} close. Pick a screen, narrow it, then open a stock.`));
    head.append(title);
    page.append(head);

    const presetRow = el("div", "dPresets");
    PRESETS.forEach((preset) => {
      const button = el("button", `dPreset${screen.preset === preset.id ? " active" : ""}`, preset.label);
      button.type = "button";
      button.title = preset.hint;
      button.addEventListener("click", () => {
        screen.preset = preset.id;
        saveScreenPrefs();
        renderStocks(host, data);
      });
      presetRow.append(button);
    });
    page.append(presetRow);
    const preset = PRESETS.find((item) => item.id === screen.preset) || PRESETS[0];
    page.append(el("p", "dPresetHint", preset.hint));

    const filters = el("div", "dFilters");
    const search = el("input", "dInput");
    search.type = "search";
    search.placeholder = "Filter by ticker or name";
    search.value = screen.query;
    search.setAttribute("aria-label", "Filter stocks by ticker or name");
    const sector = el("select", "dSelect");
    sector.setAttribute("aria-label", "Sector");
    [["", "All sectors"]].concat(data.sectors.map((name) => [name, name])).forEach(([value, label]) => {
      const option = el("option", "", label);
      option.value = value;
      if (value === screen.sector) option.selected = true;
      sector.append(option);
    });
    const liquidity = el("select", "dSelect");
    liquidity.setAttribute("aria-label", "Minimum traded a day");
    [
      [0, "Any liquidity"],
      [1e9, "Trades ≥ Rp 1 bn a day"],
      [5e9, "Trades ≥ Rp 5 bn a day"],
      [20e9, "Trades ≥ Rp 20 bn a day"],
    ].forEach(([value, label]) => {
      const option = el("option", "", label);
      option.value = String(value);
      if (Number(value) === Number(screen.liquidity)) option.selected = true;
      liquidity.append(option);
    });
    const sharia = checkbox("Sharia only", screen.sharia);
    const hideLoss = checkbox("Hide loss-makers", screen.hideLoss);
    filters.append(search, sector, liquidity, sharia.label, hideLoss.label);
    page.append(filters);

    const tableHost = el("div");
    const countLine = el("div", "dCountLine");
    const columnPicker = el("div");
    const tools = el("div", "dTableTools");
    tools.append(countLine, columnPicker);
    page.append(tools, tableHost);

    const watchlist = new Set((data.watchlist || []).map((t) => t.toUpperCase()));
    function apply() {
      const q = screen.query.trim().toLowerCase();
      const rows = data.rows.filter((row) => {
        if (!preset.test(row, { watchlist })) return false;
        if (screen.sector && row.sector !== screen.sector) return false;
        if (screen.liquidity && !((row.avg_value_20d || 0) >= Number(screen.liquidity))) return false;
        if (screen.sharia && !row.sharia) return false;
        if (screen.hideLoss && row.loss_making) return false;
        if (q && !`${row.ticker} ${row.name || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
      countLine.textContent = `${rows.length} of ${data.rows.length} stocks`;
      columnPicker.textContent = "";
      columnPicker.append(
        segmented(COLUMN_SET_LABELS, screen.columns, (value) => {
          screen.columns = value;
          saveScreenPrefs();
          apply();
        }, "Columns")
      );
      tableHost.textContent = "";
      const columns = (COLUMN_SETS[screen.columns] || COLUMN_SETS.summary).map((key) => COLUMNS[key]);
      tableHost.append(
        dataTable(columns, rows, {
          // The sort survives filtering and switching columns; a hidden column still sorts.
          sortKey: screen.sortKey,
          sortDir: screen.sortDir,
          onSort: (key, dir) => {
            screen.sortKey = key;
            screen.sortDir = dir;
            saveScreenPrefs();
          },
          onRow: (row) => openStock(row.ticker),
          empty: "No stock passes these filters.",
        })
      );
    }
    search.addEventListener("input", () => {
      screen.query = search.value;
      apply();
    });
    sector.addEventListener("change", () => {
      screen.sector = sector.value;
      saveScreenPrefs();
      apply();
    });
    liquidity.addEventListener("change", () => {
      screen.liquidity = Number(liquidity.value);
      saveScreenPrefs();
      apply();
    });
    sharia.input.addEventListener("change", () => {
      screen.sharia = sharia.input.checked;
      saveScreenPrefs();
      apply();
    });
    hideLoss.input.addEventListener("change", () => {
      screen.hideLoss = hideLoss.input.checked;
      saveScreenPrefs();
      apply();
    });
    apply();
    page.append(el("p", "dFoot", "Prices are IDX closes adjusted for splits and rights. Ratios come from IDX's monthly Financial Data and Ratio table, at the latest price. Returns leave out dividends."));
    host.append(page);
  }

  function checkbox(text, checked) {
    const label = el("label", "dCheck");
    const input = el("input");
    input.type = "checkbox";
    input.checked = Boolean(checked);
    label.append(input, el("span", "", text));
    return { label, input };
  }

  // ---------------------------------------------------------------- Stock page

  let chartRange = 252;

  function renderStock(host, detail) {
    host.textContent = "";
    const row = detail.row;
    const page = el("div", "dPage");

    const back = el("button", "dBack", "← All stocks");
    back.type = "button";
    back.addEventListener("click", () => openStock(null));
    page.append(back);

    const header = el("div", "dStockHead");
    const id = el("div", "dStockId");
    const tickerLine = el("div", "dStockTicker");
    tickerLine.append(el("h2", "", row.ticker));
    const star = el("button", `dStar${detail.watchlisted ? " active" : ""}`, detail.watchlisted ? "★ On watchlist" : "☆ Add to watchlist");
    star.type = "button";
    star.addEventListener("click", async () => {
      if (window.StockTriageApp && window.StockTriageApp.toggleWatchlist) {
        const on = await window.StockTriageApp.toggleWatchlist(row.ticker);
        detail.watchlisted = on;
        star.className = `dStar${on ? " active" : ""}`;
        star.textContent = on ? "★ On watchlist" : "☆ Add to watchlist";
      }
    });
    tickerLine.append(star);
    id.append(tickerLine, el("div", "dStockName", row.name));
    const tags = el("div", "dTags");
    [row.sector, row.industry].filter(Boolean).forEach((text) => tags.append(el("span", "dTag", text)));
    if (row.sharia) tags.append(el("span", "dTag", "Sharia"));
    id.append(tags);
    const price = el("div", "dStockPrice");
    price.append(el("div", "dBigNum", fmt.price(row.close)), signed(`${fmt.pct(row.change_1d_pct)} today`, row.change_1d_pct));
    price.append(el("div", "dMuted", `${fmt.date(row.as_of)} close · market cap ${fmt.rp(row.market_cap)}`));
    header.append(id, price);
    page.append(header);

    if (detail.market_state === "stressed" || detail.market_state === "crisis") {
      const note = el("div", "dMarketNote");
      note.append(stateBadge(detail.market_state));
      note.append(
        el(
          "span",
          "",
          `The whole market is ${detail.market_state === "crisis" ? "in crisis" : "stressed"}.` +
            (isNum(row.beta) ? ` This stock has tended to move ${row.beta.toFixed(1)}x as much as IHSG over the past year.` : "")
        )
      );
      page.append(note);
    }

    const top = el("div", "dGrid2 dWideLeft");
    const chartCard = card("Price", "Adjusted for splits and rights. The dashed line is IHSG, scaled to start at the same point.");
    const chartBody = el("div");
    function drawChart() {
      chartBody.textContent = "";
      const prices = detail.prices;
      const start = Number.isFinite(chartRange) ? Math.max(0, prices.dates.length - chartRange) : 0;
      const dates = prices.dates.slice(start);
      const closes = prices.close.slice(start);
      const ihsg = prices.ihsg_rebased.slice(start);
      const base = ihsg.find(isNum);
      const scale = isNum(base) && closes[0] ? closes[0] / base : 1;
      chartBody.append(
        segmented(
          [
            [126, "6M"],
            [252, "1Y"],
            [756, "3Y"],
            [Infinity, "All"],
          ],
          chartRange,
          (value) => {
            chartRange = value;
            drawChart();
          },
          "Chart range"
        )
      );
      chartBody.append(
        lineChart(
          dates,
          [
            { values: closes, label: row.ticker, cls: "isPrimary" },
            { values: ihsg.map((v) => (isNum(v) ? v * scale : null)), label: "IHSG (scaled)", cls: "isCompare" },
          ],
          { legend: true, label: `${row.ticker} price against IHSG` }
        )
      );
    }
    drawChart();
    chartCard.append(chartBody);
    chartCard.append(el("h4", "dSub", "52-week range"), rangeBar(row.low_52w, row.high_52w, row.close));
    const returns = el("div", "dReturns");
    [
      ["1 month", row.ret_1m_pct],
      ["3 months", row.ret_3m_pct],
      ["6 months", row.ret_6m_pct],
      ["This year", row.ret_ytd_pct],
      ["1 year", row.ret_1y_pct],
    ].forEach(([label, value]) => {
      const item = el("div", "dReturn");
      item.append(el("span", "dMuted", label), signed(fmt.pct(value), value));
      returns.append(item);
    });
    chartCard.append(returns);

    const side = el("div", "dStack");
    const warn = card("Warnings", "What deserves a closer look before buying or holding.");
    warn.append(flagList(row.flags));
    if (row.notices && row.notices.length) {
      const list = el("ul", "dPlainList");
      row.notices.slice(0, 4).forEach((notice) => {
        const li = el("li");
        li.append(el("span", "dDate", fmt.date(notice.date)), el("span", "", notice.title));
        list.append(li);
      });
      warn.append(el("h4", "dSub", "Exchange notices, last 30 days"), list);
    }
    const scores = card("Scores within its sector", null);
    scores.append(scoreBars(row.scores, detail.score_themes));
    side.append(warn, scores);
    top.append(chartCard, side);
    page.append(top);

    page.append(keyNumbers(row));

    const history = el("div", "dGrid2");
    history.append(statementsCard(detail.statements), dividendsCard(detail.dividends));
    page.append(history, valuationHistoryCard(detail.ratio_history, row));

    const peers = card("Peers", `${row.peer_group || "Same sector"}: the largest by market cap. Click one to open it.`);
    peers.append(
      dataTable(
        [
          COLUMNS.stock,
          COLUMNS.close,
          COLUMNS.market_cap,
          COLUMNS.pe,
          COLUMNS.pbv,
          COLUMNS.roe_pct,
          COLUMNS.npm_pct,
          COLUMNS.profit_yoy_pct,
          COLUMNS.dividend_yield_pct,
          COLUMNS.ret_1y_pct,
          COLUMNS.scores,
        ],
        detail.peers || [],
        { sortKey: "market_cap", sortDir: "desc", onRow: (peer) => openStock(peer.ticker) }
      )
    );
    page.append(peers);

    const research = card("Filings and checks", "The filing workspace has the statements, extracted figures and filing checks for this company.");
    const openFilings = el("button", "dLinkButton", `Open ${row.ticker}'s filings`);
    openFilings.type = "button";
    openFilings.addEventListener("click", () => {
      if (window.StockTriageApp && window.StockTriageApp.openCompany) window.StockTriageApp.openCompany(row.ticker);
    });
    research.append(openFilings);
    page.append(research);
    page.append(el("p", "dFoot", "Numbers describe the company and its price; they are not advice. Returns leave out dividends."));
    host.append(page);
  }

  function peers(value, format) {
    return isNum(value) ? `peers ${format(value)}` : "";
  }

  function keyNumbers(row) {
    const node = card("Key numbers", "Each compared with its peers' median and, for valuation, with its own range since 2021.");
    const grid = el("div", "dNumGrid");
    const groups = [
      {
        title: "Valuation",
        rows: [
          ["P/E", row.loss_making ? "loss" : fmt.x(row.pe), peers(row.peer_pe, fmt.x), "Price over trailing earnings per share."],
          ["P/E vs its past", isNum(row.pe_history_pct) ? `${fmt.pct(row.pe_history_pct, 0, false)} of range` : "–", "0% = cheapest since 2021", "Where today's P/E sits in its monthly range since 2021."],
          ["P/BV", fmt.x(row.pbv), peers(row.peer_pbv, fmt.x), "Price over book value per share."],
          ["P/BV vs its past", isNum(row.pbv_history_pct) ? `${fmt.pct(row.pbv_history_pct, 0, false)} of range` : "–", "", ""],
          ["Earnings yield", fmt.pct(row.earnings_yield_pct, 1, false), "earnings per Rp 100 of price", ""],
          ["Dividend yield", fmt.pct(row.dividend_yield_pct, 1, false), isNum(row.dividends_12m) && row.dividends_12m ? `Rp ${fmt.price(row.dividends_12m)} a share, past year` : peers(row.peer_dividend_yield_pct, (v) => fmt.pct(v, 1, false)), "Cash paid per share over the past year, over today's price."],
          ["Payout", fmt.pct(row.payout_pct, 0, false), isNum(row.dividend_years) ? `paid in ${row.dividend_years} years since 2019` : "", "Dividends over the past year as a share of earnings per share."],
        ],
      },
      {
        title: "Profitability and balance sheet",
        rows: [
          ["ROE", fmt.pct(row.roe_pct, 1, false), peers(row.peer_roe_pct, (v) => fmt.pct(v, 1, false)), "Profit over shareholders' equity."],
          ["ROA", fmt.pct(row.roa_pct, 1, false), "", "Profit over total assets."],
          ["Net margin", fmt.pct(row.npm_pct, 1, false), peers(row.peer_npm_pct, (v) => fmt.pct(v, 1, false)), "Profit per rupiah of sales."],
          ["Debt / equity", fmt.x(row.der), row.sector === "Financials" ? "high by nature for banks" : peers(row.peer_der, fmt.x), "Liabilities over equity."],
          ["Equity", fmt.rp(row.equity), "", ""],
        ],
      },
      {
        title: "Growth",
        rows: [
          ["Sales vs a year ago", fmt.pct(row.sales_yoy_pct, 0), "same period", ""],
          ["Profit vs a year ago", fmt.pct(row.profit_yoy_pct, 0), "same period", ""],
          ["Sales, year to date", fmt.rp(row.sales), "", ""],
          ["Profit, year to date", fmt.rp(row.profit), "to owners", ""],
          ["Latest statement", fmt.date(row.fs_date), row.fs_type || "", ""],
        ],
      },
      {
        title: "Trading and risk",
        rows: [
          ["vs IHSG, 1 year", fmt.pts(row.rel_1y_pts), "", "The stock's one-year return minus IHSG's."],
          ["Below 52-week high", fmt.pct(row.below_high_pct, 0, false), "", ""],
          ["Volatility", fmt.pct(row.volatility_pct, 0, false), "a year, 60 sessions", "How much the price swings."],
          ["Beta", fmt.num(row.beta), "vs IHSG, 1 year", "1.0 moves with the market; above 1 swings more."],
          ["Traded a day", fmt.rp(row.avg_value_20d), `${row.untraded_days_20d || 0} quiet days in 20`, "Average value traded over 20 sessions."],
          ["Foreign, 20 days", fmt.rp(row.foreign_net_20d, true), "estimated net", ""],
        ],
      },
    ];
    groups.forEach((group) => {
      const box = el("div", "dNumGroup");
      box.append(el("h4", "", group.title));
      const dl = el("dl", "dNums");
      group.rows.forEach(([label, value, sub, hint]) => {
        const dt = el("dt", "", label);
        if (hint) dt.title = hint;
        const dd = el("dd");
        dd.append(el("span", "dNumValue", value));
        if (sub) dd.append(el("span", "dNumSub", sub));
        dl.append(dt, dd);
      });
      box.append(dl);
      grid.append(box);
    });
    node.append(grid);
    if (!row.has_fundamentals) node.append(el("p", "dFoot", "IDX's ratio table has no figures for this stock yet; run market-sync to fetch it."));
    return node;
  }

  function statementsCard(statements) {
    const node = card("Results by statement", "Year to date at each statement date, from IDX's ratio table.");
    const rows = (statements || []).slice(-8).reverse();
    node.append(
      dataTable(
        [
          { key: "fs_date", label: "Statement", render: (r) => `${fmt.date(r.fs_date)}${r.fs_type ? ` · ${r.fs_type}` : ""}`, value: (r) => r.fs_date },
          { key: "sales", label: "Sales", align: "right", render: (r) => fmt.rp(r.sales) },
          { key: "profit_attr_owner", label: "Profit", align: "right", render: (r) => signed(fmt.rp(r.profit_attr_owner), r.profit_attr_owner) },
          { key: "equity", label: "Equity", align: "right", render: (r) => fmt.rp(r.equity) },
          { key: "eps", label: "EPS (12m)", align: "right", render: (r) => fmt.num(r.eps, 1) },
        ],
        rows,
        { sortKey: "fs_date", sortDir: "desc", empty: "No statements in IDX's ratio table yet." }
      )
    );
    return node;
  }

  function dividendsCard(dividends) {
    const node = card("Dividends", "Cash dividends by ex-date, from IDX's announcements since 2019.");
    node.append(
      dataTable(
        [
          { key: "ex_date", label: "Ex-date", render: (r) => fmt.date(r.ex_date) },
          {
            key: "cash_per_share",
            label: "Per share",
            align: "right",
            render: (r) => (r.currency === "IDR" ? `Rp ${fmt.num(r.cash_per_share, r.cash_per_share < 10 ? 2 : 0)}` : `US$ ${fmt.num(r.cash_per_share, 4)}`),
          },
          { key: "idr_per_share", label: "In rupiah", align: "right", render: (r) => (isNum(r.idr_per_share) ? fmt.num(r.idr_per_share, r.idr_per_share < 10 ? 2 : 0) : "–") },
          { key: "note", label: "Type", render: (r) => (r.note ? (String(r.note).startsWith("I") ? "Interim" : "Final") : "–") },
          { key: "payment_date", label: "Paid", align: "right", render: (r) => fmt.date(r.payment_date) },
        ],
        dividends || [],
        { sortKey: "ex_date", sortDir: "desc", empty: "No cash dividends announced since 2019." }
      )
    );
    return node;
  }

  function valuationHistoryCard(history, row) {
    const node = card("P/E and P/BV over time", "IDX's monthly figures at each month's price.");
    const months = (history || []).filter((item) => isNum(item.per) || isNum(item.pbv));
    if (months.length < 3) {
      node.append(el("p", "dMuted", "Not enough monthly history yet."));
      return node;
    }
    const dates = months.map((item) => `${item.report_month}-01`);
    const pair = el("div", "dGrid2 dTight");
    const pe = el("div");
    pe.append(
      el("h4", "", "P/E"),
      lineChart(dates, [{ values: months.map((item) => (isNum(item.per) && item.per > 0 && item.per < 200 ? item.per : null)), label: "P/E", cls: "isPrimary", format: fmt.x }], {
        height: 170,
        yFormat: (v) => `${v.toFixed(0)}x`,
        label: `${row.ticker} P/E history`,
      })
    );
    const pbv = el("div");
    pbv.append(
      el("h4", "", "Price to book"),
      lineChart(dates, [{ values: months.map((item) => (isNum(item.pbv) && item.pbv > 0 ? item.pbv : null)), label: "P/BV", cls: "isPrimary", format: fmt.x }], {
        height: 170,
        yFormat: (v) => `${v.toFixed(1)}x`,
        label: `${row.ticker} price-to-book history`,
      })
    );
    pair.append(pe, pbv);
    node.append(pair);
    return node;
  }

  // ---------------------------------------------------------------- Portfolio

  function renderPortfolio(host, data) {
    host.textContent = "";
    const page = el("div", "dPage");
    const head = el("div", "dScreenHead");
    const title = el("div");
    title.append(el("h2", "dTitle", "Portfolio"), el("p", "dLead", data && data.as_of ? `Valued at the ${fmt.date(data.as_of)} close.` : ""));
    head.append(title);
    page.append(head);
    if (!data || data.error) {
      page.append(empty("The holdings file has a problem.", (data && data.error) || "Could not read it."));
      host.append(page);
      return;
    }
    const p = data.portfolio;
    if (!p) {
      const node = card("No holdings yet", null);
      node.append(holdingsHelp(data.path));
      page.append(node);
    } else {
      const summary = card(null, null);
      const kpis = el("div", "dKpis");
      kpis.append(
        kpi("Total value", fmt.rp(p.total), `stocks ${fmt.rp(p.invested)} · cash ${fmt.rp(p.cash)}`),
        kpi("Today", signed(fmt.rp(p.day_change, true), p.day_change), signed(fmt.pct(p.day_change_pct), p.day_change_pct)),
        kpi("Gain since bought", signed(fmt.rp(p.gain, true), p.gain), signed(fmt.pct(p.gain_pct), p.gain_pct)),
        kpi("Largest position", p.largest ? p.largest.ticker : "–", p.largest ? `${fmt.pct(p.largest.weight_pct, 0, false)} of the total` : null),
        kpi("Dividends, past year", fmt.rp(p.dividend_income), isNum(p.dividend_yield_pct) ? `${fmt.pct(p.dividend_yield_pct, 1, false)} of today's value · ${fmt.pct(p.yield_on_cost_pct, 1, false)} on cost` : null),
        kpi("Serious warnings", String(p.flag_count), "across your positions", p.flag_count ? "down" : "")
      );
      summary.append(kpis);
      page.append(summary);

      const positions = card("Positions", "Click a stock to open its page. Gain is against your average price, before fees and dividends.");
      positions.append(positionsTable(p.positions, false));
      page.append(positions);

      const grid = el("div", "dGrid2");
      const mix = card("Sector mix", "Share of your total, cash included.");
      const bar = el("div", "dMixBar");
      const list = el("ul", "dPlainList");
      const shades = 6;
      p.sectors.forEach((item, i) => {
        const part = el("span", `dMixPart shade-${i % shades}`);
        part.style.width = `${item.weight_pct || 0}%`;
        part.title = `${item.sector}: ${fmt.pct(item.weight_pct, 1, false)}`;
        bar.append(part);
        const li = el("li");
        li.append(el("span", `dMixKey shade-${i % shades}`), el("span", "", item.sector), el("span", "num", fmt.pct(item.weight_pct, 1, false)));
        list.append(li);
      });
      if (p.cash) {
        const part = el("span", "dMixPart isCash");
        part.style.width = `${p.cash_pct || 0}%`;
        bar.append(part);
        const li = el("li");
        li.append(el("span", "dMixKey isCash"), el("span", "", "Cash"), el("span", "num", fmt.pct(p.cash_pct, 1, false)));
        list.append(li);
      }
      mix.append(bar, list);

      const risk = card("If the market falls", "A rough guide from how your stocks have moved with IHSG over the past year.");
      if (isNum(p.beta)) {
        const fall = 10 * p.beta * (1 - (p.cash_pct || 0) / 100);
        risk.append(
          el("p", "dBigLine", `If IHSG fell 10%, your portfolio would typically fall about ${fall.toFixed(1)}%, or ${fmt.rp((p.total * fall) / 100)}.`),
          el("p", "dMuted", `Your stocks' beta is ${p.beta.toFixed(2)} (weighted by value); cash does not move. Single stocks can fall much more than this on their own news.`)
        );
      } else {
        risk.append(el("p", "dMuted", "Needs a year of prices for your stocks."));
      }
      grid.append(mix, risk);
      page.append(grid);
      page.append(el("p", "dFoot", `Edit your holdings in ${p.path}. Reload after saving.`));
    }

    const watch = card("Watchlist", "Star a stock on its page to add it here.");
    watch.append(
      dataTable(
        [
          COLUMNS.stock,
          { key: "close", label: "Price", align: "right", render: (r) => (r.missing ? "not listed" : fmt.price(r.close)) },
          COLUMNS.change_1d_pct,
          COLUMNS.ret_1m_pct,
          COLUMNS.ret_1y_pct,
          COLUMNS.pe,
          COLUMNS.below_high_pct,
          { key: "flags", label: "Warnings", sortable: false, render: (r) => flagPills(r.flags, 2) },
        ],
        data.watchlist || [],
        { sortKey: "change_1d_pct", sortDir: "desc", onRow: (r) => openStock(r.ticker), empty: "No stocks on the watchlist yet." }
      )
    );
    page.append(watch);
    host.append(page);
  }

  window.StockTriageDecide = { renderOverview, renderStocks, renderStock, renderPortfolio, fmt };
})();
