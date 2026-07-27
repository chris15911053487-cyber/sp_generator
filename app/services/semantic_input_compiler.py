"""Compile frozen computation inputs into deterministic source obligations."""

from __future__ import annotations

import hashlib

from app.contracts.computation_blueprint import ComputationBlueprint
from app.contracts.semantic_design import FactBlueprint, ResultContract
from app.contracts.semantic_input_obligations import (
    SemanticInputObligation,
    SemanticInputObligationSet,
)
from app.services.computation_blueprint_validator import (
    validate_computation_blueprint,
)


class SemanticInputCompilerError(ValueError):
    def __init__(self, code: str, message: str, *, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _usage_count(expression, symbol: str) -> int:
    count = int(expression.kind == "input" and expression.symbol == symbol)
    if expression.kind in {"binary", "unary", "function"}:
        count += sum(_usage_count(arg, symbol) for arg in expression.args)
    elif expression.kind == "case":
        count += sum(
            _usage_count(branch.when, symbol)
            + _usage_count(branch.then, symbol)
            for branch in expression.cases
        )
        if expression.else_expr is not None:
            count += _usage_count(expression.else_expr, symbol)
    return count


def _obligation_id(
    result_hash: str,
    fact_hash: str,
    computation_hash: str,
    fact_symbol: str,
    input_symbol: str,
) -> str:
    payload = "|".join((
        result_hash,
        fact_hash,
        computation_hash,
        fact_symbol,
        input_symbol,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_semantic_input_obligations(
    result: ResultContract,
    facts: FactBlueprint,
    computations: ComputationBlueprint,
) -> SemanticInputObligationSet:
    validate_computation_blueprint(result, facts, computations)
    grouped: dict[tuple[str, str], dict] = {}
    for computation in computations.fact_values:
        for input_spec in computation.inputs:
            key = (computation.fact_symbol, input_spec.symbol)
            usage_count = _usage_count(computation.expression, input_spec.symbol)
            if usage_count < 1:
                raise SemanticInputCompilerError(
                    "COMPUTATION_INPUT_UNUSED",
                    "计算输入没有被公式使用",
                    evidence={
                        "fact": computation.fact_symbol,
                        "value": computation.value_symbol,
                        "input": input_spec.symbol,
                    },
                )
            signature = (
                input_spec.meaning,
                input_spec.logical_type,
                input_spec.nullable,
            )
            entry = grouped.setdefault(key, {
                "signature": signature,
                "value_symbols": [],
                "usage_paths": [],
            })
            if entry["signature"] != signature:
                raise SemanticInputCompilerError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "同一事实内复用的计算输入定义不一致",
                    evidence={
                        "fact": computation.fact_symbol,
                        "input": input_spec.symbol,
                        "expected": entry["signature"],
                        "actual": signature,
                    },
                )
            entry["value_symbols"].append(computation.value_symbol)
            entry["usage_paths"].extend(
                f"fact_values.{computation.fact_symbol}."
                f"{computation.value_symbol}.expression#{index + 1}"
                for index in range(usage_count)
            )

    inputs = []
    for (fact_symbol, input_symbol), entry in sorted(grouped.items()):
        meaning, logical_type, nullable = entry["signature"]
        inputs.append(SemanticInputObligation(
            obligation_id=_obligation_id(
                result.content_hash,
                facts.content_hash,
                computations.content_hash,
                fact_symbol,
                input_symbol,
            ),
            slot_name=f"input_{fact_symbol}_{input_symbol}",
            fact_symbol=fact_symbol,
            value_symbols=sorted(set(entry["value_symbols"])),
            input_symbol=input_symbol,
            meaning=meaning,
            logical_type=logical_type,
            nullable=nullable,
            usage_paths=entry["usage_paths"],
        ))
    return SemanticInputObligationSet(
        result_contract_hash=result.content_hash,
        fact_blueprint_hash=facts.content_hash,
        computation_blueprint_hash=computations.content_hash,
        inputs=inputs,
    )
