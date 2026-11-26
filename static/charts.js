// Hand-written SVG charts (no chart library, same rule as the original workbench).
(function () {
    'use strict';
    const NS = 'http://www.w3.org/2000/svg';

    function el(tag, attrs, parent) {
        const node = document.createElementNS(NS, tag);
        for (const k in attrs) node.setAttribute(k, attrs[k]);
        if (parent) parent.appendChild(node);
        return node;
    }

    function niceTicks(lo, hi, count) {
        const span = hi - lo || 1;
        const step0 = span / count;
        const mag = Math.pow(10, Math.floor(Math.log10(step0)));
        const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= count) || 10 * mag;
        const ticks = [];
        for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) ticks.push(+t.toFixed(10));
        return ticks;
    }

    // Axis titles: x centred under the tick labels, y rotated along the left edge.
    function axisTitles(svg, W, H, m, xTitle, yTitle) {
        if (xTitle) el('text', {x: (m.l + W - m.r) / 2, y: H - 5, 'text-anchor': 'middle', 'font-size': 11, fill: '#64748b'}, svg).textContent = xTitle;
        if (yTitle) {
            const cy = (m.t + H - m.b) / 2;
            el('text', {x: 12, y: cy, 'text-anchor': 'middle', 'font-size': 11, fill: '#64748b', transform: `rotate(-90 12 ${cy})`}, svg).textContent = yTitle;
        }
    }
    const margins = (xTitle, yTitle, base) => ({l: base.l + (yTitle ? 16 : 0), r: base.r, t: base.t, b: base.b + (xTitle ? 14 : 0)});

    function path(xs, ys, sx, sy) {
        let d = '', pen = false;
        xs.forEach((x, i) => {
            const y = ys[i];
            if (y === null || y === undefined || Number.isNaN(y)) { pen = false; return; }
            d += (pen ? 'L' : 'M') + sx(x).toFixed(1) + ',' + sy(y).toFixed(1);
            pen = true;
        });
        return d;
    }

    // opts: {x: [...], series: [{y, color, width, opacity, dots}], bands: [{lo, hi, color, x?}],
    //        xLabel, yLabel (axis titles), yFormat, height, shade: [bool per x] (greys out invalid days)}
    function lineChart(container, opts) {
        container.innerHTML = '';
        const W = container.clientWidth || 700, H = (opts.height || 220) + (opts.xLabel ? 14 : 0);
        const m = margins(opts.xLabel, opts.yLabel, {l: 48, r: 14, t: 10, b: 30});
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img'}, container);
        const allX = opts.x;
        const ys = [];
        opts.series.forEach(s => s.y.forEach(v => { if (v !== null) ys.push(v); }));
        (opts.bands || []).forEach(b => { b.lo.forEach(v => v !== null && ys.push(v)); b.hi.forEach(v => v !== null && ys.push(v)); });
        if (!ys.length) { el('text', {x: W / 2, y: H / 2, 'text-anchor': 'middle', fill: '#94a3b8', 'font-size': 13}, svg).textContent = 'No data'; return; }
        let lo = Math.min(...ys), hi = Math.max(...ys);
        const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
        const x0 = Math.min(...allX), x1 = Math.max(...allX);
        const sx = x => m.l + (x - x0) / ((x1 - x0) || 1) * (W - m.l - m.r);
        const sy = y => H - m.b - (y - lo) / (hi - lo) * (H - m.t - m.b);
        const fmt = opts.yFormat || (v => +v.toFixed(1));

        if (opts.shade) {
            const w = (W - m.l - m.r) / Math.max(1, allX.length - 1);
            opts.shade.forEach((bad, i) => {
                if (bad) el('rect', {x: sx(allX[i]) - w / 2, y: m.t, width: w, height: H - m.t - m.b, fill: '#f1f5f9'}, svg);
            });
        }
        niceTicks(lo, hi, 4).forEach(t => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = fmt(t);
        });
        niceTicks(x0, x1, 8).forEach(t => {
            el('text', {x: sx(t), y: H - m.b + 16, 'text-anchor': 'middle', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = t;
        });
        axisTitles(svg, W, H, m, opts.xLabel, opts.yLabel);

        (opts.bands || []).forEach(b => {
            const bx = b.x || allX;
            let up = '', down = '';
            bx.forEach((x, i) => { if (b.hi[i] !== null) up += (up ? 'L' : 'M') + sx(x) + ',' + sy(b.hi[i]); });
            for (let i = bx.length - 1; i >= 0; i--) if (b.lo[i] !== null) down += 'L' + sx(bx[i]) + ',' + sy(b.lo[i]);
            if (up) el('path', {d: up + down + 'Z', fill: b.color, opacity: 0.15}, svg);
        });
        opts.series.forEach(s => {
            const sxs = s.x || allX;
            if (s.dots) {
                sxs.forEach((x, i) => { if (s.y[i] !== null) el('circle', {cx: sx(x), cy: sy(s.y[i]), r: 1.8, fill: s.color, opacity: s.opacity || 0.5}, svg); });
            } else {
                el('path', {d: path(sxs, s.y, sx, sy), fill: 'none', stroke: s.color, 'stroke-width': s.width || 2,
                            opacity: s.opacity || 1, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, svg);
            }
        });
        if (opts.refLines) opts.refLines.forEach(r => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(r.y), y2: sy(r.y), stroke: r.color, 'stroke-dasharray': '4 4'}, svg);
        });
    }

    // Each group: {name, color, values}. Box (IQR + median) with jittered dots, one per person.
    function stripChart(container, groups, opts) {
        container.innerHTML = '';
        const W = container.clientWidth || 320, H = (opts.height || 260) + (opts.xLabel ? 14 : 0);
        const m = margins(opts.xLabel, opts.yLabel, {l: 48, r: 10, t: 10, b: 34});
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, container);
        const all = groups.flatMap(g => g.values.filter(v => v !== null));
        if (!all.length) return;
        axisTitles(svg, W, H, m, opts.xLabel, opts.yLabel);
        let lo = Math.min(...all), hi = Math.max(...all);
        const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
        const sy = y => H - m.b - (y - lo) / (hi - lo) * (H - m.t - m.b);
        const bw = (W - m.l - m.r) / groups.length;
        const fmt = opts.yFormat || (v => +v.toFixed(1));
        niceTicks(lo, hi, 4).forEach(t => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = fmt(t);
        });
        groups.forEach((g, i) => {
            const cx = m.l + bw * (i + 0.5);
            const v = g.values.filter(x => x !== null).sort((a, b) => a - b);
            const q = p => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))];
            if (v.length) {
                el('rect', {x: cx - bw * 0.22, y: sy(q(0.75)), width: bw * 0.44, height: Math.max(1, sy(q(0.25)) - sy(q(0.75))),
                            fill: g.color, opacity: 0.12, stroke: g.color, rx: 4}, svg);
                el('line', {x1: cx - bw * 0.22, x2: cx + bw * 0.22, y1: sy(q(0.5)), y2: sy(q(0.5)), stroke: g.color, 'stroke-width': 2.5}, svg);
            }
            v.forEach((y, j) => {
                const jitter = ((j * 9301 + 49297) % 233280) / 233280 - 0.5;  // deterministic, so dots don't jump on redraw
                el('circle', {cx: cx + jitter * bw * 0.36, cy: sy(y), r: 3, fill: g.color, opacity: 0.55}, svg);
            });
            el('text', {x: cx, y: H - m.b + 16, 'text-anchor': 'middle', 'font-size': 11, fill: '#475569'}, svg).textContent = g.name;
            el('text', {x: cx, y: H - m.b + 29, 'text-anchor': 'middle', 'font-size': 10, fill: '#94a3b8'}, svg).textContent = 'n=' + v.length;
        });
    }

    // Tile sparkline (lab-card style): weekly medians, personal usual range shaded, latest point filled.
    function sparkline(container, values, band) {
        container.innerHTML = '';
        const W = container.clientWidth || 160, H = 36, pad = 4;
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, container);
        const v = values.filter(x => x !== null);
        if (v.length < 2) return;
        let lo = Math.min(...v, band[0] ?? Infinity), hi = Math.max(...v, band[1] ?? -Infinity);
        const p = (hi - lo) * 0.1 || 1; lo -= p; hi += p;
        const sx = i => pad + i / (values.length - 1) * (W - 2 * pad);
        const sy = y => H - pad - (y - lo) / (hi - lo) * (H - 2 * pad);
        if (band[0] !== null && band[1] !== null) {
            el('rect', {x: pad, width: W - 2 * pad, y: sy(band[1]), height: Math.max(1, sy(band[0]) - sy(band[1])), fill: '#dcfce7', opacity: 0.8}, svg);
            el('line', {x1: pad, x2: W - pad, y1: sy(band[1]), y2: sy(band[1]), stroke: '#86efac', 'stroke-dasharray': '3 3'}, svg);
        }
        el('path', {d: path(values.map((_, i) => i), values, sx, sy), fill: 'none', stroke: '#1e3a8a', 'stroke-width': 1.6}, svg);
        let last = values.length - 1;
        while (last >= 0 && values[last] === null) last--;
        if (last >= 0) el('circle', {cx: sx(last), cy: sy(values[last]), r: 3, fill: '#1e3a8a'}, svg);
    }

    const clockLabel = h => { const m = ((Math.round((18 + h) * 60) % 1440) + 1440) % 1440; return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'); };

    // Sleep timing: one vertical bar per night (x = study day of waking), from bedtime (top) to wake (bottom), on
    // the hours-after-18:00 scale so nights crossing midnight stay continuous. band = usual midpoint [q1, q3].
    // Clicking a night calls onSelect(night) and fires a 'nightselect' event on the container.
    function sleepTiming(container, nights, band, onSelect, selectedDay) {
        container.innerHTML = '';
        const W = container.clientWidth || 800, H = 334, m = margins(true, true, {l: 48, r: 12, t: 10, b: 30});
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Sleep timing by night'}, container);
        axisTitles(svg, W, H, m, 'Study day of waking', 'Clock time');
        const rec = nights.filter(n => n.type && n.start !== null && n.end !== null);
        // 18:00 → 14:00 by default, stretched for main nights only (to 16:00 at most); a daytime record outside the
        // range is clipped to its edge, so it still shows and can be clicked.
        const mains = rec.filter(n => n.main);
        let lo = Math.min(0, ...mains.map(n => n.start)), hi = Math.max(20, ...mains.map(n => n.end));
        lo = Math.max(-2, Math.floor(lo / 2) * 2); hi = Math.min(22, Math.ceil(hi / 2) * 2);
        const cw = (W - m.l - m.r) / Math.max(1, nights.length);
        const cx = i => m.l + cw * (i + 0.5);
        const sy = h => m.t + (Math.min(hi, Math.max(lo, h)) - lo) / (hi - lo) * (H - m.t - m.b);
        if (band && band[0] !== null && band[1] !== null) {
            el('rect', {x: m.l, width: W - m.l - m.r, y: sy(band[0]), height: Math.max(2, sy(band[1]) - sy(band[0])), fill: '#dcfce7', opacity: 0.9}, svg);
        }
        const step = 2;
        for (let h = Math.ceil(lo / step) * step; h <= hi; h += step) {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(h), y2: sy(h), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(h) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = clockLabel(h);
        }
        const bw = Math.max(3, Math.min(16, cw * 0.55));
        const every = Math.ceil(nights.length / Math.max(1, Math.floor((W - m.l - m.r) / 44)));
        let marker = null;
        nights.forEach((n, i) => {
            const x = cx(i);
            if (i % every === 0 || i === nights.length - 1) {
                el('text', {x, y: H - m.b + 16, 'text-anchor': 'middle', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = n.day;
            }
            if (!n.type) {
                el('rect', {x: x - cw / 2 + 1, width: cw - 2, y: m.t, height: H - m.t - m.b, fill: '#f1f5f9'}, svg);
                if (cw >= 12) {
                    const t = el('text', {x, y: H - m.b - 8, 'font-size': 10, fill: '#94a3b8', transform: `rotate(-90 ${x} ${H - m.b - 8})`}, svg);
                    t.textContent = 'no record';
                }
                return;
            }
            if (n.start === null || n.end === null) return;
            el('rect', {x: x - bw / 2, width: bw, y: Math.min(sy(n.start), H - m.b - 3), height: Math.max(3, sy(n.end) - sy(n.start)), rx: Math.min(3, bw / 3),
                        fill: n.main ? '#3949ab' : '#c7d2fe'}, svg);
            if (n.main) el('circle', {cx: x, cy: sy((n.start + n.end) / 2), r: Math.min(4, bw / 2 + 1), fill: '#f59e0b', stroke: '#fff', 'stroke-width': 1.5}, svg);
            const hit = el('rect', {x: x - cw / 2, width: cw, y: m.t, height: H - m.t - m.b, fill: 'transparent', style: 'cursor:pointer'}, svg);
            const title = el('title', {}, hit);
            title.textContent = `Night ending day ${n.day}: ${clockLabel(n.start)} → ${clockLabel(n.end)}${n.main ? '' : ' (' + n.type.replace('_', ' ') + ')'}`;
            const select = () => {
                if (marker) marker.remove();
                marker = el('rect', {x: x - cw / 2 + 0.5, width: cw - 1, y: m.t, height: H - m.t - m.b, fill: 'none', stroke: '#2563eb', 'stroke-width': 1.5, rx: 3}, svg);
                onSelect(n);
                container.dispatchEvent(new CustomEvent('nightselect', {detail: n}));
            };
            hit.addEventListener('click', select);
            if (n.day === selectedDay) marker = el('rect', {x: x - cw / 2 + 0.5, width: cw - 1, y: m.t, height: H - m.t - m.b, fill: 'none', stroke: '#2563eb', 'stroke-width': 1.5, rx: 3}, svg);
        });
    }

    // Hypnogram for one night: a row per stage (awake on top, deep at the bottom), one block per run of 5-minute slots.
    const STAGE_ROWS = [['4', 'Awake', '#f59e0b'], ['3', 'REM', '#8b5cf6'], ['2', 'Light', '#60a5fa'], ['1', 'Deep', '#1e3a8a']];
    function hypnogram(container, night) {
        container.innerHTML = '';
        const W = container.clientWidth || 800, H = 154, m = margins(true, true, {l: 48, r: 12, t: 6, b: 24});
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Sleep stages'}, container);
        const s = night.hyp || '';
        if (!s) { el('text', {x: W / 2, y: H / 2, 'text-anchor': 'middle', fill: '#94a3b8', 'font-size': 13}, svg).textContent = 'No sleep-stage record for this night'; return; }
        axisTitles(svg, W, H, m, 'Clock time', 'Stage');
        const t0 = night.start, t1 = night.start + s.length / 12;
        const sx = h => m.l + (h - t0) / ((t1 - t0) || 1) * (W - m.l - m.r);
        const rowH = (H - m.t - m.b) / STAGE_ROWS.length;
        const row = {};
        STAGE_ROWS.forEach(([code, label], i) => {
            row[code] = m.t + i * rowH;
            el('line', {x1: m.l, x2: W - m.r, y1: m.t + (i + 0.5) * rowH, y2: m.t + (i + 0.5) * rowH, stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: m.t + (i + 0.5) * rowH + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = label;
        });
        const color = Object.fromEntries(STAGE_ROWS.map(([c, , col]) => [c, col]));
        for (let i = 0; i < s.length;) {
            let j = i;
            while (j < s.length && s[j] === s[i]) j++;
            el('rect', {x: sx(t0 + i / 12), width: Math.max(1, sx(t0 + j / 12) - sx(t0 + i / 12)), y: row[s[i]] + 3, height: rowH - 6, fill: color[s[i]], rx: 1}, svg);
            i = j;
        }
        for (let h = Math.ceil(t0); h <= t1; h++) {
            el('text', {x: sx(h), y: H - m.b + 16, 'text-anchor': 'middle', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = clockLabel(h);
        }
    }

    // Oura activity classes 0-5: ring off, rest, inactive, low, medium, high.
    const ACT_COLORS = ['#e5e7eb', '#3949ab', '#bfdbfe', '#fcd34d', '#fb923c', '#c2410c'];
    const slotClock = h => String(Math.round(h) % 24).padStart(2, '0') + ':00';

    // 24-hour activity profile: MET percentile bands per 5-minute slot (04:00 -> 04:00), shaded where the patient
    // usually rests (darker = rests then on more days). prof: {hours, met: {p10..p90}, rest_share}.
    function percentileProfile(container, prof) {
        container.innerHTML = '';
        const W = container.clientWidth || 700, H = 240, m = margins(true, true, {l: 40, r: 12, t: 10, b: 28});
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '24-hour activity profile'}, container);
        const n = prof.hours.length, x0 = prof.hours[0], x1 = x0 + 24;
        const hi = Math.max(3, ...prof.met.p90.filter(v => v !== null)) * 1.05;
        const sx = h => m.l + (h - x0) / (x1 - x0) * (W - m.l - m.r);
        const sy = y => H - m.b - y / hi * (H - m.t - m.b);
        prof.rest_share.forEach((r, i) => {
            if (r > 0.05) el('rect', {x: sx(prof.hours[i]), width: (W - m.l - m.r) / n + 0.5, y: m.t, height: H - m.t - m.b, fill: '#7c3aed', opacity: r * 0.16}, svg);
        });
        niceTicks(0, hi, 4).forEach(t => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = t;
        });
        for (let h = x0; h <= x1; h += 4) {
            el('text', {x: sx(h), y: H - m.b + 16, 'text-anchor': h === x0 ? 'start' : h === x1 ? 'end' : 'middle', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = slotClock(h);
        }
        const band = (loK, hiK, op) => {
            let d = '';
            prof.hours.forEach((h, i) => { if (prof.met[hiK][i] !== null) d += (d ? 'L' : 'M') + sx(h) + ',' + sy(prof.met[hiK][i]); });
            for (let i = n - 1; i >= 0; i--) if (prof.met[loK][i] !== null) d += 'L' + sx(prof.hours[i]) + ',' + sy(prof.met[loK][i]);
            if (d) el('path', {d: d + 'Z', fill: '#2563eb', opacity: op}, svg);
        };
        band('p10', 'p90', 0.15);
        band('p25', 'p75', 0.3);
        el('path', {d: path(prof.hours, prof.met.p50, sx, sy), fill: 'none', stroke: '#1e3a8a', 'stroke-width': 2}, svg);
        axisTitles(svg, W, H, m, 'Time of day', 'Activity (MET)');
    }

    // Horizontal bars, one per item: [{label, hours}]; colors[i] per bar (offset by 1: class 0 is left out).
    function hBars(container, items, colors, xTitle) {
        container.innerHTML = '';
        const rowH = 30, W = container.clientWidth || 360, m = {l: 76, r: 40, t: 4, b: 44};
        const H = m.t + m.b + rowH * items.length;
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': xTitle}, container);
        const hi = Math.max(1, ...items.map(d => d.hours));
        const ticks = niceTicks(0, hi, 4), top = ticks[ticks.length - 1] || hi;
        const sx = v => m.l + v / Math.max(top, hi) * (W - m.l - m.r);
        ticks.forEach(t => {
            el('line', {x1: sx(t), x2: sx(t), y1: m.t, y2: H - m.b, stroke: '#eef2f7'}, svg);
            el('text', {x: sx(t), y: H - m.b + 16, 'text-anchor': 'middle', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = t;
        });
        items.forEach((d, i) => {
            const y = m.t + i * rowH;
            el('text', {x: m.l - 8, y: y + rowH / 2 + 4, 'text-anchor': 'end', 'font-size': 12, fill: '#475569'}, svg).textContent = d.label;
            el('rect', {x: m.l, y: y + 6, width: Math.max(1, sx(d.hours) - m.l), height: rowH - 12, rx: 3, fill: colors[i + 1]}, svg);
            el('text', {x: sx(d.hours) + 5, y: y + rowH / 2 + 4, 'font-size': 11, fill: '#475569'}, svg).textContent = d.hours + ' h';
        });
        axisTitles(svg, W, H, m, xTitle, null);
    }

    // Actogram: one row per study day, one column per 5-minute slot (04:00 -> 04:00), coloured by activity class.
    // rows: [{day, valid, cls}] (cls = 288-character string or ''); invalid days are hatched.
    function actogram(container, rows) {
        container.innerHTML = '';
        const W = container.clientWidth || 800, rowH = rows.length > 40 ? 5 : 8;
        const m = margins(true, true, {l: 40, r: 12, t: 4, b: 26}), H = m.t + m.b + rows.length * rowH;
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Rest and activity by day'}, container);
        const defs = el('defs', {}, svg);
        const pat = el('pattern', {id: 'actHatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'}, defs);
        el('line', {x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#cbd5e1', 'stroke-width': 2}, pat);
        const cw = (W - m.l - m.r) / 288;
        const every = Math.ceil(rows.length / Math.max(1, Math.floor(rows.length * rowH / 22)));
        rows.forEach((r, k) => {
            const y = m.t + k * rowH;
            if (r.cls) {
                for (let i = 0; i < r.cls.length;) {  // one rect per run of the same class
                    let j = i;
                    while (j < r.cls.length && r.cls[j] === r.cls[i]) j++;
                    el('rect', {x: m.l + i * cw, y, width: (j - i) * cw + 0.3, height: rowH, fill: ACT_COLORS[+r.cls[i]] || '#fff'}, svg);
                    i = j;
                }
            }
            if (!r.valid) el('rect', {x: m.l, y, width: W - m.l - m.r, height: rowH, fill: 'url(#actHatch)', opacity: r.cls ? 0.8 : 1}, svg);
            if (k % every === 0 || k === rows.length - 1) {
                el('text', {x: m.l - 6, y: y + rowH / 2 + 4, 'text-anchor': 'end', 'font-size': 10, fill: '#94a3b8'}, svg).textContent = r.day;
            }
        });
        for (let h = 4; h <= 28; h += 4) {
            el('text', {x: m.l + (h - 4) / 24 * (W - m.l - m.r), y: H - m.b + 16, 'text-anchor': h === 4 ? 'start' : h === 28 ? 'end' : 'middle',
                        'font-size': 11, fill: '#94a3b8'}, svg).textContent = slotClock(h);
        }
        axisTitles(svg, W, H, m, 'Time of day', 'Study day');
    }

    window.Charts = {lineChart, stripChart, sparkline, sleepTiming, hypnogram, percentileProfile, hBars, actogram, ACT_COLORS};
})();
