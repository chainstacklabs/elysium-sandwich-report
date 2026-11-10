"""Inline SVG charts. Colors come from CSS variables so the page follows light and dark mode."""
from html import escape


def _open(w, h, label):
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{escape(label)}">'


def nice_top(value, steps=4):
    """Axis top and step: a round number per step that fits `value`."""
    raw = max(value, 1e-9) / steps
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    return step * steps, step


def _grid(x0, x1, y, top, step, fmt):
    out, v = [], 0
    while v <= top + 1e-9:
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{x0 - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="tick">{fmt(v)}</text>')
        v += step
    return "".join(out)


def stacked_bars(columns, series, fmt, label, band=None, every=1, label_values=True, events=(), height=330):
    """columns: [(key, axis label)]; series: [(name, color, {key: value})]; band: (first key, text left, text right);
    events: [(column position as float, number, css class)]."""
    W, H, ml, mr, mt, mb = 980, height, 64, 16, 48, 40
    totals = {k: sum(s[2].get(k, 0) for s in series) for k, _ in columns}
    top, step = nice_top(max(totals.values()) * 1.08)
    bw = (W - ml - mr) / len(columns)
    y = lambda v: mt + (H - mt - mb) * (1 - v / top)  # noqa: E731
    out = [_open(W, H, label)]
    if band:
        i = [k for k, _ in columns].index(band[0])
        out.append(f'<rect x="{ml}" y="{mt - 30}" width="{i * bw:.1f}" height="{H - mb - mt + 30}" class="band"/>'
                   f'<text x="{ml + 6}" y="{mt - 16}" class="bandl">{escape(band[1])}</text>'
                   f'<text x="{ml + i * bw + 6}" y="{mt - 16}" class="bandl">{escape(band[2])}</text>')
    out.append(_grid(ml, W - mr, y, top, step, fmt))
    for i, (key, axis) in enumerate(columns):
        x, acc = ml + i * bw + bw * 0.12, 0
        for name, color, values in series:
            v = values.get(key, 0)
            if v > 0:
                out.append(f'<rect x="{x:.1f}" y="{y(acc + v):.1f}" width="{max(1, bw * 0.76):.1f}" '
                           f'height="{y(acc) - y(acc + v):.1f}" style="fill:{color}"><title>{escape(axis)}: {escape(name)} {fmt(v)}</title></rect>')
                acc += v
        if label_values and acc > 0:
            out.append(f'<text x="{x + bw * 0.38:.1f}" y="{y(acc) - 6:.1f}" text-anchor="middle" class="val">{fmt(acc)}</text>')
        if i % every == 0:
            out.append(f'<text x="{x:.1f}" y="{H - mb + 18}" class="tick">{escape(axis)}</text>')
    for pos, n, cls in events:
        x, lift = ml + pos * bw, (n % 2) * 14
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{mt - 24 + lift}" y2="{H - mb}" class="ev {cls}"/>'
                   f'<circle cx="{x:.1f}" cy="{mt - 30 + lift}" r="8" class="evc {cls}"/>'
                   f'<text x="{x:.1f}" y="{mt - 26 + lift}" text-anchor="middle" class="evn">{n}</text>')
    return "".join(out) + "</svg>"


def bars(items, fmt, label, label_width=260, color="var(--loss)"):
    """Horizontal bars: items = [(label, value, color or None)]."""
    W, row = 980, 30
    top = max((v for _, v, _ in items), default=1) or 1
    out = [_open(W, row * len(items) + 10, label)]
    for i, (text, v, c) in enumerate(items):
        y0, w = 5 + i * row, (W - label_width - 120) * v / top
        out.append(f'<text x="{label_width - 10}" y="{y0 + row * 0.62:.1f}" text-anchor="end" class="lab">{escape(text)}</text>'
                   f'<rect x="{label_width}" y="{y0 + 5}" width="{max(1, w):.1f}" height="{row - 12}" rx="3" style="fill:{c or color}"/>'
                   f'<text x="{label_width + w + 8:.1f}" y="{y0 + row * 0.62:.1f}" class="val">{escape(fmt(v))}</text>')
    return "".join(out) + "</svg>"
