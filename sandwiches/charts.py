"""Inline SVG charts. Colors come from CSS variables defined in report.css."""

from html import escape

W = 948  # viewBox width: the page's content width (980 - 2 x 16), so one unit is one pixel on a desktop


def _open(w, h, label):
    return f'<svg viewBox="0 0 {w} {h:.0f}" width="100%" role="img" aria-label="{escape(label)}">'


def nice_top(value, steps=4):
    """Axis top and step: a round number per step that fits `value`."""
    raw = max(value, 1e-9) / steps
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    return step * steps, step


def _bar(x, y, w, h, r=3):
    """A horizontal bar rounded at the end and square at the baseline."""
    r = min(r, h / 2, w)
    return (
        f"M{x:.1f} {y:.1f}H{x + w - r:.1f}Q{x + w:.1f} {y:.1f} {x + w:.1f} {y + r:.1f}"
        f"V{y + h - r:.1f}Q{x + w:.1f} {y + h:.1f} {x + w - r:.1f} {y + h:.1f}H{x:.1f}Z"
    )


def _grid(x0, x1, y, top, step, fmt):
    out, v = [], 0
    while v <= top + 1e-9:
        out.append(
            f'<line x1="{x0}" x2="{x1}" y1="{y(v) + 0.5}" y2="{y(v) + 0.5}" class="{"base" if v == 0 else "grid"}"/>'
            f'<text x="{x0 - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="tick">{fmt(v)}</text>'
        )
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
        row = next(
            (i for i, used in enumerate(rows) if all(span[0] > b + 10 or span[1] < a - 10 for a, b in used)), len(rows)
        )
        if row == len(rows):
            rows.append([])
        rows[row].append(span)
        out.append((x, tx, anchor, row, text, cls))
    return out, len(rows)


def _frame(columns, series, *, axis, band, events, height, label):
    """Layout shared by the stacked charts: margins, scale, band, grid and event marks (behind the data)."""
    ml, mr, mb = (56 if axis else 8), 12, 26
    keys = [k for k, _ in columns]
    totals = [sum(s[2].get(k, 0) for s in series) for k in keys]
    top, step = nice_top(max(totals))
    bw = (W - ml - mr) / len(columns)
    placed, n_rows = _place(events, lambda p: ml + p * bw, W - mr)
    band_top = 6 + 17 * n_rows
    mt = band_top + (8 if band else 0)
    y = lambda v: round(mt + height * (1 - v / top))  # noqa: E731  whole units: edges fall on pixels
    out = [_open(W, mt + height + mb, label)]
    if band:
        i = keys.index(band[0])
        out.append(
            f'<rect x="{ml}" y="{band_top}" width="{round(i * bw)}" height="{mt + height - band_top}" class="band"/>'
        )
    if axis:
        out.append(_grid(ml, W - mr, y, top, step, axis))
    for x, tx, anchor, row, text, cls in placed:
        ty = 14 + 17 * row
        lx = round(x) + 0.5
        out.append(
            f'<line x1="{lx}" x2="{lx}" y1="{ty - 10}" y2="{y(0)}" class="ev {cls}"/>'
            f'<text x="{tx:.1f}" y="{ty}" text-anchor="{anchor}" class="evl">{escape(text)}</text>'
        )
    return out, keys, totals, ml, mr, mt, bw, y


def stacked_area(columns, series, label, *, axis, band=None, events=(), height=260, tip=str, tip_key=None):
    """Stacked areas for many time points (hours), first series at the bottom on the shared baseline.
    Points sit at the middle of each column and are joined by straight lines. Each band is filled from its top boundary
    down to the baseline and the bands below paint over it, so neighbouring colors meet with no seam or gap. A transparent strip per column carries the hover text with every value."""
    out, keys, totals, ml, mr, mt, bw, y = _frame(
        columns, series, axis=axis, band=band, events=events, height=height, label=label
    )
    fy = lambda v: mt + height * (1 - v / _top(totals))  # noqa: E731  unrounded: lines stay smooth
    xs = [ml + (i + 0.5) * bw for i in range(len(keys))]
    cum, acc = [[0.0] * len(keys)], [0.0] * len(keys)
    for _, _, values in series:
        acc = [a + values.get(k, 0) for a, k in zip(acc, keys, strict=True)]
        cum.append(acc)

    def line(vals):
        return " ".join(f"{x:.1f},{fy(v):.1f}" for x, v in zip(xs, vals, strict=True))

    base = f"{xs[-1]:.1f},{fy(0):.1f} {xs[0]:.1f},{fy(0):.1f}"
    for j in range(len(series) - 1, -1, -1):  # top band first; each lower band paints over it
        out.append(f'<polygon points="{line(cum[j + 1])} {base}" style="fill:{series[j][1]}"/>')
    out.append(f'<line x1="{ml}" x2="{W - mr}" y1="{y(0) + 0.5}" y2="{y(0) + 0.5}" class="base"/>')
    for i, (key, text) in enumerate(columns):
        x0 = ml + i * bw
        if totals[i] > 0:
            lines = "; ".join(f"{name}: {tip(values.get(key, 0))}" for name, _, values in series if values.get(key, 0))
            out.append(
                f'<rect x="{x0:.1f}" y="{mt}" width="{bw:.1f}" height="{height}" class="hit">'
                f"<title>{escape(tip_key(key) if tip_key else text or key)}: {escape(lines)}</title></rect>"
            )
        if text:
            out.append(f'<text x="{x0:.1f}" y="{mt + height + 18}" class="tick">{escape(text)}</text>')
    return "".join(out) + "</svg>"


def _top(totals):
    return nice_top(max(totals))[0]


def bars(items, fmt, label, label_width=150, row=28):
    """Horizontal bars, one series: items = [(label, value)]."""
    bh = row - 10
    top = max((v for _, v in items), default=1) or 1
    span = W - label_width - 90
    out = [_open(W, row * len(items) + 8, label)]
    for i, (text, v) in enumerate(items):
        y0, w = 4 + i * row, span * v / top
        baseline = y0 + row / 2 + 4
        out.append(
            f'<text x="{label_width - 12}" y="{baseline:.1f}" text-anchor="end" class="lab">{escape(text)}</text>'
            f'<path d="{_bar(label_width, y0 + (row - bh) / 2, max(1, w), bh)}" style="fill:var(--bar)"/>'
            f'<text x="{label_width + w + 8:.1f}" y="{y0 + row / 2 + 4:.1f}" class="val">{escape(fmt(v))}</text>'
        )
    return "".join(out) + "</svg>"
