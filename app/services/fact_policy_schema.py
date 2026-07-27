"""Dynamic required fact-policy targets and deterministic materialization."""

from __future__ import annotations

from pydantic import Field, create_model

from app.contracts.base import StrictContract
from app.contracts.fact_blueprint_draft import (
    FactExpressionTarget,
    FactFilterTarget,
    JoinTarget,
    NoTarget,
)
from app.contracts.semantic_design import (
    ContractOnlyPolicyBinding,
    FactBlueprint,
    FactBlueprintItem,
    FactExpressionPolicyBinding,
    FactFilterPolicyBinding,
    FactJoinBlueprint,
    JoinPolicyBinding,
    ResultContract,
    ResultFilterPolicyBinding,
)


_TARGET_TYPE = {
    "source_population": FactFilterTarget,
    "calculation": FactExpressionTarget,
    "matching": JoinTarget,
}


def _slot_name(policy_key: str) -> str:
    return "policy_target_" + policy_key


def create_fact_blueprint_response_model(result: ResultContract):
    target_fields = {}
    for policy in result.business_policies:
        if policy.effect in _TARGET_TYPE:
            target_fields[_slot_name(policy.key)] = (
                list[_TARGET_TYPE[policy.effect]],
                Field(min_length=1),
            )
        else:
            target_fields[_slot_name(policy.key)] = (NoTarget, ...)
    targets_model = create_model(
        "RequiredFactPolicyTargets_" + result.content_hash[:12],
        __base__=StrictContract,
        **target_fields,
    )
    return create_model(
        "FactBlueprintDraft_" + result.content_hash[:12],
        __base__=StrictContract,
        facts=(list[FactBlueprintItem], Field(min_length=1)),
        joins=(list[FactJoinBlueprint], Field(default_factory=list)),
        derived_output_symbols=(list[str], Field(default_factory=list)),
        policy_targets=(targets_model, ...),
    )


def materialize_fact_blueprint(draft, result: ResultContract) -> FactBlueprint:
    bindings = []
    for policy in result.business_policies:
        target = getattr(draft.policy_targets, _slot_name(policy.key))
        if policy.effect == "source_population":
            bindings.extend(
                FactFilterPolicyBinding(
                    kind="fact_filter",
                    policy_key=policy.key,
                    fact_symbol=item.fact_symbol,
                )
                for item in target
            )
        elif policy.effect == "calculation":
            bindings.extend(
                FactExpressionPolicyBinding(
                    kind="fact_expression",
                    policy_key=policy.key,
                    fact_symbol=item.fact_symbol,
                    value_symbol=item.value_symbol,
                )
                for item in target
            )
        elif policy.effect == "matching":
            bindings.extend(
                JoinPolicyBinding(
                    kind="join",
                    policy_key=policy.key,
                    join_symbol=item.join_symbol,
                    match_mode=item.match_mode,
                )
                for item in target
            )
        elif policy.effect == "result_selection":
            bindings.append(ResultFilterPolicyBinding(
                kind="result_filter",
                policy_key=policy.key,
            ))
        else:
            bindings.append(ContractOnlyPolicyBinding(
                kind="contract_only",
                policy_key=policy.key,
            ))
    return FactBlueprint(
        facts=draft.facts,
        joins=draft.joins,
        derived_output_symbols=draft.derived_output_symbols,
        policy_bindings=bindings,
    )
