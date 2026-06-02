import math


def estimate_credits(tool: dict, form: dict | None = None) -> int:
    form = form or {}
    pricing = tool["pricing"]
    priority = form.get("priority") or "standard"
    priority_multiplier = pricing.get("priorityMultiplier", {}).get(priority, 1)

    if pricing["mode"] == "image":
        count = int(form.get("imageCount") or 1)
        estimate = count * pricing["unitCredits"] * priority_multiplier
        return max(pricing["minimumCredits"], math.ceil(estimate))

    seconds = int(form.get("duration") or 30)
    units = math.ceil(seconds / pricing["unitSeconds"])
    resolution = form.get("resolution") or "1080p"
    resolution_multiplier = pricing.get("resolutionMultiplier", {}).get(resolution, 1)
    complexity = 1.25 if int(form.get("watermarkCount") or 1) > 1 or form.get("maskComplexity") == "complex" else 1
    estimate = units * pricing["unitCredits"] * resolution_multiplier * priority_multiplier * complexity
    return max(pricing["minimumCredits"], math.ceil(estimate))
