import hashlib

import pytest
from pydantic import ValidationError

from app.contracts.computation_blueprint import (
    ComputationBlueprint,
    FactValueComputation,
    ResultValueComputation,
)
from app.contracts.semantic_design import (
    BusinessPolicySpec,
    EntityRequirement,
    ExpressionDesign,
    FactBlueprint,
    FactBlueprintItem,
    FactDimensionExpression,
    FactDimensionNeed,
    FactJoinBlueprint,
    FactMeasureExpression,
    FactMeasureNeed,
    FactExpressionPolicyBinding,
    ResultBindingExpression,
    ResultContract,
    ResultOutputSpec,
    SourceFieldRequirement,
    SourceRequirements,
    make_symbol_expression as SymbolExpression,
)
from app.services.semantic_obligation_compiler import compile_semantic_obligations
from app.contracts.semantic_input_obligations import (
    SemanticInputObligation,
    SemanticInputObligationSet,
)
from app.services.semantic_compiler_v3 import (
    SemanticCompileError,
    compile_semantic_contract,
)


def staged_reconciliation():
    result = ResultContract(
        procedure_name="usp_ReconcileRevenue",
        purpose="按月份对比销售收入与凭证收入",
        result_mode="full_rows",
        outputs=[
            ResultOutputSpec(
                symbol="period",
                name="Period",
                meaning="对账年月",
                logical_type="string",
                nullable=False,
            ),
            ResultOutputSpec(
                symbol="sales_amount",
                name="SalesAmount",
                meaning="销售收入金额",
                logical_type="money",
                nullable=False,
            ),
            ResultOutputSpec(
                symbol="journal_amount",
                name="JournalAmount",
                meaning="凭证收入金额",
                logical_type="money",
                nullable=False,
            ),
            ResultOutputSpec(
                symbol="difference",
                name="Difference",
                meaning="销售与凭证收入差额",
                logical_type="money",
                nullable=False,
            ),
        ],
        grain_output_symbols=["period"],
        business_policies=[
            BusinessPolicySpec(
                key="currency_basis",
                value="本位币",
                effect="calculation",
                meaning="销售收入和凭证收入统一按本位币计算",
            ),
        ],
    )
    blueprint = FactBlueprint(
        facts=[
            FactBlueprintItem(
                symbol="sales",
                meaning="销售收入事实",
                entity_symbols=["sales_invoice"],
                dimensions=[
                    FactDimensionNeed(
                        symbol="period",
                        meaning="销售收入年月",
                        logical_type="string",
                        result_output_symbol=None,
                    )
                ],
                measures=[
                    FactMeasureNeed(
                        symbol="amount",
                        meaning="销售收入合计",
                        logical_type="money",
                        aggregation="sum",
                        result_output_symbol="sales_amount",
                    )
                ],
                grain_dimension_symbols=["period"],
            ),
            FactBlueprintItem(
                symbol="journal",
                meaning="凭证收入事实",
                entity_symbols=["journal_line"],
                dimensions=[
                    FactDimensionNeed(
                        symbol="period",
                        meaning="凭证收入年月",
                        logical_type="string",
                        result_output_symbol=None,
                    )
                ],
                measures=[
                    FactMeasureNeed(
                        symbol="amount",
                        meaning="凭证收入合计",
                        logical_type="money",
                        aggregation="sum",
                        result_output_symbol="journal_amount",
                    )
                ],
                grain_dimension_symbols=["period"],
            ),
        ],
        joins=[
            FactJoinBlueprint(
                symbol="match_period",
                left_fact_symbol="sales",
                right_fact_symbol="journal",
                left_dimension_symbol="period",
                right_dimension_symbol="period",
                join_type="full",
                meaning="按年月匹配销售与凭证",
            )
        ],
        derived_output_symbols=["period", "difference"],
    )
    blueprint = blueprint.model_copy(update={
        "policy_bindings": [
            FactExpressionPolicyBinding(
                kind="fact_expression",
                policy_key="currency_basis",
                fact_symbol="sales",
                value_symbol="amount",
            ),
            FactExpressionPolicyBinding(
                kind="fact_expression",
                policy_key="currency_basis",
                fact_symbol="journal",
                value_symbol="amount",
            ),
        ],
    })
    blueprint = FactBlueprint.model_validate(
        blueprint.model_dump(mode="json"),
    )
    sources = SourceRequirements(
        entities=[
            EntityRequirement(
                symbol="sales_invoice",
                meaning="销售发票",
                grain_meaning="一张销售发票",
            ),
            EntityRequirement(
                symbol="journal_line",
                meaning="凭证明细",
                grain_meaning="一条凭证分录",
            ),
        ],
        fields=[
            SourceFieldRequirement(
                symbol="sales_date",
                entity_symbol="sales_invoice",
                meaning="销售发票日期",
                logical_type="date",
                nullable=False,
            ),
            SourceFieldRequirement(
                symbol="sales_value",
                entity_symbol="sales_invoice",
                meaning="销售收入金额",
                logical_type="money",
                nullable=False,
            ),
            SourceFieldRequirement(
                symbol="journal_date",
                entity_symbol="journal_line",
                meaning="凭证日期",
                logical_type="date",
                nullable=False,
            ),
            SourceFieldRequirement(
                symbol="journal_value",
                entity_symbol="journal_line",
                meaning="凭证收入金额",
                logical_type="money",
                nullable=False,
            ),
        ],
    )

    def period(source):
        return SymbolExpression(
            kind="function",
            operator="CONCAT",
            args=[
                SymbolExpression(
                    kind="function",
                    operator="YEAR",
                    args=[SymbolExpression(kind="source", symbol=source)],
                ),
                SymbolExpression(kind="literal", value="-"),
                SymbolExpression(
                    kind="function",
                    operator="MONTH",
                    args=[SymbolExpression(kind="source", symbol=source)],
                ),
            ],
        )

    def fact_value(fact, value):
        return SymbolExpression(
            kind="fact_value",
            fact_symbol=fact,
            value_symbol=value,
        )

    expressions = ExpressionDesign(
        dimensions=[
            FactDimensionExpression(
                fact_symbol="sales",
                dimension_symbol="period",
                expression=period("sales_date"),
                logical_type="string",
            ),
            FactDimensionExpression(
                fact_symbol="journal",
                dimension_symbol="period",
                expression=period("journal_date"),
                logical_type="string",
            ),
        ],
        measures=[
            FactMeasureExpression(
                fact_symbol="sales",
                measure_symbol="amount",
                expression=SymbolExpression(
                    kind="source", symbol="sales_value",
                ),
                aggregation="sum",
                logical_type="money",
            ),
            FactMeasureExpression(
                fact_symbol="journal",
                measure_symbol="amount",
                expression=SymbolExpression(
                    kind="source", symbol="journal_value",
                ),
                aggregation="sum",
                logical_type="money",
            ),
        ],
        results=[
            ResultBindingExpression(
                output_symbol="period",
                expression=SymbolExpression(
                    kind="function",
                    operator="COALESCE",
                    args=[
                        fact_value("sales", "period"),
                        fact_value("journal", "period"),
                    ],
                ),
            ),
            ResultBindingExpression(
                output_symbol="sales_amount",
                expression=fact_value("sales", "amount"),
            ),
            ResultBindingExpression(
                output_symbol="journal_amount",
                expression=fact_value("journal", "amount"),
            ),
            ResultBindingExpression(
                output_symbol="difference",
                expression=SymbolExpression(
                    kind="binary",
                    operator="-",
                    args=[
                        SymbolExpression(
                            kind="output", symbol="sales_amount",
                        ),
                        SymbolExpression(
                            kind="output", symbol="journal_amount",
                        ),
                    ],
                ),
            ),
        ],
    )
    return (
        result,
        blueprint,
        sources,
        expressions,
        compile_semantic_obligations(result, blueprint),
    )


def staged_computation_artifacts(staged):
    """Build frozen artifacts for compiler tests from the canonical fixture."""
    result, blueprint, sources, expressions, _ = staged
    source_by_symbol = {item.symbol: item for item in sources.fields}

    def convert(expression, *, fact_context):
        payload = expression.model_dump(mode="json")
        if fact_context and payload["kind"] == "source":
            return {"kind": "input", "symbol": payload["symbol"]}
        if payload["kind"] in {"binary", "unary", "function"}:
            payload["args"] = [
                convert(arg, fact_context=fact_context)
                for arg in expression.args
            ]
        elif payload["kind"] == "case":
            payload["cases"] = [
                {
                    "when": convert(
                        branch.when,
                        fact_context=fact_context,
                    ),
                    "then": convert(
                        branch.then,
                        fact_context=fact_context,
                    ),
                }
                for branch in expression.cases
            ]
            payload["else_expr"] = (
                convert(
                    expression.else_expr,
                    fact_context=fact_context,
                )
                if expression.else_expr is not None else None
            )
        return payload

    def source_symbols(expression):
        if expression is None:
            return set()
        found = (
            {expression.symbol}
            if expression.kind == "source"
            else set()
        )
        if expression.kind in {"binary", "unary", "function"}:
            for arg in expression.args:
                found.update(source_symbols(arg))
        elif expression.kind == "case":
            for branch in expression.cases:
                found.update(source_symbols(branch.when))
                found.update(source_symbols(branch.then))
            found.update(source_symbols(expression.else_expr))
        return found

    dimension_by_target = {
        (item.fact_symbol, item.dimension_symbol): item
        for item in expressions.dimensions
    }
    measure_by_target = {
        (item.fact_symbol, item.measure_symbol): item
        for item in expressions.measures
    }
    fact_values = []
    obligation_owners = {}
    for fact in blueprint.facts:
        for value in fact.dimensions + fact.measures:
            design = (
                dimension_by_target.get((fact.symbol, value.symbol))
                or measure_by_target[(fact.symbol, value.symbol)]
            )
            used_sources = sorted(source_symbols(design.expression))
            inputs = [
                {
                    "symbol": symbol,
                    "meaning": source_by_symbol[symbol].meaning,
                    "logical_type": source_by_symbol[symbol].logical_type,
                    "nullable": source_by_symbol[symbol].nullable,
                }
                for symbol in used_sources
            ]
            fact_values.append(FactValueComputation(
                fact_symbol=fact.symbol,
                value_symbol=value.symbol,
                inputs=inputs,
                expression=(
                    convert(design.expression, fact_context=True)
                    if design.expression is not None else None
                ),
                aggregation=getattr(value, "aggregation", "none"),
                logical_type=value.logical_type,
            ))
            for symbol in used_sources:
                obligation_owners.setdefault(
                    (fact.symbol, symbol),
                    [],
                ).append(value.symbol)
    computations = ComputationBlueprint(
        result_contract_hash=result.content_hash,
        fact_blueprint_hash=blueprint.content_hash,
        fact_values=fact_values,
        results=[
            ResultValueComputation(
                output_symbol=item.output_symbol,
                expression=convert(item.expression, fact_context=False),
            )
            for item in expressions.results
        ],
        result_filter=(
            convert(expressions.result_filter, fact_context=False)
            if expressions.result_filter is not None else None
        ),
    )
    inputs = SemanticInputObligationSet(
        result_contract_hash=result.content_hash,
        fact_blueprint_hash=blueprint.content_hash,
        computation_blueprint_hash=computations.content_hash,
        inputs=[
            SemanticInputObligation(
                obligation_id=hashlib.sha256(
                    f"{fact}:{symbol}".encode()
                ).hexdigest(),
                slot_name=symbol,
                fact_symbol=fact,
                value_symbols=sorted(set(values)),
                input_symbol=symbol,
                meaning=source_by_symbol[symbol].meaning,
                logical_type=source_by_symbol[symbol].logical_type,
                nullable=source_by_symbol[symbol].nullable,
                usage_paths=[f"{fact}.{value}.{symbol}" for value in values],
            )
            for (fact, symbol), values in sorted(obligation_owners.items())
        ],
    )
    return {
        "computations": computations,
        "input_obligations": inputs,
    }


def test_semantic_compiler_builds_stable_multi_fact_contract():
    staged = staged_reconciliation()
    frozen = staged_computation_artifacts(staged)
    first, first_symbols = compile_semantic_contract(
        *staged,
        **frozen,
        decision_hash="decisions",
        confirmed_decision_keys={"currency_basis"},
    )
    second, second_symbols = compile_semantic_contract(
        *staged,
        **frozen,
        decision_hash="decisions",
        confirmed_decision_keys={"currency_basis"},
    )

    assert first.content_hash == second.content_hash
    assert first_symbols == second_symbols
    assert first.grain == ["period"]
    assert len(first.facts) == 2
    assert first.facts[0].dimensions[0].expression is not None


def test_expression_schema_is_discriminated_and_rejects_function_symbol_field():
    schema = ExpressionDesign.model_json_schema()
    expression_schema = schema["$defs"]["SymbolExpression"]
    assert expression_schema["discriminator"]["propertyName"] == "kind"

    _, _, _, expressions, _ = staged_reconciliation()
    payload = expressions.model_dump(mode="json")
    payload["results"][0]["expression"] = {
        "kind": "function",
        "symbol": "coalesce",
        "args": [
            {
                "kind": "fact_value",
                "fact_symbol": "sales",
                "value_symbol": "period",
            }
        ],
    }

    with pytest.raises(ValidationError) as exc:
        ExpressionDesign.model_validate(payload)
    errors = exc.value.errors()
    assert any(item["type"] == "missing" for item in errors)
    assert any(item["type"] == "extra_forbidden" for item in errors)


def test_result_contract_rejects_unknown_grain_before_compilation():
    with pytest.raises(ValueError, match="RESULT_GRAIN_OUTPUT_MISSING"):
        ResultContract(
            procedure_name="usp_Invalid",
            purpose="错误粒度",
            result_mode="full_rows",
            outputs=[
                ResultOutputSpec(
                    symbol="document_number",
                    name="DocumentNumber",
                    meaning="单据编号",
                    logical_type="string",
                )
            ],
            grain_output_symbols=["matched_trans_id"],
        )


def test_result_contract_rejects_physical_output_name_at_first_stage():
    with pytest.raises(ValueError, match="SAP B1 物理字段名: DocEntry"):
        ResultContract(
            procedure_name="usp_InvalidPhysicalAlias",
            purpose="返回销售单据",
            result_mode="full_rows",
            outputs=[
                ResultOutputSpec(
                    symbol="document_id",
                    name="DocEntry",
                    meaning="销售单据内部标识",
                    logical_type="integer",
                )
            ],
            grain_output_symbols=["document_id"],
        )


def test_compiler_rejects_unconsumed_confirmed_decision():
    staged = staged_reconciliation()
    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            *staged,
            **staged_computation_artifacts(staged),
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis", "amount_basis"},
        )
    assert exc.value.code == "DECISION_NOT_CONSUMED"


def test_compiler_rejects_expression_target_not_in_frozen_blueprint():
    result, blueprint, sources, expressions, obligations = staged_reconciliation()
    frozen = staged_computation_artifacts(
        (result, blueprint, sources, expressions, obligations),
    )
    invalid = expressions.model_copy(deep=True)
    invalid.dimensions.append(
        FactDimensionExpression(
            fact_symbol="sales",
            dimension_symbol="invented_dimension",
            expression=SymbolExpression(
                kind="source", symbol="sales_date",
            ),
            logical_type="date",
        )
    )

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            result,
            blueprint,
            sources,
            invalid,
            obligations,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "EXPRESSION_TARGET_UNKNOWN"


def test_compiler_rejects_measure_that_changes_frozen_blueprint():
    staged = list(staged_reconciliation())
    frozen = staged_computation_artifacts(tuple(staged))
    expressions = staged[3].model_copy(deep=True)
    expressions.measures[0].aggregation = "avg"
    staged[3] = expressions

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            *staged,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "FACT_MEASURE_BLUEPRINT_MISMATCH"


def test_compiler_rejects_unused_source_field():
    result, blueprint, sources, expressions, obligations = staged_reconciliation()
    frozen = staged_computation_artifacts(
        (result, blueprint, sources, expressions, obligations),
    )
    invalid_sources = sources.model_copy(deep=True)
    invalid_sources.fields.append(SourceFieldRequirement(
        symbol="unused_value",
        entity_symbol="sales_invoice",
        meaning="未被任何事实使用的底层值",
        logical_type="decimal",
    ))

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            result,
            blueprint,
            invalid_sources,
            expressions,
            obligations,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "SOURCE_FIELD_UNUSED"


def test_compiler_infers_derived_dimension_type_before_schema():
    staged = list(staged_reconciliation())
    frozen = staged_computation_artifacts(tuple(staged))
    expressions = staged[3].model_copy(deep=True)
    expressions.dimensions[0].logical_type = "date"
    staged[3] = expressions

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            *staged,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "FACT_DIMENSION_BLUEPRINT_MISMATCH"
    assert exc.value.evidence["actual"] == "date"


def test_compiler_supports_typed_calendar_bucket_dimensions():
    result, blueprint, sources, expressions, _ = staged_reconciliation()
    period_output = next(
        item for item in result.outputs if item.symbol == "period"
    )
    period_output.logical_type = "date"
    for fact in blueprint.facts:
        fact.dimensions[0].logical_type = "date"
    for index, dimension in enumerate(expressions.dimensions):
        source_symbol = "sales_date" if index == 0 else "journal_date"
        source = SymbolExpression(kind="source", symbol=source_symbol)
        dimension.logical_type = "date"
        dimension.expression = SymbolExpression(
            kind="function",
            operator="DATEFROMPARTS",
            args=[
                SymbolExpression(
                    kind="function", operator="YEAR", args=[source],
                ),
                SymbolExpression(
                    kind="function",
                    operator="MONTH",
                    args=[source.model_copy(deep=True)],
                ),
                SymbolExpression(kind="literal", value=1),
            ],
        )

    compiled, _ = compile_semantic_contract(
        result,
        blueprint,
        sources,
        expressions,
        compile_semantic_obligations(result, blueprint),
        **staged_computation_artifacts((
            result,
            blueprint,
            sources,
            expressions,
            compile_semantic_obligations(result, blueprint),
        )),
        decision_hash="decisions",
        confirmed_decision_keys={"currency_basis"},
    )

    assert compiled.facts[0].dimensions[0].logical_type == "date"
    assert (
        compiled.facts[0].dimensions[0].expression.operator
        == "DATEFROMPARTS"
    )


def test_compiler_rejects_invalid_calendar_function_arity():
    result, blueprint, sources, expressions, obligations = staged_reconciliation()
    frozen = staged_computation_artifacts(
        (result, blueprint, sources, expressions, obligations),
    )
    expressions.dimensions[0].expression = SymbolExpression(
        kind="function",
        operator="DATEFROMPARTS",
        args=[SymbolExpression(kind="source", symbol="sales_date")],
    )

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            result,
            blueprint,
            sources,
            expressions,
            obligations,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "EXPRESSION_FUNCTION_ARITY_INVALID"


def test_compiler_rejects_output_dependency_cycle_with_stable_code():
    staged = list(staged_reconciliation())
    frozen = staged_computation_artifacts(tuple(staged))
    expressions = staged[3].model_copy(deep=True)
    by_output = {
        item.output_symbol: item for item in expressions.results
    }
    by_output["sales_amount"].expression = SymbolExpression(
        kind="output", symbol="journal_amount",
    )
    by_output["journal_amount"].expression = SymbolExpression(
        kind="output", symbol="sales_amount",
    )
    staged[3] = expressions

    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            *staged,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "RESULT_DEPENDENCY_CYCLE"
