/* Reporting, analytics and the two dashboards.
 *
 * A separate module from app.js on purpose: the booking flow and the back-office
 * analytics share a design language and nothing else, and keeping them apart means
 * a guest loading the booking page does not download the reporting engine.
 *
 * Charts are drawn as inline SVG by hand. There is no chart library because the
 * project may not add dependencies and there is no build step — but the constraint
 * turned out to suit the brief, which asks for flat, clean, readable charts with
 * minimal gridlines rather than anything a library would give for free. Every
 * chart also renders an accessible summary, because a <svg> full of <path> tells a
 * screen reader nothing.
 *
 * Nothing here computes money. Amounts arrive as integer minor units with their
 * currency and are formatted, never divided — JPY has no decimal places, and two
 * places of arithmetic in the client is how a dashboard starts disagreeing with
 * the receipt.
 */
(function () {
  'use strict';

  const R = {
    catalog: null,
    active: null,          // report key
    filters: { date_preset: 'this_month', compare_with: 'previous_period' },
    breadcrumb: [],
    views: [],
    loaded: false,
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  // A rounded tile carrying a shared topic icon (designIcon.md). `key` is a report
  // catalog / KPI key; the shared module resolves it to a symbol so reports and
  // settings never drift to different icon families. Falls back to a bare tile if
  // the icon module has not loaded, so a report still renders.
  function iconTile(key, cls, size) {
    const span = el('span', 'rp-ico uic-tile' + (cls ? ' ' + cls : ''));
    span.setAttribute('aria-hidden', 'true');
    if (window.utpIcons) span.innerHTML = window.utpIcons.report(key, { size: size || 20 });
    return span;
  }

  /* ---------------------------------------------------------------- format */

  function locale() {
    return (window.utpLocale && window.utpLocale()) || 'en-GB';
  }

  // Integer minor units in, formatted string out. The server tells us the
  // currency and how many decimal places it has; we never assume two.
  function money(minor, currency) {
    const code = currency || 'THB';
    const value = Number(minor || 0) / Math.pow(10, decimalsFor(code));
    try {
      return new Intl.NumberFormat(locale(), {
        style: 'currency', currency: code, maximumFractionDigits: decimalsFor(code),
      }).format(value);
    } catch (_) {
      return `${code} ${value.toFixed(decimalsFor(code))}`;
    }
  }

  // Zero-decimal currencies. Assuming two places everywhere would render
  // JPY 5,000 as JPY 50.00.
  const ZERO_DECIMAL = { JPY: true, KRW: true, VND: true, CLP: true, ISK: true };

  function decimalsFor(code) {
    return ZERO_DECIMAL[code] ? 0 : 2;
  }

  const number = (value) => new Intl.NumberFormat(locale()).format(Number(value || 0));
  const percent = (bp) => `${(Number(bp || 0) / 100).toFixed(1)}%`;

  // Split a stored timestamp into its date and time parts without shifting the zone.
  // The backend already emits business-local strings ("2026-09-01T10:00:00",
  // "2026-09-01 10:00:00", or "2026-09-01"), so we parse the literal text rather
  // than constructing a Date (which would re-interpret it in the browser's zone and
  // move a Bangkok time by seven hours). Returns { date, time } with either blank.
  const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function splitDateTime(value) {
    if (value === null || value === undefined || value === '') return { date: '', time: '' };
    const text = String(value).trim();
    const m = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/);
    if (!m) return { date: text, time: '' };
    const [, y, mo, d, hh, mm, ss] = m;
    const monthIndex = Math.min(11, Math.max(0, parseInt(mo, 10) - 1));
    // Date format required by the venue back office: yyyy-Mmm-dd (2026-Sep-01).
    const date = `${y}-${_MONTHS[monthIndex]}-${d}`;
    const time = hh !== undefined ? `${hh}:${mm}:${ss !== undefined ? ss : '00'}` : '';
    return { date: date, time: time };
  }

  function fmtDate(value) { return splitDateTime(value).date; }
  function fmtTime(value) {
    const t = splitDateTime(value).time;
    if (t) return t;
    // A time-only value like "10:00" or "10:00:00".
    const m = String(value || '').trim().match(/^(\d{2}):(\d{2})(?::(\d{2}))?$/);
    return m ? `${m[1]}:${m[2]}:${m[3] || '00'}` : (value ? String(value) : '');
  }

  function cell(value, kind, currency) {
    if (value === null || value === undefined || value === '') return '';
    if (kind === 'money') return money(value, currency);
    if (kind === 'percent') return percent(value);
    if (kind === 'number') return number(value);
    if (kind === 'date') return fmtDate(value);
    if (kind === 'time') return fmtTime(value);
    if (kind === 'datetime') return `${fmtDate(value)} ${fmtTime(value)}`.trim();
    return String(value);
  }

  /* ------------------------------------------------------------------ api */

  // Routed through app.js's helper rather than a second fetch wrapper: that is
  // where the staff bearer token and the CSRF header are attached, and where a
  // server error is already unwrapped into a friendly message with a reference.
  async function get(path) {
    if (!window.utpApi) throw new Error('The application is still starting. Please try again.');
    return window.utpApi(path);
  }

  function queryString(extra) {
    const params = new URLSearchParams();
    const merged = Object.assign({}, R.filters, extra || {});
    Object.keys(merged).forEach((key) => {
      const value = merged[key];
      if (value === null || value === undefined || value === '') return;
      if (Array.isArray(value)) value.forEach((v) => params.append(key, v));
      else params.append(key, value);
    });
    const text = params.toString();
    return text ? `?${text}` : '';
  }

  /* ------------------------------------------------------------- svg chart */

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach((key) => node.setAttribute(key, attrs[key]));
    return node;
  };

  // A flat multi-series line chart. Minimal gridlines, no gradient fills, no 3D:
  // the brief is explicit that charts stay clean while icons carry the depth.
  function lineChart(series, options) {
    const opts = Object.assign({ width: 720, height: 240, currency: 'THB' }, options || {});
    const pad = { top: 14, right: 12, bottom: 26, left: 62 };
    const plotW = opts.width - pad.left - pad.right;
    const plotH = opts.height - pad.top - pad.bottom;
    const labels = opts.labels || [];
    const all = series.reduce((acc, s) => acc.concat(s.values), []);
    const max = Math.max.apply(null, all.concat([1]));

    const svg = svgEl('svg', {
      viewBox: `0 0 ${opts.width} ${opts.height}`,
      class: 'chart', role: 'img',
      'aria-label': opts.summary || 'Chart',
      preserveAspectRatio: 'none',
    });

    // Four horizontal guides only — enough to read a value, not a grid to wade through.
    for (let i = 0; i <= 4; i += 1) {
      const y = pad.top + (plotH * i) / 4;
      svg.appendChild(svgEl('line', {
        x1: pad.left, y1: y, x2: pad.left + plotW, y2: y, class: 'ch-grid',
      }));
      const value = max - (max * i) / 4;
      const text = svgEl('text', { x: pad.left - 8, y: y + 4, class: 'ch-axis', 'text-anchor': 'end' });
      text.textContent = opts.kind === 'money' ? compact(value) : number(Math.round(value));
      svg.appendChild(text);
    }

    series.forEach((s, index) => {
      if (!s.values.length) return;
      const step = s.values.length > 1 ? plotW / (s.values.length - 1) : 0;
      const points = s.values.map((value, i) => {
        const x = pad.left + step * i;
        const y = pad.top + plotH - (Number(value || 0) / max) * plotH;
        return [x, y];
      });
      const path = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('');
      svg.appendChild(svgEl('path', { d: path, class: `ch-line ch-s${index}`, fill: 'none' }));
      // Dots only when the series is short enough for them to mean something.
      if (points.length <= 40) {
        points.forEach((p, i) => {
          const dot = svgEl('circle', { cx: p[0], cy: p[1], r: 2.6, class: `ch-dot ch-s${index}` });
          const title = svgEl('title');
          title.textContent = `${labels[i] || ''} · ${s.label}: ${
            opts.kind === 'money' ? money(s.values[i], opts.currency) : number(s.values[i])}`;
          dot.appendChild(title);
          svg.appendChild(dot);
        });
      }
    });

    // Sparse x labels: a month of days will not fit, so show about six.
    const every = Math.max(1, Math.ceil(labels.length / 6));
    labels.forEach((label, i) => {
      if (i % every !== 0 && i !== labels.length - 1) return;
      const step = labels.length > 1 ? plotW / (labels.length - 1) : 0;
      const text = svgEl('text', {
        x: pad.left + step * i, y: opts.height - 8, class: 'ch-axis', 'text-anchor': 'middle',
      });
      text.textContent = shortLabel(label);
      svg.appendChild(text);
    });
    return svg;
  }

  function compact(value) {
    const n = Number(value || 0);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
    return String(Math.round(n));
  }

  function shortLabel(label) {
    const text = String(label || '');
    if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text.slice(5);
    if (/^\d{4}-\d{2}$/.test(text)) return text.slice(2);
    return text.length > 8 ? `${text.slice(0, 7)}…` : text;
  }

  // Horizontal bars rather than a donut for channel mix: a bar is easier to
  // compare and to label, and the brief warns against overly complex charts.
  function barList(rows, options) {
    const opts = Object.assign({ currency: 'THB', valueKey: 'net_minor', kind: 'money' }, options || {});
    const wrap = el('div', 'bars');
    const max = rows.reduce((acc, row) => Math.max(acc, Number(row[opts.valueKey] || 0)), 1);
    rows.forEach((row, index) => {
      const line = el('div', 'bar-row');
      const head = el('div', 'bar-head');
      head.appendChild(el('span', 'bar-label', row.label || row.product || row.promotion || row.partner || ''));
      head.appendChild(el('span', 'bar-value',
        opts.kind === 'money' ? money(row[opts.valueKey], opts.currency) : number(row[opts.valueKey])));
      line.appendChild(head);
      const track = el('div', 'bar-track');
      const fill = el('div', `bar-fill ch-b${index % 5}`);
      fill.style.width = `${Math.max(2, (Number(row[opts.valueKey] || 0) / max) * 100)}%`;
      track.appendChild(fill);
      line.appendChild(track);
      if (row.share_bp !== undefined) {
        line.appendChild(el('div', 'bar-note', `${percent(row.share_bp)} of total`));
      }
      wrap.appendChild(line);
    });
    return wrap;
  }

  // Day-of-week × hour heatmap. Intensity is reinforced by a printed value in the
  // busiest cells and a full text summary, so colour is never the only cue.
  function heatmap(data) {
    const wrap = el('div', 'heat');
    const hours = [];
    for (let h = 0; h < 24; h += 1) {
      const anyValue = data.grid.some((row) => row[h] > 0);
      if (anyValue) hours.push(h);
    }
    if (!hours.length) {
      wrap.appendChild(el('p', 'muted', 'No activity in this period.'));
      return wrap;
    }
    const table = el('table', 'heat-table');
    const thead = el('thead');
    const headRow = el('tr');
    headRow.appendChild(el('th', 'heat-corner', ''));
    hours.forEach((h) => headRow.appendChild(el('th', null, `${String(h).padStart(2, '0')}`)));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el('tbody');
    data.days.forEach((day, index) => {
      const row = el('tr');
      row.appendChild(el('th', null, day));
      hours.forEach((h) => {
        const value = data.grid[index][h] || 0;
        const intensity = data.peak ? value / data.peak : 0;
        const td = el('td', 'heat-cell');
        td.style.setProperty('--i', intensity.toFixed(3));
        td.setAttribute('title', `${day} ${String(h).padStart(2, '0')}:00 — ${number(value)}`);
        if (intensity > 0.55) td.textContent = number(value);
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /* ------------------------------------------------------------ components */

  function kpiCard(card) {
    const node = el('button', 'kpi');
    node.type = 'button';
    if (card.drill_to) {
      node.dataset.drill = card.drill_to;
      node.title = 'Open the detail report';
    } else {
      node.disabled = true;
    }
    node.appendChild(iconTile(card.key, 'kpi-icon', 22));
    node.appendChild(el('span', 'kpi-label', card.label));
    node.appendChild(el('strong', 'kpi-value',
      cell(card.value, card.kind, card.currency) || '—'));
    if (card.change_bp !== null && card.change_bp !== undefined) {
      // A rise in refunds is not good news, so the tone follows the metric, not
      // the arrow direction.
      const good = card.lower_is_better ? card.change_bp < 0 : card.change_bp > 0;
      const trend = el('span', `kpi-trend ${good ? 'is-good' : 'is-bad'}`);
      trend.textContent = `${card.change_bp > 0 ? '▲' : card.change_bp < 0 ? '▼' : '■'} ${
        percent(Math.abs(card.change_bp))}`;
      node.appendChild(trend);
      node.appendChild(el('span', 'kpi-compare', 'vs previous period'));
    } else {
      node.appendChild(el('span', 'kpi-compare', 'no comparison'));
    }
    return node;
  }

  // A section header with an optional leading topic icon (§16, §33). `iconKey` lets
  // a panel carry the same glyph as the report it belongs to; omit it for neutral
  // sub-panels.
  function panel(title, body, extra, iconKey) {
    const section = el('section', 'rp-panel');
    const head = el('div', 'rp-panel-head');
    const heading = el('h3');
    if (iconKey) heading.appendChild(iconTile(iconKey, 'rp-panel-ico', 18));
    heading.appendChild(el('span', null, title));
    head.appendChild(heading);
    if (extra) head.appendChild(extra);
    section.appendChild(head);
    section.appendChild(body);
    return section;
  }

  function emptyState(message, hint) {
    const wrap = el('div', 'rp-empty');
    wrap.appendChild(iconTile('report', 'rp-empty-icon', 30));
    wrap.appendChild(el('p', 'rp-empty-title', message));
    if (hint) wrap.appendChild(el('p', 'muted', hint));
    const actions = el('div', 'rp-empty-actions');
    const clear = el('button', 'ghost', 'Clear filters');
    clear.type = 'button';
    clear.addEventListener('click', () => {
      R.filters = { date_preset: 'this_month', compare_with: 'previous_period' };
      render();
    });
    actions.appendChild(clear);
    wrap.appendChild(actions);
    return wrap;
  }

  // Skeletons rather than a spinner over the whole page: the layout stays put, so
  // the eye is not thrown when the data lands.
  function skeleton(rows) {
    const wrap = el('div', 'rp-skeleton');
    for (let i = 0; i < (rows || 5); i += 1) wrap.appendChild(el('div', 'sk-line'));
    return wrap;
  }

  function errorState(error) {
    const wrap = el('div', 'rp-error');
    wrap.appendChild(el('p', 'rp-empty-title', 'We couldn\u2019t load this report.'));
    wrap.appendChild(el('p', 'muted', error.message || 'Please try again.'));
    if (error.reference) wrap.appendChild(el('p', 'rp-ref', `Reference ${error.reference}`));
    const retry = el('button', 'primary', 'Try again');
    retry.type = 'button';
    retry.addEventListener('click', render);
    wrap.appendChild(retry);
    return wrap;
  }

  // A "datetime" column is shown as TWO columns — Date (yyyy-Mmm-dd) and Time
  // (hh:mm:ss) — because reading a timestamp is easier when the calendar date and
  // the clock time are aligned in their own columns. Any other column passes
  // through unchanged. The expanded columns carry a _part marker the renderer uses
  // to format each half from the same underlying value.
  function expandColumns(columns) {
    const out = [];
    columns.forEach((c) => {
      if (c.kind === 'datetime') {
        out.push({ key: c.key, label: c.label + ' (date)', kind: 'date', align: 'left', _part: 'date' });
        out.push({ key: c.key, label: c.label + ' (time)', kind: 'time', align: 'left', _part: 'time' });
      } else {
        out.push(c);
      }
    });
    return out;
  }

  function dataTable(columns, rows, meta, totals) {
    if (!rows.length) return emptyState('No rows match the current filters.',
      'Try a wider date range or a different venue.');
    const cols = expandColumns(columns);
    const wrap = el('div', 'rp-table-wrap');
    const table = el('table', 'rp-table');
    const thead = el('thead');
    const headRow = el('tr');
    cols.forEach((column) => {
      const th = el('th', column.align === 'right' ? 'r' : null, column.label);
      th.scope = 'col';
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el('tbody');
    rows.forEach((row) => {
      const tr = el('tr');
      cols.forEach((column) => {
        const value = row[column.key];
        const td = el('td', column.align === 'right' ? 'r' : null);
        if (column.kind === 'status') {
          td.appendChild(statusBadge(value));
        } else {
          td.textContent = cell(value, column.kind, meta.currency);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    if (totals && Object.keys(totals).length) {
      const tfoot = el('tfoot');
      const tr = el('tr');
      cols.forEach((column, index) => {
        const td = el('td', column.align === 'right' ? 'r' : null);
        if (index === 0) td.textContent = 'Total';
        else if (column._part) td.textContent = '';   // no total for a split datetime
        else if (totals[column.key] !== undefined) {
          td.textContent = cell(totals[column.key], column.kind, meta.currency);
        }
        tr.appendChild(td);
      });
      tfoot.appendChild(tr);
      table.appendChild(tfoot);
    }
    wrap.appendChild(table);
    return wrap;
  }

  // Status is never colour alone: the badge always carries its text (R68.4).
  function statusBadge(value) {
    const text = String(value === null || value === undefined ? '' : value);
    const badge = el('span', `st ${statusTone(text)}`, text);
    return badge;
  }

  function statusTone(text) {
    const t = text.toLowerCase();
    if (/(fail|offline|expired|cancel|refund|void|error|critical|no-short|short by|invalid|unmatched)/.test(t)) return 'st-bad';
    if (/(pending|late|near full|paper low|warning|starting soon|above threshold|delayed|partly)/.test(t)) return 'st-warn';
    if (/(admit|checked in|online|matched|balanced|confirmed|completed|full|paid|issued|valid|happening)/.test(t)) return 'st-good';
    return 'st-neutral';
  }

  /* -------------------------------------------------------------- filters */

  const DATE_LABELS = {
    today: 'Today', yesterday: 'Yesterday', this_week: 'This week', last_week: 'Last week',
    this_month: 'This month', last_month: 'Last month', this_year: 'This year', custom: 'Custom',
  };

  function filterBar(report) {
    const bar = el('div', 'rp-filters');
    const relevant = (report && report.filters) || ['date_range', 'venue'];

    if (relevant.indexOf('date_range') !== -1) {
      bar.appendChild(select('date_preset', 'Period',
        Object.keys(DATE_LABELS).map((key) => ({ value: key, label: DATE_LABELS[key] }))));
      if (R.filters.date_preset === 'custom') {
        bar.appendChild(dateInput('date_from', 'From'));
        bar.appendChild(dateInput('date_to', 'To'));
      }
    }
    if (relevant.indexOf('venue') !== -1 && R.catalog && R.catalog.venues.length > 1) {
      bar.appendChild(select('venue', 'Venue',
        [{ value: '', label: 'All venues' }].concat(
          R.catalog.venues.map((v) => ({ value: v.id, label: v.name })))));
    }
    if (relevant.indexOf('date_basis') !== -1) {
      bar.appendChild(select('date_basis', 'Dated by', [
        { value: 'visit_date', label: 'Visit date' },
        { value: 'order_date', label: 'Order date' },
      ]));
    }
    if (relevant.indexOf('channel') !== -1) {
      bar.appendChild(select('channel', 'Channel', [{ value: '', label: 'All channels' }].concat(
        ['ONLINE', 'KIOSK', 'COUNTER', 'PARTNER', 'STAFF', 'API']
          .map((c) => ({ value: c, label: titleCase(c) })))));
    }
    if (relevant.indexOf('group_by') !== -1) {
      bar.appendChild(select('group_by', 'Grouped by',
        ['hourly', 'daily', 'weekly', 'monthly'].map((g) => ({ value: g, label: titleCase(g) }))));
    }
    if (relevant.indexOf('compare_with') !== -1) {
      bar.appendChild(select('compare_with', 'Compare with', [
        { value: 'previous_period', label: 'Previous period' },
        { value: 'same_period_last_year', label: 'Same period last year' },
        { value: 'none', label: 'No comparison' },
      ]));
    }
    return bar;
  }

  function titleCase(text) {
    return String(text || '').replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function select(key, label, options) {
    const field = el('label', 'rp-field');
    field.appendChild(el('span', 'rp-field-label', label));
    const input = el('select');
    options.forEach((option) => {
      const opt = el('option', null, option.label);
      opt.value = option.value;
      if (String(R.filters[key] || '') === String(option.value)) opt.selected = true;
      input.appendChild(opt);
    });
    input.addEventListener('change', () => {
      if (input.value) R.filters[key] = input.value;
      else delete R.filters[key];
      render();
    });
    field.appendChild(input);
    return field;
  }

  function dateInput(key, label) {
    const field = el('label', 'rp-field');
    field.appendChild(el('span', 'rp-field-label', label));
    const input = el('input');
    input.type = 'date';
    input.value = R.filters[key] || '';
    input.addEventListener('change', () => {
      if (input.value) R.filters[key] = input.value;
      else delete R.filters[key];
      render();
    });
    field.appendChild(input);
    return field;
  }

  // Active filters as removable chips, so what is being excluded is always visible.
  function chips() {
    const wrap = el('div', 'rp-chips');
    const skip = { date_preset: true, compare_with: true };
    Object.keys(R.filters).forEach((key) => {
      if (skip[key] || !R.filters[key]) return;
      const label = `${titleCase(key)}: ${chipValue(key, R.filters[key])}`;
      const chip = el('button', 'rp-chip');
      chip.type = 'button';
      chip.textContent = label;
      chip.appendChild(el('span', 'rp-chip-x', '\u00d7'));
      chip.setAttribute('aria-label', `Remove filter ${label}`);
      chip.addEventListener('click', () => { delete R.filters[key]; render(); });
      wrap.appendChild(chip);
    });
    return wrap;
  }

  function chipValue(key, value) {
    if (key === 'venue' && R.catalog) {
      const found = R.catalog.venues.filter((v) => v.id === value)[0];
      return found ? found.name : value;
    }
    return titleCase(value);
  }

  /* ------------------------------------------------------------ dashboards */

  function renderExecutive(data, host) {
    const currency = data.meta.currency;
    const grid = el('div', 'kpi-grid');
    data.kpis.forEach((card) => grid.appendChild(kpiCard(card)));
    host.appendChild(grid);

    const series = data.revenue_series || [];
    const labels = series.map((row) => row.bucket);
    const row1 = el('div', 'rp-row rp-row-wide');
    row1.appendChild(panel('Revenue trend', series.length
      ? lineChart([
        { label: 'Net sales', values: series.map((r) => r.net_minor) },
        { label: 'Discount', values: series.map((r) => r.discount_minor) },
        { label: 'Refund', values: series.map((r) => r.refund_minor) },
      ], {
        labels, kind: 'money', currency,
        summary: `Net sales by ${data.meta.group_by} from ${data.meta.date_from} to ${data.meta.date_to}`,
      })
      : emptyState('No sales in this period.'), legend([
        'Net sales', 'Discount', 'Refund',
      ])));
    row1.appendChild(panel('Sales by channel', (data.channels || []).length
      ? barList(data.channels, { currency })
      : emptyState('No channel activity.')));
    host.appendChild(row1);

    const row2 = el('div', 'rp-row');
    row2.appendChild(panel('Visitor mix', (data.visitor_segments || []).length
      ? barList(data.visitor_segments, { valueKey: 'tickets', kind: 'number' })
      : emptyState('No visitors yet.')));
    row2.appendChild(panel('Thai vs international', (data.pricing_groups || []).length
      ? pricingGroupTable(data.pricing_groups, currency)
      : emptyState('No pricing-group split available.')));
    host.appendChild(row2);

    const row3 = el('div', 'rp-row');
    row3.appendChild(panel('Top products', (data.top_products || []).length
      ? barList(data.top_products, { currency })
      : emptyState('Nothing sold in this period.')));
    row3.appendChild(panel('Promotions', (data.promotions || []).length
      ? barList(data.promotions, { valueKey: 'discount_minor', currency })
      : emptyState('No promotions redeemed.')));
    host.appendChild(row3);

    const row4 = el('div', 'rp-row');
    row4.appendChild(panel('Capacity', capacityPanel(data.capacity)));
    row4.appendChild(panel('Advance booking', (data.advance_booking || []).length
      ? barList(data.advance_booking, { valueKey: 'bookings', kind: 'number' })
      : emptyState('No bookings to analyse.')));
    host.appendChild(row4);

    host.appendChild(panel('Peak time', heatmap(data.peak_time || { days: [], grid: [], peak: 0 })));
    host.appendChild(panel('Exceptions', exceptionList(data.exceptions || [])));
  }

  function legend(labels) {
    const wrap = el('div', 'ch-legend');
    labels.forEach((label, index) => {
      const item = el('span', 'ch-key');
      item.appendChild(el('i', `ch-swatch ch-s${index}`));
      item.appendChild(el('span', null, label));
      wrap.appendChild(item);
    });
    return wrap;
  }

  function pricingGroupTable(groups, currency) {
    const table = el('table', 'rp-table rp-table-compact');
    const thead = el('thead');
    const headRow = el('tr');
    ['Group', 'Tickets', 'Net sales', 'Per ticket'].forEach((label, i) => {
      const th = el('th', i ? 'r' : null, label);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el('tbody');
    groups.forEach((group) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, group.label));
      tr.appendChild(el('td', 'r', number(group.tickets)));
      tr.appendChild(el('td', 'r', money(group.net_minor, currency)));
      tr.appendChild(el('td', 'r', money(group.per_visitor_minor, currency)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function capacityPanel(capacity) {
    const wrap = el('div', 'cap-panel');
    if (!capacity || !capacity.capacity) {
      wrap.appendChild(el('p', 'muted',
        'Capacity is not limited for this venue\u2019s general admission, so there is nothing to utilise.'));
      return wrap;
    }
    const pct = Math.min(100, Number(capacity.utilization_bp || 0) / 100);
    wrap.appendChild(el('div', 'cap-figure', `${pct.toFixed(1)}%`));
    const track = el('div', 'bar-track cap-track');
    const fill = el('div', 'bar-fill ' + (pct >= 100 ? 'ch-b2' : pct >= 90 ? 'ch-b1' : 'ch-b0'));
    fill.style.width = `${Math.max(2, pct)}%`;
    track.appendChild(fill);
    wrap.appendChild(track);
    const stats = el('dl', 'cap-stats');
    [['Capacity', number(capacity.capacity)], ['Reserved', number(capacity.reserved)],
      ['Checked in', number(capacity.checked_in)],
      ['Remaining', number(Math.max(capacity.capacity - capacity.reserved, 0))]].forEach((pair) => {
      stats.appendChild(el('dt', null, pair[0]));
      stats.appendChild(el('dd', null, pair[1]));
    });
    wrap.appendChild(stats);
    return wrap;
  }

  function exceptionList(findings) {
    if (!findings.length) {
      const ok = el('div', 'rp-allclear');
      ok.appendChild(iconTile('check', 'rp-allclear-icon', 30));
      ok.appendChild(el('p', null, 'Nothing looks unusual in this period.'));
      return ok;
    }
    const list = el('ul', 'alerts');
    findings.forEach((finding) => {
      const item = el('li', `alert alert-${finding.severity.toLowerCase()}`);
      const head = el('div', 'alert-head');
      head.appendChild(el('span', 'alert-sev', titleCase(finding.severity)));
      head.appendChild(el('strong', 'alert-title', finding.title));
      if (finding.metric) head.appendChild(el('span', 'alert-metric', finding.metric));
      item.appendChild(head);
      item.appendChild(el('p', 'alert-detail', finding.detail));
      if (finding.action) item.appendChild(el('p', 'alert-action', finding.action));
      if (finding.drill_to) {
        const link = el('button', 'link-btn', 'Open the detail report');
        link.type = 'button';
        link.dataset.drill = finding.drill_to;
        item.appendChild(link);
      }
      list.appendChild(item);
    });
    return list;
  }

  function renderOperations(data, host) {
    const currency = data.meta.currency;
    const grid = el('div', 'kpi-grid kpi-grid-tight');
    data.kpis.forEach((tile) => {
      const card = el('div', `tile tile-${tile.tone || 'normal'}`);
      card.appendChild(iconTile(tile.key, 'kpi-icon', 20));
      card.appendChild(el('span', 'kpi-label', tile.label));
      card.appendChild(el('strong', 'kpi-value', cell(tile.value, tile.kind, currency) || '0'));
      grid.appendChild(card);
    });
    host.appendChild(grid);

    host.appendChild(panel('Exceptions', exceptionList(data.exceptions || [])));

    const arrivalCols = [
      { key: 'visit_time', label: 'Visit time', kind: 'time' },
      { key: 'booking_number', label: 'Booking', kind: 'text' },
      { key: 'customer', label: 'Customer', kind: 'pii' },
      { key: 'party_size', label: 'Party', kind: 'number', align: 'right' },
      { key: 'ticket_type', label: 'Ticket type', kind: 'text' },
      { key: 'state', label: 'Status', kind: 'status' },
    ];
    const counts = data.arrival_counts || {};
    const summary = el('div', 'rp-inline-counts');
    [['Arriving soon', counts.ARRIVING], ['Checked in', counts.CHECKED_IN],
      ['Late', counts.LATE], ['No-show', counts.NO_SHOW]].forEach((pair) => {
      const chip = el('span', 'count-chip');
      chip.appendChild(el('strong', null, number(pair[1] || 0)));
      chip.appendChild(el('span', null, pair[0]));
      summary.appendChild(chip);
    });
    host.appendChild(panel('Expected arrivals',
      dataTable(arrivalCols, data.arrivals || [], data.meta, null), summary));

    const gate = data.gate || {};
    const gateWrap = el('div');
    const gateStats = el('div', 'rp-inline-counts');
    [['Scans', gate.total_scans], ['Admitted', gate.admitted], ['Refused', gate.refused],
      ['Already used', gate.already_used], ['Overrides', gate.overrides]].forEach((pair) => {
      const chip = el('span', 'count-chip');
      chip.appendChild(el('strong', null, number(pair[1] || 0)));
      chip.appendChild(el('span', null, pair[0]));
      gateStats.appendChild(chip);
    });
    gateWrap.appendChild(gateStats);
    gateWrap.appendChild(dataTable([
      { key: 'at_local', label: 'Time', kind: 'time' },
      { key: 'booking_number', label: 'Booking', kind: 'text' },
      { key: 'ticket_number', label: 'Ticket', kind: 'text' },
      { key: 'access_point', label: 'Access point', kind: 'text' },
      { key: 'decision', label: 'Result', kind: 'status' },
    ], data.gate_activity || [], data.meta, null));
    host.appendChild(panel('Gate activity', gateWrap));

    const row = el('div', 'rp-row');
    row.appendChild(panel('Capacity by session', dataTable([
      { key: 'label', label: 'Session', kind: 'text' },
      { key: 'start_time', label: 'Time', kind: 'time' },
      { key: 'reserved', label: 'Reserved', kind: 'number', align: 'right' },
      { key: 'capacity', label: 'Capacity', kind: 'number', align: 'right' },
      { key: 'utilization_bp', label: 'Used', kind: 'percent', align: 'right' },
      { key: 'state', label: 'Status', kind: 'status' },
    ], data.capacity_rows || [], data.meta, null)));
    row.appendChild(panel('Devices', dataTable([
      { key: 'name', label: 'Device', kind: 'text' },
      { key: 'kind', label: 'Kind', kind: 'text' },
      { key: 'state', label: 'Status', kind: 'status' },
      { key: 'last_seen_at', label: 'Last heartbeat', kind: 'date' },
    ], data.devices || [], data.meta, null)));
    host.appendChild(row);

    if ((data.shows || []).length) {
      host.appendChild(panel('Shows today', dataTable([
        { key: 'show', label: 'Show', kind: 'text' },
        { key: 'location', label: 'Location', kind: 'text' },
        { key: 'start_time', label: 'Time', kind: 'time' },
        { key: 'reserved', label: 'Reserved', kind: 'number', align: 'right' },
        { key: 'state', label: 'Status', kind: 'status' },
      ], data.shows, data.meta, null)));
    }
    host.appendChild(panel('Payments', (data.payments || []).length
      ? barList(data.payments, { valueKey: 'amount_minor', currency })
      : emptyState('No payments today.')));
  }

  /* ---------------------------------------------------------------- render */

  function sidebar() {
    const nav = el('nav', 'rp-side');
    nav.setAttribute('aria-label', 'Reports');
    // Section headers (Analytics / Operations / Finance) carry a family icon so the
    // three groups are recognizable at a glance (§16, §35).
    const sectionIcon = { analytics: 'dashboard', operations: 'activity', finance: 'finance' };
    (R.catalog.sections || []).forEach((section) => {
      const head = el('p', 'rp-side-group');
      const skey = String(section.key || section.label || '').toLowerCase();
      head.appendChild(iconTile(sectionIcon[skey] || 'report', 'rp-side-group-ico', 16));
      head.appendChild(el('span', null, section.label));
      nav.appendChild(head);
      const list = el('ul', 'rp-side-list');
      section.reports.forEach((report) => {
        const item = el('li');
        const button = el('button', 'rp-side-link' + (R.active === report.key ? ' is-active' : ''));
        button.type = 'button';
        button.dataset.report = report.key;
        const ico = el('span', 'rp-side-icon');
        ico.setAttribute('aria-hidden', 'true');
        if (window.utpIcons) ico.innerHTML = window.utpIcons.report(report.key, { size: 18 });
        button.appendChild(ico);
        button.appendChild(el('span', 'rp-side-text', report.title));
        if (R.active === report.key) button.setAttribute('aria-current', 'page');
        item.appendChild(button);
        list.appendChild(item);
      });
      nav.appendChild(list);
    });
    return nav;
  }

  function reportFor(key) {
    let found = null;
    (R.catalog.sections || []).forEach((section) => {
      section.reports.forEach((report) => { if (report.key === key) found = report; });
    });
    return found;
  }

  async function render() {
    const host = $('reportBody');
    if (!host) return;
    const report = reportFor(R.active);
    $('reportSide').replaceChildren(sidebar());
    $('reportTitle').textContent = report ? report.title : 'Reports';
    $('reportSummary').textContent = report ? report.summary : '';
    $('reportFilters').replaceChildren(filterBar(report), chips());
    renderBreadcrumb();
    renderExportMenu(report);
    host.replaceChildren(skeleton(6));

    try {
      let data;
      if (R.active === 'executive_overview') {
        data = await get(`/api/staff/dashboard/executive${queryString()}`);
      } else if (R.active === 'today_overview') {
        data = await get(`/api/staff/dashboard/operations${queryString()}`);
      } else {
        data = await get(`/api/staff/reports/${encodeURIComponent(R.active)}${queryString()}`);
      }
      const body = el('div');
      if (R.active === 'executive_overview') renderExecutive(data, body);
      else if (R.active === 'today_overview') renderOperations(data, body);
      else if (R.active === 'peak_time') body.appendChild(panel('Peak time', heatmap(data.peak_time)));
      else body.appendChild(reportBody(data));
      host.replaceChildren(body);
      renderMeta(data.meta);
    } catch (error) {
      host.replaceChildren(errorState(error));
      renderMeta(null);
    }
  }

  function reportBody(data) {
    const wrap = el('div');
    const chartable = data.columns.filter((c) => c.kind === 'money' && c.align === 'right');
    if (data.rows.length > 1 && chartable.length && data.rows[0].bucket !== undefined) {
      wrap.appendChild(panel('Trend', lineChart([
        { label: chartable[chartable.length - 1].label,
          values: data.rows.map((r) => r[chartable[chartable.length - 1].key]) },
      ], {
        labels: data.rows.map((r) => r.bucket), kind: 'money', currency: data.meta.currency,
        summary: `${data.report.title} over time`,
      })));
    }
    wrap.appendChild(dataTable(data.columns, data.rows, data.meta, data.totals));
    if (data.meta.reconciliation) {
      const note = el('p', 'rp-reconcile');
      note.textContent = `Line totals differ from the headline by ${
        money(data.meta.reconciliation.difference_minor, data.meta.currency)}. ${
        data.meta.reconciliation.explanation}`;
      wrap.appendChild(note);
    }
    return wrap;
  }

  function renderMeta(meta) {
    const node = $('reportMeta');
    if (!node) return;
    if (!meta) { node.textContent = ''; return; }
    const parts = [`${meta.date_from} to ${meta.date_to}`, meta.timezone, meta.currency];
    if (meta.masked) parts.push('personal data masked');
    node.textContent = `${parts.filter(Boolean).join(' \u00b7 ')} \u00b7 updated ${
      String(meta.generated_local || '').slice(11, 16)}`;
  }

  function renderBreadcrumb() {
    const node = $('reportCrumb');
    if (!node) return;
    node.replaceChildren();
    if (!R.breadcrumb.length) { node.hidden = true; return; }
    node.hidden = false;
    R.breadcrumb.concat([{ key: R.active }]).forEach((crumb, index, all) => {
      const report = reportFor(crumb.key);
      const label = report ? report.title : crumb.key;
      if (index === all.length - 1) {
        node.appendChild(el('span', 'crumb is-current', label));
        return;
      }
      const button = el('button', 'crumb');
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', () => {
        R.breadcrumb = R.breadcrumb.slice(0, index);
        R.active = crumb.key;
        if (crumb.filters) R.filters = Object.assign({}, crumb.filters);
        render();
      });
      node.appendChild(button);
      node.appendChild(el('span', 'crumb-sep', '\u203a'));
    });
  }

  function renderExportMenu(report) {
    const node = $('reportExport');
    if (!node) return;
    node.replaceChildren();
    if (!report || report.dashboard || !R.catalog.can_export) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    [['csv', 'Export CSV'], ['print', 'Print / PDF']].forEach((pair) => {
      const link = el('a', 'ghost');
      link.href = `/api/staff/reports/${encodeURIComponent(report.key)}/export${
        queryString({ format: pair[0] })}`;
      link.textContent = pair[1];
      if (pair[0] === 'print') link.target = '_blank';
      link.rel = 'noopener';
      node.appendChild(link);
    });
  }

  /* ------------------------------------------------------------------ boot */

  // Drill-down is delegated so a KPI card, a chart link and an alert all reach the
  // same path, and the filters and date range survive the jump (§48).
  function wireDrill() {
    const shell = $('view-reports');
    if (!shell || shell._wired) return;
    shell._wired = true;
    shell.addEventListener('click', (event) => {
      const side = event.target.closest('[data-report]');
      if (side) {
        R.breadcrumb = [];
        R.active = side.dataset.report;
        render();
        return;
      }
      const drill = event.target.closest('[data-drill]');
      if (drill && drill.dataset.drill) {
        R.breadcrumb = R.breadcrumb.concat([{ key: R.active, filters: Object.assign({}, R.filters) }]);
        R.active = drill.dataset.drill;
        render();
      }
    });
  }

  async function open() {
    wireDrill();
    if (R.loaded) { render(); return; }
    const host = $('reportBody');
    if (host) host.replaceChildren(skeleton(6));
    try {
      R.catalog = await get('/api/staff/reports');
      R.loaded = true;
      const first = (R.catalog.sections[0] || {}).reports || [];
      R.active = R.active || (first[0] && first[0].key) || null;
      if (!R.active) {
        if (host) host.replaceChildren(emptyState('You do not have access to any reports.',
          'Ask an administrator for Dashboard or Reports access.'));
        return;
      }
      render();
    } catch (error) {
      if (host) host.replaceChildren(errorState(error));
    }
  }

  window.utpReports = { open, state: R };
})();
