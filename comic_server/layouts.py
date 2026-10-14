"""Normalized, non-overlapping page layouts; matching templates live in Android."""


def panel_layout(count, style="GRID"):
    if count not in range(3, 7) or style not in {"GRID", "CINEMATIC"}:
        raise ValueError("Choose 3–6 panels and GRID or CINEMATIC")
    rows = ({3: [1, 2], 4: [1, 2, 1], 5: [1, 2, 2], 6: [1, 2, 3]}[count]
            if style == "CINEMATIC" else [2] * (count // 2) + ([1] if count % 2 else []))
    gap, margin = 0.04, 0.04
    height = (1 - 2 * margin - gap * (len(rows) - 1)) / len(rows)
    bounds = []
    for row, columns in enumerate(rows):
        width = (1 - 2 * margin - gap * (columns - 1)) / columns
        for column in range(columns):
            left = margin + column * (width + gap)
            top = margin + row * (height + gap)
            bounds.append(dict(xMin=left, yMin=top, xMax=left + width, yMax=top + height))
    return bounds
