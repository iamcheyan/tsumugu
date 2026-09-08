"""Reusable media-processing primitives shared by Web and automation clients."""

from .naming import sanitize_component
from .policy import AutomationPolicy, choose_split_policy

__all__ = ["AutomationPolicy", "choose_split_policy", "sanitize_component"]
