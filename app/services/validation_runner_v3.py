"""V3 最终验证：同一数据库、同一参数、同一快照下比较结果。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from app.contracts.reference import ReferenceBundle, ValidationCase
from app.contracts.schema import CatalogSnapshot, SchemaBinding
from app.contracts.semantic import SemanticContract
from app.contracts.validation import (
    CoverageEvidence,
    ProcedureCandidateV3,
    ValidationEvidence,
)
from app.services.catalog_v3 import catalog_fingerprint
from app.services.comparators_v3 import ComparisonError, compare_rows
from app.services.issues_v3 import GatePipeline, issue
from app.services.sql_renderer_v3 import RENDERER_VERSION, SqlRendererV3
from app.services.validation_cases import choose_case, coverage_is_effective


MAX_RESULT_ROWS = 50_000


def _serialize_v3(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _runtime_value_v3(value):
    """保留计算所需的数据库原生类型，仅规范二进制文本。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass(frozen=True)
class RuntimeResult:
    columns: tuple[str, ...]
    rows: list[dict]


@dataclass(frozen=True)
class SnapshotExecution:
    snapshot_id: str
    database_identity: str
    references: dict[str, RuntimeResult]
    actual: RuntimeResult


class ValidationExecutor(Protocol):
    def inspect_environment(self) -> dict: ...

    def execute_same_snapshot(
        self,
        contract: SemanticContract,
        reference_sql: dict[str, str],
        actual_procedure_sql: str,
        actual_procedure_name: str,
        parameters: dict,
    ) -> SnapshotExecution: ...


class ValidationExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class SqlServerValidationExecutor:
    """在一个 SNAPSHOT 事务中执行所有 Reference 与实际候选查询。"""

    def __init__(self, connection_factory=None, max_rows: int = MAX_RESULT_ROWS):
        if connection_factory is None:
            from app.db.sqlserver import get_connection

            connection_factory = get_connection
        self.connection_factory = connection_factory
        self.max_rows = max_rows

    def inspect_environment(self) -> dict:
        connection = self.connection_factory()
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                    COALESCE(
                        CONVERT(nvarchar(128), SERVERPROPERTY('ServerName')),
                        N'(unknown)'
                    ) AS server_identity,
                    DB_NAME() AS database_name,
                    DB_ID() AS database_id,
                    snapshot_isolation_state_desc
                FROM sys.databases
                WHERE database_id = DB_ID()
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise ValidationExecutionError(
                    "ENV_DATABASE_IDENTITY_UNAVAILABLE",
                    "无法读取当前数据库身份",
                )
            return {
                "server_identity": str(row[0]),
                "database_name": str(row[1]),
                "database_id": int(row[2]),
                "snapshot_isolation_state": str(row[3]),
            }
        finally:
            connection.close()

    def discover_parameter_values(
        self,
        contract: SemanticContract,
        binding,
    ) -> tuple[dict, dict]:
        """从已绑定字段中探测覆盖参数；不依赖默认值，也不修改业务数据。"""
        field_by_semantic: dict[str, list] = {}
        for item in binding.fields:
            field_by_semantic.setdefault(item.semantic_id, []).append(item)
        entity_by_id = {item.entity_id: item for item in binding.entities}
        parameter_fields: dict[str, str] = {}
        for filter_item in contract.filters:
            if not filter_item.parameter_ids:
                continue
            if len(filter_item.field_ids) != 1:
                raise ValidationExecutionError(
                    "COVERAGE_PARAMETER_FIELD_AMBIGUOUS",
                    f"过滤条件 {filter_item.id} 不能唯一定位探测字段",
                )
            for parameter_id in filter_item.parameter_ids:
                parameter_fields.setdefault(
                    parameter_id, filter_item.field_ids[0]
                )

        values: dict = {}
        evidence: dict = {"source": "schema_bound_data_probe", "parameters": {}}
        connection = self.connection_factory(autocommit=False)
        try:
            cursor = connection.cursor()
            for parameter in contract.parameters:
                semantic_id = parameter_fields.get(parameter.id)
                candidates = field_by_semantic.get(semantic_id or "", [])
                if len(candidates) > 1:
                    raise ValidationExecutionError(
                        "COVERAGE_PARAMETER_FIELD_AMBIGUOUS",
                        f"参数 {parameter.name} 对应多个物理字段",
                    )
                if not candidates:
                    if parameter.default is not None or not parameter.required:
                        values[parameter.id] = parameter.default
                        evidence["parameters"][parameter.id] = {
                            "strategy": "confirmed_default",
                        }
                        continue
                    raise ValidationExecutionError(
                        "COVERAGE_PARAMETER_FIELD_MISSING",
                        f"必填参数 {parameter.name} 没有可探测的绑定字段",
                    )
                field = candidates[0]
                entity = entity_by_id[field.entity_id]
                schema_name = entity.schema.replace("]", "]]")
                object_name = entity.object_name.replace("]", "]]")
                column_name = field.column_name.replace("]", "]]")
                qualified = (
                    f"[{schema_name}].[{object_name}]"
                )
                column_sql = f"[{column_name}]"
                strategy = "first_non_null"
                if parameter.logical_type in {"date", "datetime"}:
                    aggregate = (
                        "MAX" if parameter.boundary == "inclusive_full_day"
                        else "MIN"
                    )
                    cursor.execute(
                        f"SELECT {aggregate}({column_sql}) FROM {qualified} "
                        f"WHERE {column_sql} IS NOT NULL"
                    )
                    strategy = aggregate.casefold()
                else:
                    cursor.execute(
                        f"SELECT TOP (1) {column_sql} FROM {qualified} "
                        f"WHERE {column_sql} IS NOT NULL ORDER BY {column_sql}"
                    )
                row = cursor.fetchone()
                value = row[0] if row else None
                if value is None and parameter.required:
                    raise ValidationExecutionError(
                        "COVERAGE_PARAMETER_VALUE_NOT_FOUND",
                        f"参数 {parameter.name} 的绑定字段没有可用数据",
                    )
                values[parameter.id] = _serialize_v3(value)
                evidence["parameters"][parameter.id] = {
                    "strategy": strategy,
                    "object_id": entity.object_id,
                    "column_id": field.column_id,
                }
            connection.rollback()
            return values, evidence
        except ValidationExecutionError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise ValidationExecutionError(
                "COVERAGE_DATA_PROBE_FAILED",
                "无法从绑定数据选择覆盖参数",
                detail=str(exc),
            ) from exc
        finally:
            connection.close()

    def execute_same_snapshot(
        self,
        contract: SemanticContract,
        reference_sql: dict[str, str],
        actual_procedure_sql: str,
        actual_procedure_name: str,
        parameters: dict,
    ) -> SnapshotExecution:
        parameter_by_id = {item.id: item for item in contract.parameters}
        unknown = set(parameters) - set(parameter_by_id)
        missing = {
            item.id for item in contract.parameters
            if item.required and item.id not in parameters
        }
        if unknown or missing:
            raise ValidationExecutionError(
                "EXEC_PARAMETER_INVALID",
                "执行参数与语义合同不一致",
                detail=f"unknown={sorted(unknown)}, missing={sorted(missing)}",
            )

        # 必须先在 autocommit 会话设置隔离级别，再显式开启事务；
        # pyodbc autocommit=False 可能在首条 SET 前已启动默认级别事务。
        connection = self.connection_factory(autocommit=True)
        try:
            cursor = connection.cursor()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute(
                "SELECT @@SPID, @@TRANCOUNT, "
                "CONCAT(COALESCE(CONVERT(nvarchar(128), "
                "SERVERPROPERTY('ServerName')), N'(unknown)'), "
                "N'/', DB_NAME(), N'/', DB_ID())"
            )
            identity = cursor.fetchone()
            snapshot_id = (
                f"spid:{identity[0]}/trancount:{identity[1]}/"
                f"run:{uuid.uuid4()}"
            )
            database_identity = str(identity[2])
            references = {
                fact_id: self._execute_query(
                    cursor,
                    contract,
                    sql,
                    parameters,
                )
                for fact_id, sql in reference_sql.items()
            }
            cursor.execute(actual_procedure_sql)
            actual = self._execute_procedure(
                cursor, contract, actual_procedure_name, parameters
            )
            try:
                cursor.execute(
                    f"DROP PROCEDURE [{actual_procedure_name.replace(']', ']]')}]"
                )
            except Exception as exc:
                raise ValidationExecutionError(
                    "INTERNAL_CLEANUP_FAILED",
                    "隔离存储过程清理失败",
                    detail=str(exc),
                ) from exc
            connection.rollback()
            return SnapshotExecution(
                snapshot_id=snapshot_id,
                database_identity=database_identity,
                references=references,
                actual=actual,
            )
        except ValidationExecutionError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            raise ValidationExecutionError(
                "EXEC_SNAPSHOT_FAILED",
                "同快照执行失败",
                detail=str(exc),
            ) from exc
        finally:
            connection.close()

    def preflight_reference(
        self,
        contract: SemanticContract,
        sql: str,
        parameters: dict,
    ) -> list[dict]:
        # Preflight 只证明单个 Reference 可执行且能命中覆盖数据，
        # 不与候选 SP 做双边比较，因此不要求 SNAPSHOT。
        connection = self.connection_factory(autocommit=False)
        try:
            cursor = connection.cursor()
            result = self._execute_query(cursor, contract, sql, parameters)
            connection.rollback()
            return result.rows
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _execute_query(
        self,
        cursor,
        contract: SemanticContract,
        sql: str,
        parameters: dict,
    ) -> RuntimeResult:
        declarations = []
        values = []
        type_map = {
            "string": "nvarchar(4000)",
            "integer": "bigint",
            "decimal": "decimal(38,10)",
            "money": "decimal(19,4)",
            "date": "date",
            "datetime": "datetime2(7)",
            "boolean": "bit",
        }
        for parameter in contract.parameters:
            value = parameters.get(parameter.id, parameter.default)
            declarations.append(
                f"DECLARE {parameter.name} {type_map[parameter.logical_type]} = ?;"
            )
            values.append(value)
        batch = "\n".join(declarations + [sql])
        cursor.execute(batch, values)
        while cursor.description is None:
            if not cursor.nextset():
                raise ValidationExecutionError(
                    "EXEC_RESULT_SET_MISSING",
                    "查询没有返回结果集",
                )
        columns = tuple(str(item[0]) for item in cursor.description)
        fetched = cursor.fetchmany(self.max_rows + 1)
        if len(fetched) > self.max_rows:
            raise ValidationExecutionError(
                "EXEC_RESULT_LIMIT_EXCEEDED",
                f"结果超过 {self.max_rows} 行安全上限",
            )
        rows = [
            {
                column: _runtime_value_v3(value)
                for column, value in zip(columns, row)
            }
            for row in fetched
        ]
        return RuntimeResult(columns=columns, rows=rows)

    def _execute_procedure(
        self,
        cursor,
        contract: SemanticContract,
        procedure_name: str,
        parameters: dict,
    ) -> RuntimeResult:
        ordered = [
            item for item in contract.parameters if item.id in parameters
        ]
        assignments = ", ".join(f"{item.name}=?" for item in ordered)
        name = procedure_name.replace("]", "]]")
        sql = f"EXEC [{name}]"
        if assignments:
            sql += " " + assignments
        cursor.execute(sql, *[parameters[item.id] for item in ordered])
        columns = (
            tuple(str(item[0]) for item in cursor.description)
            if cursor.description else ()
        )
        rows = []
        if cursor.description:
            for row in cursor.fetchmany(self.max_rows + 1):
                rows.append(
                    {
                        column: _runtime_value_v3(value)
                        for column, value in zip(columns, row)
                    }
                )
        if len(rows) > self.max_rows:
            raise ValidationExecutionError(
                "EXEC_RESULT_LIMIT_EXCEEDED",
                f"候选 SP 返回超过 {self.max_rows} 行，停止验证",
            )
        return RuntimeResult(columns=columns, rows=rows)


def _names(items) -> list[str]:
    return [item.name.casefold() for item in items]


def validate_candidate_v3(
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    binding: SchemaBinding,
    reference: ReferenceBundle,
    candidate: ProcedureCandidateV3,
    *,
    executor: ValidationExecutor,
    case_id: str | None = None,
) -> ValidationEvidence:
    pipeline = GatePipeline()
    selected_case: ValidationCase | None = None
    comparisons = []
    coverage = None
    database_identity = (
        f"{catalog.server_identity}/{catalog.database_name}/{catalog.database_id}"
    )
    # 用例选择不依赖数据库环境；即使环境门提前终止，也必须保留
    # “本次原本验证哪个参数集”的完整证据。
    selected_case = choose_case(reference, case_id)

    environment = executor.inspect_environment()
    expected_environment = {
        "server_identity": catalog.server_identity,
        "database_name": catalog.database_name,
        "database_id": catalog.database_id,
    }
    actual_environment = {
        key: environment.get(key) for key in expected_environment
    }
    if actual_environment != expected_environment:
        pipeline.record(
            "environment",
            "failed",
            issues=[
                issue(
                    code="ENV_DATABASE_IDENTITY_MISMATCH",
                    stage="environment",
                    artifact="catalog_snapshot",
                    title="数据库环境已变化",
                    summary="验证连接与 Schema 快照不是同一个数据库。",
                    evidence={
                        "expected": expected_environment,
                        "actual": actual_environment,
                    },
                    user_action="请重新捕获 Schema 并重新生成全部制品。",
                )
            ],
        )
        return _evidence(
            candidate,
            reference,
            catalog,
            pipeline,
            selected_case,
            comparisons,
            coverage,
            "failed",
            database_identity,
        )
    if str(environment.get("snapshot_isolation_state")).strip().upper() != "ON":
        pipeline.record(
            "environment",
            "inconclusive",
            issues=[
                issue(
                    code="ENV_SNAPSHOT_ISOLATION_UNAVAILABLE",
                    stage="environment",
                    artifact="database",
                    title="无法建立一致性快照",
                    summary="数据库未启用 SNAPSHOT isolation，不能证明两边读取的是同一版本数据。",
                    evidence=environment,
                    status="inconclusive",
                    user_action="请由数据库管理员启用快照隔离，或在隔离测试库中验证。",
                )
            ],
        )
        return _evidence(
            candidate,
            reference,
            catalog,
            pipeline,
            selected_case,
            comparisons,
            coverage,
            "inconclusive",
            database_identity,
        )
    pipeline.record("environment", "passed", details=environment)

    if not contract.entities or not contract.outputs:
        pipeline.record(
            "semantic_contract",
            "failed",
            issues=[
                issue(
                    code="CONTRACT_INCOMPLETE",
                    stage="semantic_contract",
                    artifact="semantic_contract",
                    title="业务合同不完整",
                    summary="业务实体或输出字段为空。",
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record(
        "semantic_contract",
        "passed",
        details={"contract_hash": contract.content_hash},
    )

    try:
        from app.services.schema_binding_v3 import validate_binding_against_catalog

        validate_binding_against_catalog(contract, catalog, binding)
    except Exception as exc:
        pipeline.record(
            "schema_binding",
            "failed",
            issues=[
                issue(
                    code="SCHEMA_BINDING_STALE",
                    stage="schema_binding",
                    artifact="schema_binding",
                    title="Schema 绑定已失效",
                    summary=str(exc),
                    technical_detail=getattr(exc, "code", ""),
                    user_action="请从当前数据库重新绑定 Schema。",
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record(
        "schema_binding",
        "passed",
        details={"binding_hash": binding.content_hash},
    )

    reference_integrity_error = ""
    try:
        renderer = SqlRendererV3(contract, binding)
        from app.services.reference_planner import (
            coverage_plan,
            referenced_object_ids,
        )

        for fact in reference.facts:
            if fact.expected_sql != renderer.render_query(fact.reference_plan):
                raise ValueError(f"{fact.fact_id} 的 SQL 与冻结计划不一致")
            if fact.allowed_object_ids != referenced_object_ids(
                fact.reference_plan,
                binding,
            ):
                raise ValueError(f"{fact.fact_id} 的对象白名单与计划不一致")
            expected_coverage_plan = coverage_plan(
                fact.reference_plan, binding,
            )
            expected_coverage_sql = (
                renderer.render_query(expected_coverage_plan)
                if expected_coverage_plan is not None else None
            )
            if fact.coverage_sql != expected_coverage_sql:
                raise ValueError(f"{fact.fact_id} 的覆盖查询与计划不一致")
        if contract.facts:
            from app.services.fact_compiler_v3 import compile_fact_plan

            semantic_facts = {item.id: item for item in contract.facts}
            for fact in reference.facts:
                expected_plan = compile_fact_plan(
                    contract, binding, semantic_facts[fact.fact_id],
                )
                if (
                    fact.reference_plan.canonical_json()
                    != expected_plan.canonical_json()
                ):
                    raise ValueError(
                        f"{fact.fact_id} 的 Reference 计划偏离冻结事实"
                    )
    except Exception as exc:
        reference_integrity_error = str(exc)
    if (
        reference.status != "reference_ready"
        or reference.contract_hash != contract.content_hash
        or reference.binding_hash != binding.content_hash
        or reference.renderer_version != RENDERER_VERSION
        or reference_integrity_error
    ):
        pipeline.record(
            "reference_plan",
            "failed",
            issues=[
                issue(
                    code="REFERENCE_FROZEN_ARTIFACT_INVALID",
                    stage="reference_plan",
                    artifact="reference_bundle",
                    title="Reference 制品无效",
                    summary=(
                        reference_integrity_error
                        or "Reference 未冻结或与当前上游制品不一致。"
                    ),
                    user_action="请重新生成、编译并预执行 Reference。",
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record(
        "reference_plan",
        "passed",
        details={"reference_bundle_hash": reference.content_hash},
    )

    expected_compile_keys = {
        fact.fact_id for fact in reference.facts
    } | {
        f"{fact.fact_id}:coverage"
        for fact in reference.facts if fact.coverage_sql
    }
    if (
        set(reference.compile_evidence) != expected_compile_keys
        or not all(
            item.get("ok") for item in reference.compile_evidence.values()
        )
    ):
        pipeline.record(
            "reference_compile",
            "failed",
            issues=[
                issue(
                    code="REFERENCE_COMPILE_EVIDENCE_INVALID",
                    stage="reference_compile",
                    artifact="reference_bundle",
                    title="Reference 编译证据无效",
                    summary="并非所有 Reference 都通过了静态编译。",
                    evidence=reference.compile_evidence,
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record("reference_compile", "passed")

    expected_fact_ids = {fact.fact_id for fact in reference.facts}
    source_reference_mode = (
        reference.facts[0].comparison_role == "source_fact"
    )
    preflight_values = list(reference.preflight_evidence.values())
    preflight_effective = (
        all(item.get("executed") for item in preflight_values)
        and (
            any(
                int(item.get("source_row_count", 0)) > 0
                for item in preflight_values
            )
            if source_reference_mode
            else all(
                int(item.get("row_count", 0)) > 0
                and int(
                    item.get("source_row_count", item.get("row_count", 0))
                ) > 0
                for item in preflight_values
            )
        )
    )
    if (
        set(reference.preflight_evidence) != expected_fact_ids
        or not preflight_effective
    ):
        pipeline.record(
            "reference_preflight",
            "inconclusive",
            issues=[
                issue(
                    code="REFERENCE_PREFLIGHT_INEFFECTIVE",
                    stage="reference_preflight",
                    artifact="reference_bundle",
                    title="Reference 预执行没有有效数据",
                    summary="空结果不能作为“业务逻辑正确”的证明。",
                    evidence=reference.preflight_evidence,
                    status="inconclusive",
                    user_action="请选择能命中业务数据的覆盖参数。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "inconclusive", database_identity,
        )
    pipeline.record("reference_preflight", "passed")

    procedure_integrity_error = ""
    try:
        from app.services.sql_compile_v3 import parameter_definitions

        rendered_procedure = SqlRendererV3(
            contract,
            binding,
        ).render_procedure(candidate.procedure_plan)
        if candidate.procedure_sql != rendered_procedure:
            raise ValueError("SP SQL 与冻结关系计划不一致")
        if candidate.parameters != parameter_definitions(contract):
            raise ValueError("SP 参数清单与语义合同不一致")
        if contract.facts:
            from app.services.fact_compiler_v3 import compile_contract_plan

            expected_plan = compile_contract_plan(contract, binding)
            if (
                candidate.procedure_plan.canonical_json()
                != expected_plan.canonical_json()
            ):
                raise ValueError("结构化事实合同的 SP 计划偏离冻结语义")
    except Exception as exc:
        procedure_integrity_error = str(exc)
    if (
        candidate.contract_hash != contract.content_hash
        or candidate.binding_hash != binding.content_hash
        or candidate.reference_bundle_hash != reference.content_hash
        or candidate.renderer_version != RENDERER_VERSION
        or procedure_integrity_error
    ):
        pipeline.record(
            "procedure_plan",
            "failed",
            issues=[
                issue(
                    code="PROCEDURE_FROZEN_ARTIFACT_INVALID",
                    stage="procedure_plan",
                    artifact="procedure_candidate",
                    title="SP 候选制品无效",
                    summary=(
                        procedure_integrity_error
                        or "SP 候选与 Reference 或上游合同不一致。"
                    ),
                    user_action="请基于当前冻结 Reference 重新生成 SP。",
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record("procedure_plan", "passed")

    if candidate.status != "candidate_compiled" or not candidate.compile_evidence.get(
        "ok"
    ):
        pipeline.record(
            "procedure_compile",
            "failed",
            issues=[
                issue(
                    code="PROCEDURE_COMPILE_EVIDENCE_INVALID",
                    stage="procedure_compile",
                    artifact="procedure_candidate",
                    title="SP 编译证据无效",
                    summary="SP 候选未通过 SQL Server 静态编译。",
                    evidence=candidate.compile_evidence,
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record("procedure_compile", "passed")

    candidate_columns = _names(candidate.result_schema)
    projection_errors = {
        fact.fact_id: [
            column for column in fact.actual_projection
            if column.casefold() not in candidate_columns
        ]
        for fact in reference.facts
        if fact.comparison_role == "direct_actual"
    }
    projection_errors = {
        key: value for key, value in projection_errors.items() if value
    }
    if projection_errors:
        pipeline.record(
            "result_contract",
            "failed",
            issues=[
                issue(
                    code="RESULT_PROJECTION_MISSING",
                    stage="result_contract",
                    artifact="procedure_candidate",
                    title="SP 输出不满足比较合同",
                    summary="Reference 需要的实际投影列未全部输出。",
                    evidence=projection_errors,
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )

    temporary_procedure_name = "#v3_" + uuid.uuid4().hex
    actual_procedure_sql = SqlRendererV3(
        contract, binding
    ).render_procedure(
        candidate.procedure_plan,
        temporary_name=temporary_procedure_name,
    )
    try:
        execution = executor.execute_same_snapshot(
            contract,
            {
                **{
                    fact.fact_id: fact.expected_sql
                    for fact in reference.facts
                },
                **{
                    f"{fact.fact_id}:coverage": fact.coverage_sql
                    for fact in reference.facts
                    if fact.coverage_sql
                },
            },
            actual_procedure_sql,
            temporary_procedure_name,
            selected_case.parameters,
        )
    except ValidationExecutionError as exc:
        pipeline.record(
            "result_contract",
            "failed",
            issues=[
                issue(
                    code=exc.code,
                    stage="result_contract",
                    artifact="validation_execution",
                    title="候选执行失败",
                    summary=str(exc),
                    technical_detail=exc.detail,
                    retryable=True,
                    user_action="请检查参数、数据库状态和技术详情后重试。",
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )

    if [item.casefold() for item in execution.actual.columns] != candidate_columns:
        pipeline.record(
            "result_contract",
            "failed",
            issues=[
                issue(
                    code="RESULT_RUNTIME_SCHEMA_MISMATCH",
                    stage="result_contract",
                    artifact="procedure_candidate",
                    title="SP 运行时输出结构不一致",
                    summary="实际返回列与编译后的结果合同不一致。",
                    evidence={
                        "declared": candidate_columns,
                        "runtime": list(execution.actual.columns),
                    },
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    reference_schema_errors = {}
    for fact in reference.facts:
        runtime = execution.references.get(fact.fact_id)
        expected_names = _names(fact.expected_schema)
        actual_names = (
            [item.casefold() for item in runtime.columns]
            if runtime is not None else []
        )
        if actual_names != expected_names:
            reference_schema_errors[fact.fact_id] = {
                "expected": expected_names,
                "runtime": actual_names,
            }
        if fact.coverage_sql:
            coverage_runtime = execution.references.get(
                f"{fact.fact_id}:coverage"
            )
            if (
                coverage_runtime is None
                or [item.casefold() for item in coverage_runtime.columns]
                != ["coveragecount"]
            ):
                reference_schema_errors[f"{fact.fact_id}:coverage"] = {
                    "expected": ["coveragecount"],
                    "runtime": (
                        list(coverage_runtime.columns)
                        if coverage_runtime is not None else []
                    ),
                }
    if reference_schema_errors:
        pipeline.record(
            "result_contract",
            "failed",
            issues=[
                issue(
                    code="REFERENCE_RUNTIME_SCHEMA_MISMATCH",
                    stage="result_contract",
                    artifact="reference_bundle",
                    title="Reference 运行时输出结构不一致",
                    summary="Reference 实际返回列与冻结结果合同不一致。",
                    evidence=reference_schema_errors,
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )
    pipeline.record(
        "result_contract",
        "passed",
        details={
            "columns": list(execution.actual.columns),
            "snapshot_id": execution.snapshot_id,
        },
    )

    try:
        composed_expected_rows = None
        for fact in reference.facts:
            if fact.comparison_role == "source_fact":
                continue
            actual_rows = [
                {
                    column: row[
                        next(
                            name for name in row
                            if str(name).casefold() == column.casefold()
                        )
                    ]
                    for column in fact.actual_projection
                }
                for row in execution.actual.rows
            ]
            expected = execution.references[fact.fact_id]
            comparisons.append(
                compare_rows(
                    fact.fact_id,
                    fact.comparator,
                    actual_rows,
                    expected.rows,
                )
            )
        if reference.facts[0].comparison_role == "source_fact":
            from app.services.fact_compiler_v3 import compose_expected_rows

            composed_expected_rows = compose_expected_rows(
                contract,
                {
                    fact.fact_id: execution.references[fact.fact_id].rows
                    for fact in reference.facts
                },
                selected_case.parameters,
            )
            comparisons.append(
                compare_rows(
                    "composed_expected",
                    reference.result_comparator,
                    execution.actual.rows,
                    composed_expected_rows,
                )
            )
    except (ComparisonError, KeyError, StopIteration, ValueError) as exc:
        pipeline.record(
            "business_comparison",
            "failed",
            issues=[
                issue(
                    code="COMPARE_CONTRACT_INVALID",
                    stage="business_comparison",
                    artifact="comparison",
                    title="比较合同无法执行",
                    summary=str(exc),
                )
            ],
        )
        return _stopped(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, database_identity,
        )

    expected_counts = []
    for fact in reference.facts:
        coverage_result = execution.references.get(
            f"{fact.fact_id}:coverage"
        )
        if coverage_result is not None:
            expected_counts.append(
                int(
                    coverage_result.rows[0].get("CoverageCount", 0)
                    if coverage_result.rows else 0
                )
            )
        else:
            expected_counts.append(
                len(execution.references[fact.fact_id].rows)
            )
    source_fact_mode = (
        reference.facts[0].comparison_role == "source_fact"
    )
    coverage = CoverageEvidence(
        effective=coverage_is_effective(
            expected_counts,
            composed_expected_row_count=(
                len(composed_expected_rows or [])
                if source_fact_mode else 0
            ),
        ),
        expected_row_count=(
            len(composed_expected_rows or [])
            if source_fact_mode else sum(expected_counts)
        ),
        actual_row_count=len(execution.actual.rows),
        case_id=selected_case.case_id,
    )
    if selected_case.kind in {"coverage", "boundary"} and not coverage.effective:
        pipeline.record(
            "business_comparison",
            "inconclusive",
            issues=[
                issue(
                    code="COMPARE_COVERAGE_EMPTY",
                    stage="business_comparison",
                    artifact="comparison",
                    title="本次验证没有有效业务覆盖",
                    summary=(
                        "Reference 在需要命中数据的覆盖或边界用例下返回空结果，"
                        "不能判定 SP 正确。"
                    ),
                    status="inconclusive",
                    user_action="请选择能命中业务数据的参数重新验证。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "inconclusive", execution.database_identity,
        )
    if (
        selected_case.kind == "empty"
        and (
            coverage.expected_row_count != 0
            or coverage.actual_row_count != 0
        )
    ):
        pipeline.record(
            "business_comparison",
            "inconclusive",
            issues=[
                issue(
                    code="COMPARE_EMPTY_CASE_INEFFECTIVE",
                    stage="business_comparison",
                    artifact="comparison",
                    title="空结果用例没有形成空结果",
                    summary="该用例用于证明合法空集，但 Actual 或 Expected 仍返回了数据。",
                    status="inconclusive",
                    user_action="重新选择一个确实为空的参数范围。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "inconclusive", execution.database_identity,
        )
    if (
        selected_case.kind == "empty"
        and not contract.allow_empty
        and coverage.expected_row_count == 0
        and coverage.actual_row_count == 0
    ):
        pipeline.record(
            "business_comparison",
            "failed",
            issues=[
                issue(
                    code="COMPARE_EMPTY_NOT_ALLOWED",
                    stage="business_comparison",
                    artifact="comparison",
                    title="业务合同不允许空结果",
                    summary="Actual 与 Expected 都为空，但 SemanticContract 禁止空结果。",
                    user_action="请选择有效期间，或返回业务方案确认空结果语义。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "failed", execution.database_identity,
        )
    mismatches = [item for item in comparisons if not item.match]
    if mismatches:
        pipeline.record(
            "business_comparison",
            "failed",
            issues=[
                issue(
                    code="COMPARE_RESULT_MISMATCH",
                    stage="business_comparison",
                    artifact="comparison",
                    title="SP 与独立 Reference 结果不一致",
                    summary="比较发现缺失、多出、重复键或字段值差异。",
                    evidence={
                        "facts": [
                            {
                                "fact_id": item.fact_id,
                                "summary": item.summary,
                            }
                            for item in mismatches
                        ]
                    },
                    user_action="请根据行级差异修正 SP 关系计划，不要修改 Reference 迎合结果。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "failed", execution.database_identity,
        )
    pipeline.record(
        "business_comparison",
        "passed",
        details={"fact_count": len(comparisons)},
    )

    integrity_ok = (
        execution.snapshot_id
        and execution.database_identity
        and all(
            item.actual_row_count == len(execution.actual.rows)
            for item in comparisons
        )
    )
    if not integrity_ok:
        pipeline.record(
            "evidence_integrity",
            "failed",
            issues=[
                issue(
                    code="EVIDENCE_INTEGRITY_FAILED",
                    stage="evidence_integrity",
                    artifact="validation_evidence",
                    title="验证证据不完整",
                    summary="快照身份或行数证据无法闭合。",
                )
            ],
        )
        return _evidence(
            candidate, reference, catalog, pipeline, selected_case,
            comparisons, coverage, "failed", execution.database_identity,
        )
    pipeline.record(
        "evidence_integrity",
        "passed",
        details={
            "snapshot_id": execution.snapshot_id,
            "candidate_hash": candidate.content_hash,
            "reference_bundle_hash": reference.content_hash,
        },
    )
    return _evidence(
        candidate,
        reference,
        catalog,
        pipeline,
        selected_case,
        comparisons,
        coverage,
        "validated",
        execution.database_identity,
    )


def _stopped(
    candidate,
    reference,
    catalog,
    pipeline,
    selected_case,
    comparisons,
    coverage,
    database_identity,
) -> ValidationEvidence:
    status = "inconclusive" if any(
        item.status == "inconclusive" for item in pipeline.results
    ) else "failed"
    return _evidence(
        candidate, reference, catalog, pipeline, selected_case,
        comparisons, coverage, status, database_identity,
    )


def _evidence(
    candidate,
    reference,
    catalog,
    pipeline,
    selected_case,
    comparisons,
    coverage,
    status,
    database_identity,
) -> ValidationEvidence:
    return ValidationEvidence(
        candidate_hash=candidate.content_hash,
        reference_bundle_hash=reference.content_hash,
        catalog_fingerprint=catalog_fingerprint(catalog),
        database_identity=database_identity,
        validation_case=(
            selected_case.model_dump(mode="json") if selected_case else {}
        ),
        stages=pipeline.results,
        comparisons=comparisons,
        coverage=coverage,
        status=status,
        created_at=datetime.now(timezone.utc),
    )
