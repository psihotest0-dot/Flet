"""What the sources panel says about a source: the type label and the score text (P33).

Pure functions over the ``dict`` the backend sends for a source, no Flet. The panel
itself (``ChatMessage.open_sources_panel``) only lays the strings out.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# What the backend's ``source_type`` is called in the sources panel.
SOURCE_TYPE_LABELS = {
    "memory": "Памет",
    "concept": "Концепция",
    "text": "Текст",
    "ocr": "OCR",
}
# How the backend's ``score_type`` is called next to the score. ``distance`` is not
# here: it is shown as a distance, not as a percentage (see ``source_details``).
SCORE_TYPE_LABELS = {
    "cosine": "косинусово",
    "rrf": "RRF",
}
DISTANCE_SCORE_TYPE = "distance"
DEFAULT_SOURCE_LABEL = "Документ"


def source_type_label(source_type) -> str:
    """The panel's name of a ``source_type``; an unknown one is shown as it came."""
    key = str(source_type).strip() if source_type else ""
    if not key:
        return DEFAULT_SOURCE_LABEL
    return SOURCE_TYPE_LABELS.get(key.lower(), key)


def format_source_score(score, score_type=None) -> str:
    """Format a source's score for the panel.

    A ``distance`` is the cosine distance between the question and the source: a
    number of about 0.1-0.2 that is shown with three decimals, not as a percentage
    (with the embedding model in use even unrelated texts are at 0.15-0.30, which
    as ``(1 - distance) * 100`` looked like an 82-85 % match). The other types come
    on a 0-100 scale (older backends multiplied cosine scores by 100, RRF ones are
    normalised), so the number is shown as it is - this client used to guess the
    scale and turned a real 0.8 % into 80 %. A value that is not a number is shown
    as text.
    """
    kind = str(score_type).strip() if score_type else ""
    is_distance = kind.lower() == DISTANCE_SCORE_TYPE
    try:
        text = f"{float(score):.3f}" if is_distance else f"{float(score):.1f}%"
    except (ValueError, TypeError) as e:
        logger.warning("Failed to convert score '%s' to float: %s", score, e)
        text = str(score)
    if is_distance:
        return text
    if not kind:
        return text
    return f"{text} ({SCORE_TYPE_LABELS.get(kind.lower(), kind)})"


def source_details(src: dict) -> str:
    """The grey line under a source's title: type, page (if known) and score."""
    parts = [source_type_label(src.get("source_type") or src.get("type"))]
    page_number = src.get("page_number")
    if page_number not in (None, ""):
        parts.append(f"стр. {page_number}")
    score = src.get("score", "N/A")
    score_type = src.get("score_type")
    if str(score_type or "").strip().lower() == DISTANCE_SCORE_TYPE:
        parts.append(f"Разстояние: {format_source_score(score, score_type)} (по-малко = по-близо)")
    elif score_type or score != 0:
        parts.append(f"Сходство: {format_source_score(score, score_type)}")
    # else: a source without a measure (a recalled memory) shows no score at all
    return "• " + " • ".join(parts)
