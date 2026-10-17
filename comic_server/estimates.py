"""Itemized provider-rate calculations; variable input charges are not an invoice."""
import hashlib
import json
from collections import Counter
from decimal import Decimal, ROUND_CEILING

from .layouts import panel_layout, panel_aspect_ratio, image_size

PRICING_DATE = "2026-10-17"
IMAGE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-image-1"
TEXT_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1-mini"
OUTPUT_RATES = {"1024x1024": Decimal(".167"), "1024x1536": Decimal(".250"), "1536x1024": Decimal(".250")}


def revision(plan):
    return hashlib.sha256(json.dumps(json.loads(plan), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def amount(value, places=".01"):
    return str(value.quantize(Decimal(places), rounding=ROUND_CEILING))


def quote(request, board, provider, completed=0, reference_images=None):
    if getattr(provider, "image_model", "gpt-image-1") != "gpt-image-1" or getattr(provider, "text_model", "gpt-4.1-mini") != "gpt-4.1-mini":
        return {"available": False, "reason": "Pricing for the configured models must be reviewed before approval.", "currency": "USD"}
    # Production supplies upload kinds: every selected image is sent to image edits,
    # including images labeled DOCUMENT; text references are not image inputs.
    photos = reference_images if reference_images is not None else sum(r.useForAi for r in request.attachments)
    sizes = [image_size(panel_aspect_ratio(box)) for page in board.pages
             for box in panel_layout(len(page.panels), request.layoutStyle)]
    total = len(sizes)
    completed = max(0, min(total, completed))
    counts = Counter(sizes[completed:])
    remaining = total - completed
    output = sum((OUTPUT_RATES[size] * count for size, count in counts.items()), Decimal(0))
    # These remain explicitly variable allowances, not measured input usage.
    reference_low = Decimal(".01") * photos * remaining
    reference_high = Decimal(".20") * photos * remaining
    prompt_allowance = Decimal(".02") * remaining
    output_rounded = Decimal(amount(output))
    low = output_rounded + reference_low
    high = output_rounded + reference_high + prompt_allowance
    lines = [{"id": size, "label": f"{size} high-quality image output", "quantity": count,
              "unitPrice": amount(OUTPUT_RATES[size], ".001"), "subtotal": amount(OUTPUT_RATES[size] * count, ".001")}
             for size, count in sorted(counts.items())]
    return {"available": True, "currency": "USD", "pricingDate": PRICING_DATE,
        "imageModel": provider.image_model, "textModel": provider.text_model, "quality": "high",
        "totalImages": total, "remainingImages": remaining, "completedImages": completed, "referenceImages": photos,
        "outputLineItems": lines, "imageOutputSubtotal": amount(output, ".001"),
        "imageOutputLow": amount(output), "imageOutputHigh": amount(output),
        "roundingAdjustment": amount(output_rounded - output, ".001"),
        "referenceAllowanceLow": amount(reference_low), "referenceAllowanceHigh": amount(reference_high),
        "promptAndVisionAllowance": amount(prompt_allowance),
        "estimatedLow": amount(low), "estimatedHigh": amount(high),
        "approvalAmount": amount(high), "totalIsExact": False,
        "planningAlreadyRequested": True,
        "planningNotice": "Planning was requested separately before this review. Its charge is not included and is not measured here.",
        "notice": "Image-output subtotal is calculated from published rates and the planned image sizes. Total is an estimate, not a fixed price, invoice or guaranteed cap. Reference and prompt inputs are variable. Final charges come from provider billing; retries may cost extra. No automatic paid retries.",
        "sources": [IMAGE_SOURCE, TEXT_SOURCE]}
