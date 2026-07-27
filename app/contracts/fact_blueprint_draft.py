"""LLM-facing fact targets without writable policy ownership."""

from __future__ import annotations

from typing import Literal

from app.contracts.base import StrictContract
from app.contracts.semantic_design import Symbol


class FactFilterTarget(StrictContract):
    fact_symbol: Symbol


class FactExpressionTarget(StrictContract):
    fact_symbol: Symbol
    value_symbol: Symbol


class JoinTarget(StrictContract):
    join_symbol: Symbol
    match_mode: Literal[
        "matched_only", "left_preserved", "include_unmatched",
    ]


class NoTarget(StrictContract):
    pass
