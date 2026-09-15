"""RBAC 绑定/授权/拒绝事件的 SQLite 存储（v0.52）。

复用 StateDatabase 的连接与 BEGIN IMMEDIATE 事务原语；表结构由
state_db.SCHEMA 统一创建，此处只做行级读写。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Protocol

from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.rbac.models import CrossTenantGrant, RbacDenial, Role, RoleBinding


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


_ANY_TENANT = object()


class RoleBindingStore(Protocol):
    """RBAC 存储协议，便于测试替换。"""

    def list_bindings(self, principal: str) -> list[RoleBinding]: ...
    def list_all_bindings(
        self, tenant_id: str | None | object = _ANY_TENANT
    ) -> list[RoleBinding]: ...
    def add_binding(
        self, principal: str, tenant_id: str | None, role: Role, granted_by: str
    ) -> RoleBinding: ...
    def revoke_binding(self, binding_id: str, revoked_by: str) -> bool: ...
    def list_grants(self, source_principal: str, source_tenant: str | None) -> list[CrossTenantGrant]: ...
    def list_all_grants(self) -> list[CrossTenantGrant]: ...
    def add_grant(
        self,
        source_principal: str,
        source_tenant: str,
        target_tenant: str,
        resources: tuple[str, ...],
        granted_by: str,
    ) -> CrossTenantGrant: ...
    def revoke_grant(self, grant_id: str, revoked_by: str) -> bool: ...
    def record_denial(self, denial: RbacDenial) -> None: ...


class SqliteRoleBindingStore:
    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    def list_bindings(self, principal: str) -> list[RoleBinding]:
        try:
            with self._db._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM rbac_role_bindings
                       WHERE principal = ? AND revoked_at IS NULL
                       ORDER BY created_at""",
                    (principal,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 RBAC 绑定失败: {exc}") from exc
        return [self._binding_from_row(row) for row in rows]

    def list_all_bindings(
        self, tenant_id: str | None | object = _ANY_TENANT
    ) -> list[RoleBinding]:
        """列出未吊销绑定；省略 tenant_id 时不过滤，显式值按租户过滤。"""
        try:
            with self._db._connect() as conn:
                if tenant_id is _ANY_TENANT:
                    rows = conn.execute(
                        """SELECT * FROM rbac_role_bindings
                           WHERE revoked_at IS NULL ORDER BY created_at"""
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM rbac_role_bindings
                           WHERE tenant_id IS ? AND revoked_at IS NULL
                           ORDER BY created_at""",
                        (tenant_id,),
                    ).fetchall()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 RBAC 绑定失败: {exc}") from exc
        return [self._binding_from_row(row) for row in rows]

    def add_binding(
        self, principal: str, tenant_id: str | None, role: Role, granted_by: str
    ) -> RoleBinding:
        binding = RoleBinding(
            binding_id=uuid.uuid4().hex,
            principal=principal,
            tenant_id=tenant_id,
            role=role,
            granted_by=granted_by,
            created_at=_utc_now(),
        )
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                conn.execute(
                    """INSERT INTO rbac_role_bindings (
                       binding_id, principal, tenant_id, role, granted_by, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (binding.binding_id, principal, tenant_id, role.value,
                     granted_by, binding.created_at),
                )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"写入 RBAC 绑定失败: {exc}") from exc
        return binding

    def revoke_binding(self, binding_id: str, revoked_by: str) -> bool:
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                cur = conn.execute(
                    """UPDATE rbac_role_bindings SET revoked_at = ?
                       WHERE binding_id = ? AND revoked_at IS NULL""",
                    (_utc_now(), binding_id),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"吊销 RBAC 绑定失败: {exc}") from exc

    def list_grants(self, source_principal: str, source_tenant: str | None) -> list[CrossTenantGrant]:
        try:
            with self._db._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM rbac_cross_tenant_grants
                       WHERE source_principal = ? AND source_tenant IS ? AND revoked_at IS NULL
                       ORDER BY created_at""",
                    (source_principal, source_tenant),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询跨租户授权失败: {exc}") from exc
        return [self._grant_from_row(row) for row in rows]

    def list_all_grants(self) -> list[CrossTenantGrant]:
        try:
            with self._db._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM rbac_cross_tenant_grants
                       WHERE revoked_at IS NULL ORDER BY created_at"""
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询跨租户授权失败: {exc}") from exc
        return [self._grant_from_row(row) for row in rows]

    def add_grant(
        self,
        source_principal: str,
        source_tenant: str,
        target_tenant: str,
        resources: tuple[str, ...],
        granted_by: str,
    ) -> CrossTenantGrant:
        grant = CrossTenantGrant(
            grant_id=uuid.uuid4().hex,
            source_principal=source_principal,
            source_tenant=source_tenant,
            target_tenant=target_tenant,
            resources=resources,
            granted_by=granted_by,
            created_at=_utc_now(),
        )
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                conn.execute(
                    """INSERT INTO rbac_cross_tenant_grants (
                       grant_id, source_principal, source_tenant, target_tenant,
                       resources_json, granted_by, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (grant.grant_id, source_principal, source_tenant, target_tenant,
                     json.dumps(list(resources), sort_keys=True), granted_by, grant.created_at),
                )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"写入跨租户授权失败: {exc}") from exc
        return grant

    def revoke_grant(self, grant_id: str, revoked_by: str) -> bool:
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                cur = conn.execute(
                    """UPDATE rbac_cross_tenant_grants SET revoked_at = ?
                       WHERE grant_id = ? AND revoked_at IS NULL""",
                    (_utc_now(), grant_id),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"吊销跨租户授权失败: {exc}") from exc

    def record_denial(self, denial: RbacDenial) -> None:
        try:
            with self._db._connect() as conn:
                conn.execute(
                    """INSERT INTO rbac_denials (
                       denial_id, actor, endpoint, required_permission,
                       principal_tenant, reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (denial.denial_id, denial.actor, denial.endpoint,
                     denial.required_permission, denial.principal_tenant,
                     denial.reason, denial.created_at),
                )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"记录 RBAC 拒绝事件失败: {exc}") from exc

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> RoleBinding:
        return RoleBinding(
            binding_id=row["binding_id"],
            principal=row["principal"],
            tenant_id=row["tenant_id"],
            role=Role(row["role"]),
            granted_by=row["granted_by"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _grant_from_row(row: sqlite3.Row) -> CrossTenantGrant:
        return CrossTenantGrant(
            grant_id=row["grant_id"],
            source_principal=row["source_principal"],
            source_tenant=row["source_tenant"],
            target_tenant=row["target_tenant"],
            resources=tuple(json.loads(row["resources_json"])),
            granted_by=row["granted_by"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )
