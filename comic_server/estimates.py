"""Reviewable estimates, not an account balance or a provider-enforced charge cap."""
import hashlib
import json
from decimal import Decimal, ROUND_CEILING

PRICING_DATE = "2026-10-15"
IMAGE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-image-1"
TEXT_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1-mini"


def revision(plan):
    return hashlib.sha256(json.dumps(json.loads(plan), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quote(request, board, provider, completed=0):
    if getattr(provider, "image_model", "gpt-image-1") != "gpt-image-1" or getattr(provider, "text_model", "gpt-4.1-mini") != "gpt-4.1-mini":
        return {"available": False, "reason": "Pricing for the configured models must be reviewed before approval.", "currency": "USD"}
    photos = sum(1 for r in request.attachments if r.useForAi and r.role in {"CHARACTER", "LOCATION", "STYLE"})
    total = sum(len(p.panels) for p in board.pages)
    remaining = max(0, total - completed)
    # Exact sizes are selected from panel aspect ratios. Use all-square to all-portrait
    # output bounds; inputs, reference tokens and planning are separately disclosed.
    output_low = Decimal(".167") * remaining
    output_high = Decimal(".250") * remaining
    # Input usage is variable. This allowance is deliberately a range, never an invoice.
    reference_low = Decimal(".01") * photos * remaining
    reference_high = Decimal(".20") * photos * remaining
    prompt_allowance = Decimal(".02") * remaining
    def amount(n):
        return str(n.quantize(Decimal(".01"), rounding=ROUND_CEILING))
    return {"available": True, "currency": "USD", "pricingDate": PRICING_DATE,
        "imageModel": provider.image_model, "textModel": provider.text_model, "quality": "high",
        "totalImages": total, "remainingImages": remaining, "referenceImages": photos,
        "imageOutputLow": amount(output_low), "imageOutputHigh": amount(output_high),
        "referenceAllowanceLow": amount(reference_low), "referenceAllowanceHigh": amount(reference_high),
        "promptAndVisionAllowance": amount(prompt_allowance),
        "estimatedLow": amount(output_low + reference_low),
        "estimatedHigh": amount(output_high + reference_high + prompt_allowance),
        "planningAlreadyRequested": True,
        "notice": "Estimate for remaining artwork, not an invoice or guaranteed cap. Planning was a separate paid text request. Actual input/vision usage, retries and billing can vary. No automatic paid retries.",
        "sources": [IMAGE_SOURCE, TEXT_SOURCE]}
