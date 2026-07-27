"""Dynamic required source-filter slots and deterministic materialization."""

from __future__ import annotations

from pydantic import Field, create_model

from app.contracts.base import StrictContract
from app.contracts.semantic_design import (
    EntityRequirement,
    FactBlueprint,
    FilterRequirement,
    SourceFieldRequirement,
    SourceRequirements,
)
from app.contracts.semantic_obligations import SemanticObligationSet
from app.contracts.semantic_input_obligations import SemanticInputObligationSet
from app.contracts.source_requirements_draft import (
    OrdinaryFilterRequirement,
    PolicyFilterImplementation,
    SourceInputImplementation,
)


class SourceObligationError(ValueError):
    def __init__(self, code: str, message: str, *, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def create_source_requirements_response_model(
    obligation_set: SemanticObligationSet,
    input_obligations: SemanticInputObligationSet,
):
    filter_obligations = [
        item for item in obligation_set.obligations
        if item.kind == "fact_filter"
    ]
    policy_filters_model = create_model(
        "RequiredPolicyFilters_"
        + obligation_set.content_hash[:12],
        __base__=StrictContract,
        **{
            item.slot_name: (PolicyFilterImplementation, ...)
            for item in filter_obligations
        },
    )
    required_inputs_model = create_model(
        "RequiredSourceInputs_"
        + input_obligations.content_hash[:12],
        __base__=StrictContract,
        **{
            item.slot_name: (SourceInputImplementation, ...)
            for item in input_obligations.inputs
        },
    )
    return create_model(
        "SourceRequirementsDraft_"
        + obligation_set.content_hash[:6]
        + input_obligations.content_hash[:6],
        __base__=StrictContract,
        entities=(list[EntityRequirement], Field(min_length=1)),
        required_inputs=(required_inputs_model, ...),
        ordinary_filters=(
            list[OrdinaryFilterRequirement],
            Field(default_factory=list),
        ),
        policy_filters=(policy_filters_model, ...),
    )


def materialize_source_requirements(
    draft,
    obligation_set: SemanticObligationSet,
    input_obligations: SemanticInputObligationSet,
    facts: FactBlueprint,
) -> SourceRequirements:
    if input_obligations.result_contract_hash != (
        obligation_set.result_contract_hash
    ):
        raise SourceObligationError(
            "SOURCE_INPUT_OWNER_UNKNOWN",
            "来源输入义务与政策义务不属于同一结果合同",
        )
    input_payload = draft.required_inputs.model_dump(mode="python")
    expected_inputs = {
        item.slot_name: item for item in input_obligations.inputs
    }
    if set(input_payload) != set(expected_inputs):
        missing = sorted(set(expected_inputs) - set(input_payload))
        extra = sorted(set(input_payload) - set(expected_inputs))
        raise SourceObligationError(
            (
                "SOURCE_INPUT_IMPLEMENTATION_MISSING"
                if missing
                else "SOURCE_INPUT_IMPLEMENTATION_EXTRA"
            ),
            "来源输入实现与冻结义务不一致",
            evidence={"missing": missing, "extra": extra},
        )
    fields = []
    fact_entities = {
        item.symbol: set(item.entity_symbols) for item in facts.facts
    }
    for slot_name, obligation in expected_inputs.items():
        implementation = getattr(draft.required_inputs, slot_name)
        allowed_entities = fact_entities.get(obligation.fact_symbol)
        if (
            allowed_entities is None
            or implementation.entity_symbol not in allowed_entities
        ):
            raise SourceObligationError(
                "SOURCE_INPUT_OWNER_UNKNOWN",
                "来源输入被归属到冻结事实之外的业务实体",
                evidence={
                    "slot": slot_name,
                    "fact": obligation.fact_symbol,
                    "actual_entity": implementation.entity_symbol,
                    "allowed_entities": sorted(allowed_entities or []),
                },
            )
        if implementation.nullable != obligation.nullable:
            raise SourceObligationError(
                "COMPUTATION_INPUT_TYPE_MISMATCH",
                "来源输入的可空性不能修改冻结的计算输入合同",
                evidence={
                    "slot": slot_name,
                    "expected_nullable": obligation.nullable,
                    "actual_nullable": implementation.nullable,
                },
            )
        fields.append(SourceFieldRequirement(
            symbol=slot_name,
            entity_symbol=implementation.entity_symbol,
            meaning=implementation.meaning,
            logical_type=obligation.logical_type,
            nullable=obligation.nullable,
        ))
    policy_payload = draft.policy_filters.model_dump(mode="python")
    filters = [
        FilterRequirement(
            symbol=item.symbol,
            meaning=item.meaning,
            source_symbol=item.source_symbol,
            parameter_symbols=item.parameter_symbols,
            operator=item.operator,
            literal_values=item.literal_values,
            policy_key=None,
            fact_symbols=item.fact_symbols,
            skip_when_parameter_null=item.skip_when_parameter_null,
        )
        for item in draft.ordinary_filters
    ]
    expected_slots = {
        item.slot_name: item
        for item in obligation_set.obligations
        if item.kind == "fact_filter"
    }
    if set(policy_payload) != set(expected_slots):
        raise SourceObligationError(
            "OBLIGATION_IMPLEMENTATION_MISSING",
            "来源政策义务实现不完整",
            evidence={
                "expected": sorted(expected_slots),
                "actual": sorted(policy_payload),
            },
        )
    for slot_name, obligation in expected_slots.items():
        implementation = getattr(draft.policy_filters, slot_name)
        allowed_entities = fact_entities.get(obligation.fact_symbol)
        if (
            allowed_entities is None
            or implementation.entity_symbol not in allowed_entities
        ):
            raise SourceObligationError(
                "SOURCE_INPUT_OWNER_UNKNOWN",
                "政策过滤输入被归属到目标事实之外的业务实体",
                evidence={
                    "slot": slot_name,
                    "fact": obligation.fact_symbol,
                    "actual_entity": implementation.entity_symbol,
                    "allowed_entities": sorted(allowed_entities or []),
                },
            )
        source_symbol = "input_" + slot_name
        fields.append(SourceFieldRequirement(
            symbol=source_symbol,
            entity_symbol=implementation.entity_symbol,
            meaning=implementation.source_meaning,
            logical_type=implementation.source_logical_type,
            nullable=implementation.nullable,
        ))
        filters.append(FilterRequirement(
            symbol=slot_name,
            meaning=implementation.meaning,
            source_symbol=source_symbol,
            parameter_symbols=implementation.parameter_symbols,
            operator=implementation.operator,
            literal_values=implementation.literal_values,
            policy_key=obligation.policy_key,
            fact_symbols=[obligation.fact_symbol],
            skip_when_parameter_null=(
                implementation.skip_when_parameter_null
            ),
        ))
    return SourceRequirements(
        entities=draft.entities,
        fields=fields,
        filters=filters,
    )
