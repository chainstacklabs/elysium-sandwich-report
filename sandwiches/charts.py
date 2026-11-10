"""Inline SVG charts. Colors come from CSS variables so the page follows light and dark mode."""
from html import escape

W = 960  # viewBox width; the SVG scales to the page
GAP = 2  # surface gap between stacked segments


def _open(w, h, label):
    return f'<svg viewBox="0 0 {w} {h:.0f}" width="100%" role="img" aria-label="{escape(label)}">'


def nice_top(value, steps=4):
    """Axis top and step: a round number per step that fits `value`."""
    raw = max(value, 1e-9) / steps
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    return step * steps, step


def _column(x, top, w, h, r=3):
    """A column rounded at the top and square at the baseline."""
    r = min(r, w / 2, h)
    return (f'M{x:.1f} {top + h:.1f}V{top + r:.1f}Q{x:.1f} {top:.1f} {x + r:.1f} {top:.1f}'
            f'H{x + w - r:.1f}Q{x + w:.1f} {top:.1f} {x + w:.1f} {top + r:.1f}V{top + h:.1f}Z')


def _bar(x, y, w, h, r=3):
    """A horizontal bar rounded at the end and square at the baseline."""
    r = min(r, h / 2, w)
    return (f'M{x:.1f} {y:.1f}H{x + w - r:.1f}Q{x + w:.1f} {y:.1f} {x + w:.1f} {y + r:.1f}'
            f'V{y + h - r:.1f}Q{x + w:.1f} {y + h:.1f} {x + w - r:.1f} {y + h:.1f}H{x:.1f}Z')


def _grid(x0, x1, y, top, step, fmt):
    out, v = [], 0
    while v <= top + 1e-9:
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"base" if v == 0 else "grid"}"/>'
                   f'<text x="{x0 - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="tick">{fmt(v)}</text>')
        v += step
    return "".join(out)


def _place(events, x_of, right):
    """Put event labels in rows so none overlap: [(line x, text x, anchor, row, text, css class)], row count."""
    rows, out = [], []
    for pos, text, cls in sorted(events):
        x, width = x_of(pos), 6.6 * len(text)
        if x + 5 + width <= right:
            span, tx, anchor = (x + 5, x + 5 + width), x + 5, "start"
        else:
            span, tx, anchor = (x - 5 - width, x - 5), x - 5, "end"
        row = next((i for i, used in enumerate(rows) if all(span[0] > b + 10 or span[1] < a - 10 for a, b in used)),
                   len(rows))
        if row == len(rows):
            rows.append([])
        rows[row].append(span)
        out.append((x, tx, anchor, row, text, cls))
    return out, len(rows)


def stacked_bars(columns, series, label, *, total=None, axis=None, band=None, events=(), height=260, centered=True,
                 tip=str, tip_key=None):
    """Stacked columns. columns: [(key, axis label)]; series: [(name, color, {key: value})].
    total formats a label on top of each column (no grid); axis formats grid ticks instead;
    tip formats hover values and tip_key names a column in them.
    band: (first key of the second period, label before, label from); events: [(column position, text, css class)]."""
    ml, mr, mb = (56 if axis else 8), 12, 26
    keys = [k for k, _ in columns]
    totals = [sum(s[2].get(k, 0) for s in series) for k in keys]
    top, step = nice_top(max(totals) * (1.1 if total else 1.0))
    bw = (W - ml - mr) / len(columns)
    cw = min(bw * 0.72, 44)
    placed, n_rows = _place(events, lambda p: ml + p * bw, W - mr)
    band_top = 6 + 17 * n_rows
    mt = band_top + (24 if band else 0) + (16 if total else 0)
    y = lambda v: mt + height * (1 - v / top)  # noqa: E731
    out = [_open(W, mt + height + mb, label)]
    if band:
        i = keys.index(band[0])
        out.append(f'<rect x="{ml}" y="{band_top}" width="{i * bw:.1f}" height="{mt + height - band_top}" class="band"/>'
                   f'<text x="{ml + 8}" y="{band_top + 16}" class="bandl">{escape(band[1])}</text>'
                   f'<text x="{ml + i * bw + 8}" y="{band_top + 16}" class="bandl">{escape(band[2])}</text>')
    if axis:
        out.append(_grid(ml, W - mr, y, top, step, axis))
    for i, (key, text) in enumerate(columns):
        x = ml + i * bw + ((bw - cw) / 2 if centered else 0)
        stack = [(name, color, values.get(key, 0)) for name, color, values in series if values.get(key, 0) > 0]
        acc = 0
        for j, (name, color, v) in enumerate(stack):
            t, b = y(acc + v), y(acc)
            if acc > 0 and cw >= 12 and b - t > 2 * GAP:  # thin columns get no gap, or small segments read as holes
                b -= GAP
            hover = f"<title>{escape(tip_key(key) if tip_key else text or key)}, {escape(name)}: {tip(v)}</title>"
            if j == len(stack) - 1:
                out.append(f'<path d="{_column(x, t, cw, b - t)}" style="fill:{color}">{hover}</path>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{t:.1f}" width="{cw:.1f}" height="{b - t:.1f}" style="fill:{color}">{hover}</rect>')
            acc += v
        if total and acc > 0:
            out.append(f'<text x="{x + cw / 2:.1f}" y="{y(acc) - 6:.1f}" text-anchor="middle" class="val">{total(acc)}</text>')
        if text:
            anchor = f'x="{x + cw / 2:.1f}" text-anchor="middle"' if centered else f'x="{x:.1f}"'
            out.append(f'<text {anchor} y="{mt + height + 18}" class="tick">{escape(text)}</text>')
    if not axis:
        out.append(f'<line x1="{ml}" x2="{W - mr}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="base"/>')
    for x, tx, anchor, row, text, cls in placed:
        ty = 14 + 17 * row
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ty - 10}" y2="{y(0):.1f}" class="ev {cls}"/>'
                   f'<text x="{tx:.1f}" y="{ty}" text-anchor="{anchor}" class="evl">{escape(text)}</text>')
    return "".join(out) + "</svg>"


def bars(items, fmt, label, label_width=150, row=28):
    """Horizontal bars, one series: items = [(label, value)]."""
    bh = row - 10
    top = max((v for _, v in items), default=1) or 1
    span = W - label_width - 90
    out = [_open(W, row * len(items) + 8, label)]
    for i, (text, v) in enumerate(items):
        y0, w = 4 + i * row, span * v / top
        out.append(f'<text x="{label_width - 12}" y="{y0 + row / 2 + 4:.1f}" text-anchor="end" class="lab">{escape(text)}</text>'
                   f'<path d="{_bar(label_width, y0 + (row - bh) / 2, max(1, w), bh)}" style="fill:var(--bar)"/>'
                   f'<text x="{label_width + w + 8:.1f}" y="{y0 + row / 2 + 4:.1f}" class="val">{escape(fmt(v))}</text>')
    return "".join(out) + "</svg>"
