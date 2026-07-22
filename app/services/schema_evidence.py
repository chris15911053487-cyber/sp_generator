"""按 QuerySpec 捕获并精确绑定实时 SQL Server schema 证据。"""
import difflib
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.generation_harness import ColumnRef, QuerySpec


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ColumnEvidence(_StrictModel):
    name: str = Field(min_length=1)
    sql_type: str = Field(min_length=1)
    max_length: int | None
    precision: int | None
    scale: int | None
    nullable: bool
    description: str | None


class TableEvidence(_StrictModel):
    schema_name: str = Field(alias="schema", min_length=1)

    @property
    def schema(self) -> str:
        return self.schema_name
    name: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    columns: list[ColumnEvidence]


class UnresolvedIdentifier(_StrictModel):
    kind: Literal["object", "column"]
    identifier: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    candidates: list[str]


class SchemaEvidence(_StrictModel):
    database_name: str = Field(min_length=1)
    captured_at: datetime
    objects: list[TableEvidence]
    unresolved: list[UnresolvedIdentifier]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


SchemaLoader = Callable[[list[tuple[str, str]]], dict]


def _referenced_identifiers(
    query_spec: QuerySpec,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], set[str]]]:
    object_refs: set[tuple[str, str]] = set()
    column_refs: dict[tuple[str, str], set[str]] = {}

    for procedure in query_spec.procedures:
        aliases = {
            source.alias: (source.schema, source.table)
            for source in procedure.sources
        }
        for qualified in aliases.values():
            object_refs.add(qualified)
            column_refs.setdefault(qualified, set())

        def add_column(reference: ColumnRef) -> None:
            qualified = aliases[reference.source_alias]
            column_refs[qualified].add(reference.column)

        for join in procedure.joins:
            add_column(join.left)
            add_column(join.right)
        for item in procedure.filters:
            for reference in item.column_refs:
                add_column(reference)
        for reference in procedure.grain:
            add_column(reference)
        for output in procedure.outputs:
            for reference in output.source_columns:
                add_column(reference)
        for write in procedure.writes:
            qualified = (write.schema, write.table)
            object_refs.add(qualified)
            column_refs.setdefault(qualified, set()).update(write.key_columns)

    return sorted(object_refs), column_refs


def _matches(value: str, candidates: list[str], cutoff: float) -> list[str]:
    return difflib.get_close_matches(value, sorted(set(candidates)), n=3, cutoff=cutoff)


def _fingerprint(database_name: str, objects: list[TableEvidence]) -> str:
    normalized = {
        "database_name": database_name,
        "objects": [
            {
                **item.model_dump(mode="json", by_alias=True, exclude={"columns"}),
                "columns": [
                    column.model_dump(mode="json")
                    for column in sorted(item.columns, key=lambda value: value.name)
                ],
            }
            for item in sorted(objects, key=lambda value: (value.schema, value.name))
        ],
    }
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def capture_schema_evidence(
    query_spec: QuerySpec,
    loader: SchemaLoader | None = None,
) -> SchemaEvidence:
    """读取实际引用对象，保留无法精确绑定的标识符，不做自动替换。"""
    if loader is None:
        from app.db.sqlserver import read_schema_objects

        loader = read_schema_objects

    requested, required_columns = _referenced_identifiers(query_spec)
    loaded = loader(requested)
    database_name = str(loaded.get("database_name") or "").strip()
    if not database_name:
        raise ValueError("SQL Server 未返回当前数据库名称")

    raw_objects = loaded.get("objects", [])
    objects = [TableEvidence.model_validate(item) for item in raw_objects]
    exact_objects = {(item.schema, item.name): item for item in objects}
    available_objects = [
        str(item) for item in loaded.get("available_objects", [])
    ] or [f"{item.schema}.{item.name}" for item in objects]

    bound_objects = []
    unresolved = []
    for qualified in requested:
        schema, table = qualified
        identifier = f"{schema}.{table}"
        evidence = exact_objects.get(qualified)
        if evidence is None:
            unresolved.append(UnresolvedIdentifier(
                kind="object",
                identifier=identifier,
                reason="目标数据库中不存在完全匹配的 schema-qualified 对象",
                candidates=_matches(identifier, available_objects, 0.4),
            ))
            continue

        bound_objects.append(evidence)
        actual_columns = {item.name for item in evidence.columns}
        qualified_columns = [
            f"{schema}.{table}.{item.name}" for item in evidence.columns
        ]
        for column in sorted(required_columns.get(qualified, set())):
            if column in actual_columns:
                continue
            unresolved.append(UnresolvedIdentifier(
                kind="column",
                identifier=f"{identifier}.{column}",
                reason="目标对象中不存在完全匹配的字段",
                candidates=_matches(
                    f"{identifier}.{column}",
                    qualified_columns,
                    0.75,
                ),
            ))

    bound_objects.sort(key=lambda item: (item.schema, item.name))
    unresolved.sort(key=lambda item: (item.kind, item.identifier))
    return SchemaEvidence(
        database_name=database_name,
        captured_at=datetime.now(timezone.utc),
        objects=bound_objects,
        unresolved=unresolved,
        fingerprint=_fingerprint(database_name, bound_objects),
    )
