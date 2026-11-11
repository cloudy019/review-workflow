"""Human-in-the-loop literature review orchestration.

This package composes installed skills; it is deliberately not a Codex skill.
"""

from .skill_router import route_skills

__all__ = ["route_skills"]

