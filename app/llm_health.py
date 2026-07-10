from __future__ import annotations

from typing import Any


def estimate_tokens_from_chars(value: int) -> int:
    return max(0, round(max(0, int(value or 0)) / 4))


def llm_cost_pressure(item: dict[str, Any]) -> str:
    if int(item.get("avg_prompt_chars") or 0) >= 8000 or int(item.get("estimated_total_tokens") or 0) >= 5000:
        return "high_context"
    if int(item.get("avg_prompt_chars") or 0) >= 3000 or int(item.get("estimated_total_tokens") or 0) >= 2000:
        return "watch"
    return "normal"


def llm_route_hint(item: dict[str, Any]) -> str:
    if item.get("current_failed"):
        return "current_route_failing"
    if item.get("stale_config_failure"):
        return "old_route_failure"
    if item.get("cost_pressure") == "high_context":
        return "review_context_size"
    if int(item.get("slow") or 0) > 0:
        return "review_latency"
    return "ok"


def llm_route_recommendation(item: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    action = "keep_current"
    severity = "ok"
    explanation = "Recent local logs do not show route, latency, or context pressure."

    if item.get("current_failed"):
        action = "check_current_config"
        severity = "blocked"
        reasons.append("latest_call_failed_on_current_route")
        if item.get("last_error"):
            reasons.append("has_error_summary")
        explanation = "The latest call failed on the currently configured provider/model; check key, base URL, model, timeout, or provider status before more live use."
    elif item.get("stale_config_failure"):
        action = "ignore_stale_failure"
        severity = "info"
        reasons.append("failure_from_old_provider_or_model")
        explanation = "The latest failure belongs to a provider/model that no longer matches the current route; keep it visible as history, not as a current outage."
    elif item.get("cost_pressure") == "high_context":
        action = "review_context_size"
        severity = "watch"
        reasons.append("high_context_or_token_estimate")
        explanation = "Prompt/context size is high; trim retrieval, summaries, or injected state before considering a larger or pricier model."
    elif item.get("cost_pressure") == "watch":
        action = "consider_cheaper_route"
        severity = "watch"
        reasons.append("growing_context_or_token_estimate")
        explanation = "The route is working but token estimates are growing; consider cheaper routing or more aggressive context compaction for this task."
    elif int(item.get("slow") or 0) > 0:
        action = "review_latency"
        severity = "watch"
        reasons.append("slow_calls_observed")
        explanation = "At least one recent call exceeded the slow-call threshold; review timeout, model latency, and fallback behavior."
    elif item.get("historical_failed"):
        action = "monitor_recovered_route"
        severity = "info"
        reasons.append("historical_failures_recovered")
        explanation = "Failures exist in the window, but the latest call recovered; keep monitoring before changing routes."

    return {
        "action": action,
        "severity": severity,
        "reasons": reasons,
        "explanation": explanation,
    }


def annotate_llm_health_item(item: dict[str, Any]) -> dict[str, Any]:
    item["cost_pressure"] = llm_cost_pressure(item)
    item["route_hint"] = llm_route_hint(item)
    recommendation = llm_route_recommendation(item)
    item["route_recommendation"] = recommendation
    item["budget_action"] = recommendation["action"]
    item["budget_severity"] = recommendation["severity"]
    item["budget_reasons"] = recommendation["reasons"]
    item["budget_explanation"] = recommendation["explanation"]
    return item
