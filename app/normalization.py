from __future__ import annotations

import re


def editable_expression(value: str) -> str:
    """Keep LaTeX structure while removing wrappers that hinder editing/graphing."""
    text = str(value or "").strip()
    text = re.sub(r"^\s*\$+|\$+\s*$", "", text)
    text = text.replace("\\(", "").replace("\\)", "")
    text = text.replace("\\[", "").replace("\\]", "")
    text = text.replace("\\left", "").replace("\\right", "")
    text = re.sub(r"\\(?:displaystyle|textstyle|scriptstyle)\s*", "", text)
    text = re.sub(r"^\s*(?:f\s*\(\s*x\s*\)|y)\s*[=＝]\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text
