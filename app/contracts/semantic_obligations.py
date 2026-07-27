"""Frozen policy obligations compiled from result and fact contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract


ObligationKind = Literal[
    "fact_filter",
    "fact_expression",
    "join",
    "result_filter",
    "contract_only",
]


class SemanticPolicyObligation(StrictContract):
    obligation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    slot_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    policy_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: ObligationKind
    fact_symbol: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )
    value_symbol: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )
    join_symbol: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )
    match_mode: Literal[
        "matched_only", "left_preserved", "include_unmatched",
    ] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        required = {
            "fact_filter": ("fact_symbol",),
            "fact_expression": ("fact_symbol", "value_symbol"),
            "join": ("join_symbol", "match_mode"),
            "result_filter": (),
            "contract_only": (),
        }[self.kind]
        if any(getattr(self, item) is None for item in required):
            raise ValueError("POLICY_BINDING_TARGET_UNKNOWN")
        allowed = set(required)
        for field in ("fact_symbol", "value_symbol", "join_symbol", "match_mode"):
            if field not in allowed and getattr(self, field) is not None:
                raise ValueError("POLICY_BINDING_TARGET_CHANGED")
        return self


class SemanticObligationSet(StrictContract):
    version: Literal[1] = 1
    result_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_blueprint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    obligations: list[SemanticPolicyObligation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique(self):
        ids = [item.obligation_id for item in self.obligations]
        slots = [item.slot_name.casefold() for item in self.obligations]
        if len(ids) != len(set(ids)) or len(slots) != len(set(slots)):
            raise ValueError("POLICY_BINDING_DUPLICATE")
        return self
