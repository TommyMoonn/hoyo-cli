"""Explicit terminal breakpoints shared by all game views."""

from typing import Literal

Layout = Literal["wide", "medium", "narrow"]


def choose_layout(width: int) -> Layout:
    if width >= 80:
        return "wide"
    if width >= 60:
        return "medium"
    return "narrow"
