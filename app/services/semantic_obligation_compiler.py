"""Deterministically compile business policies into immutable obligations."""

from __future__ import annotations

import hashlib
import json

from app.contracts.semantic_design import FactBlueprint, ResultContract
from app.contracts.semantic_obligations import (
    SemanticObligationSet,
    SemanticPolicyObligation,
)


class SemanticObligationError(ValueError):
    def __init__(self, code: str, message: str, *, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


_EFFECT_KIND = {
    "source_population": "fact_filter",
    "calculation": "fact_expression",
    "matching": "join",
    "result_selection": "result_filter",
    "presentation": "contract_only",
}


def _target_payload(binding) -> dict:
    result = {}
    for field in ("fact_symbol", "value_symbol", "join_symbol", "match_mode"):
        value = getattr(binding, field, None)
        if value is not None:
            result[field] = value
    return result


def _slot_name(binding) -> str:
    parts = ["policy", binding.kind]
    for field in ("fact_symbol", "value_symbol", "join_symbol"):
        value = getattr(binding, field, None)
        if value:
            parts.append(value)
    parts.append(binding.policy_key)
    return "_".join(parts)


def compile_semantic_obligations(
    result: ResultContract,
    blueprint: FactBlueprint,
) -> SemanticObligationSet:
    policies = {item.key.casefold(): item for item in result.business_policies}
    bindings_by_policy: dict[str, list] = {}
    for binding in blueprint.policy_bindings:
        key = binding.policy_key.casefold()
        policy = policies.get(key)
        if policy is None:
            raise SemanticObligationError(
                "POLICY_UNKNOWN",
                f"政策绑定引用未知政策 {binding.policy_key}",
                evidence={
                    "policy_key": binding.policy_key,
                    "actual_kind": binding.kind,
                    "target": _target_payload(binding),
                },
            )
        expected = _EFFECT_KIND[policy.effect]
        if binding.kind != expected:
            raise SemanticObligationError(
                "POLICY_EFFECT_BINDING_MISMATCH",
                f"政策 {policy.key} 的 effect={policy.effect} "
                f"不能绑定为 {binding.kind}",
                evidence={
                    "policy_key": policy.key,
                    "policy_value": policy.value,
                    "effect": policy.effect,
                    "expected_kind": expected,
                    "actual_kind": binding.kind,
                    "target": _target_payload(binding),
                },
            )
        if (
            binding.kind == "result_filter"
            and result.result_mode != "exception_rows"
        ):
            raise SemanticObligationError(
                "POLICY_RESULT_MODE_MISMATCH",
                f"政策 {policy.key} 要求筛选最终结果，但结果模式不是 exception_rows",
                evidence={
                    "policy_key": policy.key,
                    "policy_value": policy.value,
                    "effect": policy.effect,
                    "obligation_kind": binding.kind,
                    "result_mode": result.result_mode,
                },
            )
        bindings_by_policy.setdefault(key, []).append(binding)

    missing = sorted(set(policies) - set(bindings_by_policy))
    if missing:
        raise SemanticObligationError(
            "POLICY_BINDING_MISSING",
            "业务政策没有实现绑定",
            evidence={
                "missing": missing,
                "policies": [
                    {
                        "policy_key": policies[key].key,
                        "policy_value": policies[key].value,
                        "effect": policies[key].effect,
                    }
                    for key in missing
                ],
            },
        )

    obligations = []
    for key in sorted(bindings_by_policy):
        for binding in sorted(
            bindings_by_policy[key],
            key=lambda item: json.dumps(
                item.model_dump(mode="json"), sort_keys=True,
            ),
        ):
            target = _target_payload(binding)
            seed = {
                "result_contract_hash": result.content_hash,
                "fact_blueprint_hash": blueprint.content_hash,
                "policy_key": binding.policy_key,
                "kind": binding.kind,
                **target,
            }
            obligation_id = hashlib.sha256(json.dumps(
                seed,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            obligations.append(SemanticPolicyObligation(
                obligation_id=obligation_id,
                slot_name=_slot_name(binding),
                policy_key=binding.policy_key,
                kind=binding.kind,
                **target,
            ))
    return SemanticObligationSet(
        result_contract_hash=result.content_hash,
        fact_blueprint_hash=blueprint.content_hash,
        obligations=obligations,
    )
