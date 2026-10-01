"""竞价市场权限审计服务。

敏感操作（建场次、报价、撤单、撮合、结算、场次撤销等）成功执行后，
以及越权/非法角色调用被拒绝时，统一写入 ``audit_logs``：
- 成功审计跟随业务操作，在业务事务内提交（业务失败整体回滚，不留假成功记录）；
- 拒绝审计使用独立小事务，保证即使业务层因越权直接拒绝，审计也一定落库。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.auction import AuditLog
from app.models.user import User


def record_audit(
    db: Session,
    user: User | None,
    action: str,
    *,
    target_type: str = "",
    target_id: int | None = None,
    detail: str = "",
    result: str = "success",
    ip: str = "",
    commit: bool = False,
) -> AuditLog:
    """写一条审计日志。

    ``commit=False``（默认）：加入当前业务事务，随业务一起提交/回滚；
    ``commit=True``：立即独立提交（用于越权拒绝等未进入业务事务的场景）。
    """
    log = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "anonymous",
        role=user.role if user else "",
        company_id=user.company_id if user else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=(detail or "")[:512],
        result=result,
        ip=(ip or "")[:45],
    )
    db.add(log)
    db.flush()
    if commit:
        db.commit()
        db.refresh(log)
    return log


def list_audit_logs(
    db: Session,
    *,
    action: str | None = None,
    company_id: int | None = None,
    target_type: str | None = None,
    result: str | None = None,
    limit: int = 200,
) -> list[AuditLog]:
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    if company_id is not None:
        q = q.filter(AuditLog.company_id == company_id)
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    if result:
        q = q.filter(AuditLog.result == result)
    return q.order_by(AuditLog.id.desc()).limit(max(1, min(limit, 1000))).all()
