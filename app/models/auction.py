"""碳配额集中竞价市场模型。

- AuctionSession：监管创建的竞价场次（申报窗口、价格边界、统一成交价与撮合/结算状态）；
- AuctionBid：买卖双方报价单。卖方报价成功即把对应数量从自由可用配额转为交易占用
  （allowance_accounts.reserved_balance），撤单/未成交/场次撤销时释放，成交后随结算出库；
- AuctionTrade：撮合产生的成交记录（统一成交价），结算后配额账户与流水同步落账；
- AuditLog：监管/企业在竞价市场全部敏感操作的权限审计（含越权拒绝）。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)

from app.core.database import Base


class AuctionSession(Base):
    """集中竞价场次。

    状态机：
    - open：申报中，买/卖方可报价、可撤单；
    - matched：已撮合并产生统一成交价与成交记录，等待结算（不能再报价/撤单）；
    - settled：已结算，配额账户、交易流水与买方履约缺口全部回写完成，终态；
    - cancelled：申报截止前由监管撤销，全部卖方占用配额释放，终态。
    """

    __tablename__ = "auction_sessions"

    id = Column(Integer, primary_key=True)
    session_no = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False, default="")
    year = Column(Integer, nullable=False, index=True)
    # open/matched/settled/cancelled
    status = Column(String(16), nullable=False, default="open", index=True)
    price_floor = Column(Numeric(18, 2), nullable=False, default=0)   # 申报价下限（元/t，0 表示不限）
    price_ceiling = Column(Numeric(18, 2), nullable=False, default=0)  # 申报价上限（元/t，0 表示不限）
    bid_start_at = Column(DateTime, nullable=True)
    bid_end_at = Column(DateTime, nullable=True)
    clear_price = Column(Numeric(18, 2), nullable=True)               # 统一成交价格（撮合成交后写入）
    matched_volume = Column(Numeric(18, 4), nullable=False, default=0)  # 撮合总量
    settled_volume = Column(Numeric(18, 4), nullable=False, default=0)  # 已结算总量
    remark = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    matched_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuctionBid(Base):
    """竞价报价单（买单/卖单）。

    状态：
    - active：申报有效（卖单对应数量已转为交易占用），场次 open 期间可撤销；
    - filled：全部成交；partial：部分成交；unfilled：未成交（结算后占用已释放）；
    - cancelled：申报阶段撤单（卖单占用立即释放）。

    ``filled_quantity`` 在撮合时写入；状态在结算时统一终结（matched 场次不可再改单）。
    """

    __tablename__ = "auction_bids"
    __table_args__ = (
        # 建单请求幂等：双击/重试同键只产生一张报价单（NULL 不参与唯一约束）
        UniqueConstraint("idempotency_key", name="uq_auction_bid_idem"),
        Index("ix_auction_bids_session_side_price", "session_id", "side", "price"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    side = Column(String(8), nullable=False)                 # buy/sell
    price = Column(Numeric(18, 2), nullable=False)           # 报价（元/t）
    quantity = Column(Numeric(18, 4), nullable=False)        # 申报数量（tCO2）
    filled_quantity = Column(Numeric(18, 4), nullable=False, default=0)  # 成交数量（撮合后写入）
    # active/filled/partial/unfilled/cancelled
    status = Column(String(16), nullable=False, default="active", index=True)
    remark = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuctionTrade(Base):
    """集中竞价成交记录：撮合成交（pending）→ 完成结算（settled）。"""

    __tablename__ = "auction_trades"
    __table_args__ = (
        Index("ix_auction_trades_session", "session_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    trade_no = Column(String(32), nullable=False, unique=True, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    buy_bid_id = Column(Integer, ForeignKey("auction_bids.id"), nullable=False)
    sell_bid_id = Column(Integer, ForeignKey("auction_bids.id"), nullable=False)
    buyer_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    price = Column(Numeric(18, 2), nullable=False, default=0)   # 统一成交价（元/t）
    quantity = Column(Numeric(18, 4), nullable=False)           # 成交量（tCO2）
    # pending=已撮合待结算 / settled=配额与流水已落账
    status = Column(String(16), nullable=False, default="pending", index=True)
    settled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuditLog(Base):
    """竞价市场权限审计：敏感操作的操作人、对象、结果（成功/拒绝）与时间。"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(64), nullable=False, default="")
    role = Column(String(16), nullable=False, default="")
    company_id = Column(Integer, nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)    # 如 auction.create / auction.bid / auction.settle / auction.denied
    target_type = Column(String(16), nullable=False, default="")  # session/bid/trade
    target_id = Column(Integer, nullable=True)
    detail = Column(String(512), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success", index=True)  # success/denied
    ip = Column(String(45), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
