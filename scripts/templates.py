"""Template loader — reads .md files from templates/ directory."""

from __future__ import annotations

from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def get_template(name: str, **kwargs: object) -> str:
    """Load a template by name from templates/{name}.md.

    Args:
        name: Template name (without .md extension)
        **kwargs: Placeholders to fill in the template

    Returns:
        Template text with placeholders replaced
    """
    path = _TEMPLATES_DIR / f"{name}.md"
    content = path.read_text(encoding="utf-8").strip()
    if kwargs:
        content = content.format(**kwargs)
    return content
