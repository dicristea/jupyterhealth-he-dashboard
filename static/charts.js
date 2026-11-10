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
    //        xLabel, yFormat, height, shade: [bool per x] (greys out invalid days)}
    function lineChart(container, opts) {
        container.innerHTML = '';
        const W = container.clientWidth || 700, H = opts.height || 220;
        const m = {l: 48, r: 14, t: 10, b: 30};
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
        if (opts.xLabel) el('text', {x: W - m.r, y: H - 2, 'text-anchor': 'end', 'font-size': 11, fill: '#64748b'}, svg).textContent = opts.xLabel;

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
        const W = container.clientWidth || 320, H = opts.height || 260;
        const m = {l: 48, r: 10, t: 10, b: 34};
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, container);
        const all = groups.flatMap(g => g.values.filter(v => v !== null));
        if (!all.length) return;
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

    // Actogram: one row per study day, one column per 5-minute slot, coloured by Oura activity class.
    const ACT_COLORS = {'0': '#e5e7eb', '1': '#3949ab', '2': '#bfdbfe', '3': '#fcd34d', '4': '#fb923c', '5': '#c2410c'};
    function actogram(canvas, rows) {
        const rowH = 3, slots = 288;
        canvas.width = slots; canvas.height = rows.length * rowH;
        canvas.style.height = Math.min(540, rows.length * rowH) + 'px';
        const ctx = canvas.getContext('2d');
        rows.forEach((s, r) => {
            if (!s) { ctx.fillStyle = '#ffffff'; ctx.fillRect(0, r * rowH, slots, rowH); return; }
            for (let i = 0; i < slots; i++) { ctx.fillStyle = ACT_COLORS[s[i]] || '#fff'; ctx.fillRect(i, r * rowH, 1, rowH); }
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

    // 24-hour profile (AGP-style): MET percentile bands by time of day, starting 04:00.
    function percentileProfile(container, prof) {
        container.innerHTML = '';
        const W = container.clientWidth || 700, H = 220, m = {l: 40, r: 10, t: 10, b: 28};
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, container);
        const n = prof.hours.length, x0 = prof.hours[0], x1 = prof.hours[n - 1];
        const hi = Math.max(3, ...prof.met.p90.filter(v => v !== null)) * 1.05;
        const sx = h => m.l + (h - x0) / (x1 - x0) * (W - m.l - m.r);
        const sy = y => H - m.b - y / hi * (H - m.t - m.b);
        // Shade where the patient is usually resting (darker = rests here on more days)
        prof.rest_share.forEach((r, i) => {
            if (r > 0.05) el('rect', {x: sx(prof.hours[i]), width: (W - m.l - m.r) / n + 0.5, y: m.t, height: H - m.t - m.b, fill: '#3949ab', opacity: r * 0.18}, svg);
        });
        niceTicks(0, hi, 4).forEach(t => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = t;
        });
        for (let h = 4; h <= 28; h += 4) {
            el('text', {x: sx(h), y: H - m.b + 16, 'text-anchor': h === 4 ? 'start' : h === 28 ? 'end' : 'middle', 'font-size': 11, fill: '#94a3b8'}, svg)
                .textContent = String(h % 24).padStart(2, '0') + ':00';
        }
        const band = (loK, hiK, op) => {
            let d = '';
            prof.hours.forEach((h, i) => { if (prof.met[hiK][i] !== null) d += (d ? 'L' : 'M') + sx(h) + ',' + sy(prof.met[hiK][i]); });
            for (let i = n - 1; i >= 0; i--) if (prof.met[loK][i] !== null) d += 'L' + sx(prof.hours[i]) + ',' + sy(prof.met[loK][i]);
            el('path', {d: d + 'Z', fill: '#2563eb', opacity: op}, svg);
        };
        band('p10', 'p90', 0.15);
        band('p25', 'p75', 0.3);
        el('path', {d: path(prof.hours, prof.met.p50, sx, sy), fill: 'none', stroke: '#1e3a8a', 'stroke-width': 2}, svg);
        el('text', {x: m.l + 4, y: m.t + 10, 'font-size': 11, fill: '#64748b'}, svg).textContent = 'MET';
    }

    // Stacked share bar (time-in-range style): one segment per activity class.
    function stackedBar(container, parts, colors) {
        const total = parts.reduce((a, p) => a + p.share, 0) || 1;
        container.innerHTML = '<div style="display:flex;height:34px;border-radius:6px;overflow:hidden;border:1px solid #e2e8f0">'
            + parts.map((p, i) => p.share > 0.004 ? `<div title="${p.label}: ${(p.share * 100).toFixed(1)}%" style="flex:${p.share / total};background:${colors[i]};display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:${i === 1 ? '#fff' : '#1e293b'}">${p.share > 0.07 ? Math.round(p.share * 100) + '%' : ''}</div>` : '').join('')
            + '</div>';
    }

    // Activity explorer (glucose-explorer style): 5-minute MET bars across consecutive days, activity class strip below.
    function intradayChart(container, days) {
        container.innerHTML = '';
        const W = container.clientWidth || 900, H = 230, m = {l: 40, r: 10, t: 18, b: 44}, stripH = 12;
        const svg = el('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, container);
        const total = days.length * 288;
        const all = days.flatMap(d => d.met.filter(v => v !== null));
        const hi = Math.max(3, ...(all.length ? all : [3])) * 1.05;
        const bw = (W - m.l - m.r) / total;
        const sx = i => m.l + i * bw;
        const plotB = H - m.b - stripH - 6;
        const sy = y => plotB - y / hi * (plotB - m.t);
        niceTicks(0, hi, 4).forEach(t => {
            el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: '#eef2f7'}, svg);
            el('text', {x: m.l - 6, y: sy(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#94a3b8'}, svg).textContent = t;
        });
        days.forEach((d, di) => {
            const off = di * 288;
            if (!d.valid) el('rect', {x: sx(off), y: m.t, width: bw * 288, height: plotB - m.t, fill: '#f1f5f9'}, svg);
            d.met.forEach((v, i) => {
                if (v === null) return;
                const color = v >= 6 ? '#c2410c' : v >= 3 ? '#fb923c' : v >= 1.5 ? '#fcd34d' : '#93c5fd';
                el('rect', {x: sx(off + i), y: sy(v), width: Math.max(bw, 0.6), height: plotB - sy(v), fill: color}, svg);
            });
            for (let i = 0; i < 288; i++) {
                const c = d.cls[i];
                if (c) el('rect', {x: sx(off + i), y: plotB + 6, width: Math.max(bw, 0.6), height: stripH, fill: ACT_COLORS[c]}, svg);
            }
            el('line', {x1: sx(off), x2: sx(off), y1: m.t - 6, y2: plotB + 6 + stripH, stroke: '#94a3b8'}, svg);
            el('text', {x: sx(off) + 4, y: m.t - 6, 'font-size': 11, fill: '#475569', 'font-weight': 600}, svg)
                .textContent = `Day ${d.day}${d.valid ? '' : ' (low wear)'}`;
            const step = days.length > 3 ? 12 : 4;
            for (let h = 0; h < 24; h += step) {
                el('text', {x: sx(off + h * 12), y: H - m.b + 30, 'text-anchor': 'middle', 'font-size': 10, fill: '#94a3b8'}, svg)
                    .textContent = String((4 + h) % 24).padStart(2, '0') + ':00';
            }
        });
        el('text', {x: m.l - 6, y: plotB + 6 + stripH - 2, 'text-anchor': 'end', 'font-size': 10, fill: '#94a3b8'}, svg).textContent = 'class';
    }

    window.Charts = {lineChart, stripChart, actogram, sparkline, percentileProfile, stackedBar, intradayChart, ACT_COLORS};
})();
