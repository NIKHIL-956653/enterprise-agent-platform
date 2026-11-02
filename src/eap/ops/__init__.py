"""Operational aggregates over a tenant's runs and feedback (EAP-65). See eap.ops.summary."""

from eap.ops.summary import MAX_DAYS, DayCount, FeedbackCounts, OpsSummary, summarize, window_since

__all__ = ["MAX_DAYS", "DayCount", "FeedbackCounts", "OpsSummary", "summarize", "window_since"]
