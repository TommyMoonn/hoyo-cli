"""Semantic terminal styles and heading-only game accents."""

from rich.theme import Theme

STYLES = {
    "text": "",
    "muted": "dim",
    "success": "green",
    "warning": "yellow",
    "attention": "bold yellow",
    "complete": "dim",
    "error": "red",
    "stale": "bold yellow",
    "heading": "bold",
    "game.genshin": "bold #D3BC8E",
    "game.hsr": "bold #B7A7FF",
    "game.zzz": "bold #F2D13D",
}
THEME = Theme(STYLES)
GAME_HEADINGS = {
    "genshin": "GENSHIN IMPACT",
    "hsr": "HONKAI: STAR RAIL",
    "zzz": "ZENLESS ZONE ZERO",
}
