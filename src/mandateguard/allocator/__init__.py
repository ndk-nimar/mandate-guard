"""allocator layer -- Policy interface and the six arms. See docs/architecture.md."""

from mandateguard.allocator.base import NoAskPolicy, Policy

__all__ = ["NoAskPolicy", "Policy"]
