from __future__ import annotations

from .persona_loader import load_active_persona


def load_persona() -> str:
    """Compatibility helper for older scripts."""
    return load_active_persona().prompt
