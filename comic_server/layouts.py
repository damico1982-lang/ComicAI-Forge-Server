"""Normalized, non-overlapping page layouts; matching templates live in Android."""


def panel_aspect_ratio(box):
    return (595 / 842) * (box["xMax"] - box["xMin"]) / (box["yMax"] - box["yMin"])


def image_size(aspect_ratio):
    """One source of truth for the provider request and its output-price quote."""
    return "1536x1024" if aspect_ratio >= 1.2 else "1024x1536" if aspect_ratio <= 0.85 else "1024x1024"


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
