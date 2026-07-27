"""SQL Server 目录快照：只读取系统目录，不读取业务数据。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone

from app.contracts.schema import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogObject,
    CatalogSnapshot,
)


ConnectionFactory = Callable[..., object]


def catalog_fingerprint(snapshot: CatalogSnapshot) -> str:
    """计算仅由数据库身份和目录结构决定的稳定指纹。"""
    payload = snapshot.model_dump(
        mode="json",
        by_alias=True,
        exclude={"captured_at"},
    )
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows(cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def capture_catalog_snapshot(
    connection_factory: ConnectionFactory | None = None,
) -> CatalogSnapshot:
    """从当前 SQL Server 连接捕获完整且可验证的物理目录。"""
    if connection_factory is None:
        from app.db.sqlserver import get_connection

        connection_factory = get_connection

    connection = connection_factory()
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
                d.compatibility_level,
                CONVERT(sysname, DATABASEPROPERTYEX(DB_NAME(), 'Collation'))
                    AS database_collation,
                COALESCE(SCHEMA_NAME(), 'dbo') AS default_schema,
                CURRENT_USER AS [current_user],
                HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')
                    AS can_read_catalog
            FROM sys.databases AS d
            WHERE d.database_id = DB_ID()
            """
        )
        environment = _rows(cursor)
        if len(environment) != 1:
            raise RuntimeError("无法确定当前 SQL Server 数据库身份")
        info = environment[0]

        cursor.execute(
            """
            SELECT
                o.object_id,
                s.name AS schema_name,
                o.name AS object_name,
                CASE o.type WHEN 'U' THEN 'table' ELSE 'view' END AS object_type
            FROM sys.objects AS o
            JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            ORDER BY o.object_id
            """
        )
        object_rows = _rows(cursor)

        cursor.execute(
            """
            SELECT
                c.object_id,
                c.column_id,
                c.name AS column_name,
                t.name AS sql_type,
                c.max_length,
                c.precision,
                c.scale,
                CONVERT(bit, c.is_nullable) AS nullable,
                c.collation_name AS collation
            FROM sys.columns AS c
            JOIN sys.types AS t ON t.user_type_id = c.user_type_id
            JOIN sys.objects AS o ON o.object_id = c.object_id
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            ORDER BY c.object_id, c.column_id
            """
        )
        column_rows = _rows(cursor)

        cursor.execute(
            """
            SELECT
                i.object_id,
                i.index_id,
                CONVERT(bit, i.is_primary_key) AS is_primary_key,
                CONVERT(bit, i.is_unique) AS is_unique,
                ic.key_ordinal,
                ic.column_id
            FROM sys.indexes AS i
            JOIN sys.index_columns AS ic
              ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.objects AS o ON o.object_id = i.object_id
            WHERE o.type IN ('U', 'V')
              AND o.is_ms_shipped = 0
              AND ic.key_ordinal > 0
              AND (i.is_primary_key = 1 OR i.is_unique = 1)
            ORDER BY i.object_id, i.index_id, ic.key_ordinal
            """
        )
        key_rows = _rows(cursor)

        cursor.execute(
            """
            SELECT
                fk.name,
                fkc.parent_object_id,
                fkc.parent_column_id,
                fkc.referenced_object_id,
                fkc.referenced_column_id
            FROM sys.foreign_keys AS fk
            JOIN sys.foreign_key_columns AS fkc
              ON fkc.constraint_object_id = fk.object_id
            ORDER BY fk.object_id, fkc.constraint_column_id
            """
        )
        foreign_key_rows = _rows(cursor)
    finally:
        connection.close()

    columns_by_object: dict[int, list[CatalogColumn]] = {}
    for row in column_rows:
        object_id = int(row["object_id"])
        columns_by_object.setdefault(object_id, []).append(
            CatalogColumn(
                column_id=int(row["column_id"]),
                name=str(row["column_name"]),
                sql_type=str(row["sql_type"]),
                max_length=(
                    int(row["max_length"])
                    if row["max_length"] is not None else None
                ),
                precision=(
                    int(row["precision"])
                    if row["precision"] is not None else None
                ),
                scale=int(row["scale"]) if row["scale"] is not None else None,
                nullable=bool(row["nullable"]),
                collation=(
                    str(row["collation"]) if row["collation"] is not None else None
                ),
            )
        )

    indexes: dict[tuple[int, int], dict] = {}
    for row in key_rows:
        key = (int(row["object_id"]), int(row["index_id"]))
        item = indexes.setdefault(
            key,
            {
                "primary": bool(row["is_primary_key"]),
                "unique": bool(row["is_unique"]),
                "columns": [],
            },
        )
        item["columns"].append(int(row["column_id"]))

    objects = []
    for row in object_rows:
        object_id = int(row["object_id"])
        object_indexes = [
            item for (owner_id, _), item in indexes.items()
            if owner_id == object_id
        ]
        primary_key = next(
            (item["columns"] for item in object_indexes if item["primary"]),
            [],
        )
        unique_keys = [
            item["columns"] for item in object_indexes
            if item["unique"] and not item["primary"]
        ]
        objects.append(
            CatalogObject(
                schema=str(row["schema_name"]),
                name=str(row["object_name"]),
                object_id=object_id,
                object_type=str(row["object_type"]),
                columns=columns_by_object.get(object_id, []),
                primary_key=primary_key,
                unique_keys=unique_keys,
            )
        )

    foreign_keys = [
        CatalogForeignKey(
            name=str(row["name"]),
            parent_object_id=int(row["parent_object_id"]),
            parent_column_id=int(row["parent_column_id"]),
            referenced_object_id=int(row["referenced_object_id"]),
            referenced_column_id=int(row["referenced_column_id"]),
        )
        for row in foreign_key_rows
    ]
    return CatalogSnapshot(
        server_identity=str(info["server_identity"]),
        database_name=str(info["database_name"]),
        database_id=int(info["database_id"]),
        compatibility_level=int(info["compatibility_level"]),
        database_collation=str(info["database_collation"]),
        default_schema=str(info["default_schema"]),
        current_user=str(info["current_user"]),
        can_read_catalog=bool(info["can_read_catalog"]),
        captured_at=datetime.now(timezone.utc),
        objects=objects,
        foreign_keys=foreign_keys,
    )
