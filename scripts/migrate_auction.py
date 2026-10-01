"""为已存在的数据库补齐碳配额集中竞价市场所需的表与列。

用法：python scripts/migrate_auction.py（可重复执行）

新增 4 张表（建表由 SQLAlchemy 元数据完成，已存在则跳过）：
- auction_sessions：竞价场次（申报/撮合/结算/撤销状态机、统一成交价）
- auction_bids：买卖报价单（卖单占用 reserved_balance，幂等键唯一）
- auction_trades：撮合成交记录（pending → settled）
- audit_logs：竞价市场权限审计日志

列变更：
- allowance_transactions 增加 auction_trade_id，关联集中竞价成交流水。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import Base, engine  # noqa: E402
# 触发全部模型注册，使新表进入 metadata.create_all
from app.models import (  # noqa: F401,E402
    AllowanceTransaction,
    AuctionBid,
    AuctionSession,
    AuctionTrade,
    AuditLog,
)


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def main():
    created_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    created_after = set(inspect(engine).get_table_names())
    new_tables = sorted(created_after - created_before)

    inspector = inspect(engine)
    statements: list[str] = []
    tables = set(inspector.get_table_names())

    if "allowance_transactions" in tables and not _has_column(
        inspector, "allowance_transactions", "auction_trade_id"
    ):
        statements.append(
            "ALTER TABLE allowance_transactions ADD COLUMN auction_trade_id INTEGER"
        )
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_allowance_transactions_auction_trade_id "
            "ON allowance_transactions (auction_trade_id)"
        )

    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                print(f"执行：{stmt}")
                conn.execute(text(stmt))

    expected = {"auction_sessions", "auction_bids", "auction_trades", "audit_logs"}
    print(f"竞价市场表：{', '.join(sorted(expected))}")
    print(f"新建表：{', '.join(new_tables) if new_tables else '无（均已存在）'}")
    print(f"列变更：{len(statements)} 项；迁移完成")


if __name__ == "__main__":
    main()
