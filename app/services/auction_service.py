"""碳配额集中竞价市场：场次管理、报价、撤单、集合竞价撮合与统一结算。

业务状态机
==========
场次 AuctionSession：
- open（申报中）：买/卖方可报价或撤单。卖单报价成功即把对应数量从“自由可用
  配额”转为交易占用 reserved（与企业间订单共用同一占用账本），成交后随结算
  出库，撤单/未成交/场次撤销时释放；
- matched（已撮合）：监管触发集合竞价，按“最大成交量原则”确定统一成交价并
  生成成交记录；此阶段不可再报价/撤单；
- settled（已结算，终态）：监管触发结算，全部成交在单一事务内完成卖方出库、
  买方到账与双方流水，并按买方合并自动核销其同年度履约缺口；未成交卖单占用
  同事务释放；
- cancelled（已撤销，终态）：仅申报中可由监管撤销，全部有效卖单占用释放。

报价单 AuctionBid：active →（撮合）filled/partial →（结算终态）；
active 且未成交在结算后变 unfilled；open 期间撤单为 cancelled。

集合竞价定价
============
对每个候选价 p：需求 D(p)=所有报价≥p 的买单量之和；供给 S(p)=所有报价≤p
的卖单量之和；可成交量 V(p)=min(D,S)。选取 V 最大的价格；若多个价格并列：
1) 取 |D-S| 最小者（最小未平衡量）；2) 仍并列取最接近参考价者（上一场同年度
成交价 → 价格边界中点 → 最高买/最低卖中点）。无任何可成交量时不产生成交。

配对在统一成交价下按“价格优先、时间优先（同价按报价单 id 先报先得）”顺序
逐笔撮合，生成买卖双方一一对应的成交记录。

并发安全
========
- 场次键 ``auction:<id>`` 串行化同一场次的全部写操作；
- 报价/撤单/结算涉及账户时按 ``account: < auction: < clear: < order:``
  全局锁序收集键后统一加锁，跨企业多账户结算不会形成锁环；
- 撮合 open→matched、结算 matched→settled、撤销 open→cancelled 均使用
  “状态必须为前置状态”的条件 UPDATE 抢占，多进程/多线程并发只有一方成功；
- 卖方占用、卖方出库、买方到账、占用释放、流水、履约核销、成交与场次状态在
  同一事务提交，任一失败整体回滚，绝不留半成品账。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    auction_session_key,
    auction_write_key,
    company_clear_key,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    transactional,
)
from app.models.allowance import AllowanceAccount, AllowanceTransaction
from app.models.auction import AuctionBid, AuctionSession, AuctionTrade
from app.models.company import Company
from app.services.quota_service import settle_buyer_deficit_on_auction

# 场次状态
OPEN = "open"
MATCHED = "matched"
SETTLED = "settled"
CANCELLED = "cancelled"

# 报价单状态
ACTIVE = "active"
FILLED = "filled"
PARTIAL = "partial"
UNFILLED = "unfilled"
BID_CANCELLED = "cancelled"

# 成交记录状态
PENDING = "pending"
TRADE_SETTLED = "settled"

BUY = "buy"
SELL = "sell"


class AuctionError(ValueError):
    """竞价业务规则不满足（场次状态非法、越权、报价超限、可用不足等）。"""


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _get_session(db: Session, session_id: int) -> AuctionSession:
    session = db.get(AuctionSession, session_id)
    if session is None:
        raise AuctionError("竞价场次不存在")
    return session


def _reload_in_lock(db: Session, session_id: int) -> AuctionSession:
    """获取场次锁后重新加载场次：丢弃锁等待期间可能已过期的身份映射快照。

    线程在等锁前读过的 OPEN/MATCHED 状态可能已被持锁提交改变，直接复查会看到
    陈旧状态而错误进入流转；expire 后重新查询才能以锁内最新状态做判断。
    """
    db.expire_all()
    return _get_session(db, session_id)


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise AuctionError(f"企业 {company_id} 的 {year} 年度配额账户不存在，请先完成配额分配")
    return account


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _account_keys(db: Session, company_ids, year: int) -> list[str]:
    """收集一组企业在某年度的账户键与清缴键（账户不存在时先报错）。"""
    keys: list[str] = []
    for company_id in set(company_ids):
        account = _get_account(db, company_id, year)
        keys.append(account_lock_key(account.id))
        keys.append(company_clear_key(company_id, year))
    return keys


def _gen_session_no(db: Session, year: int) -> str:
    count = db.query(AuctionSession).filter(AuctionSession.year == year).count()
    return f"AS{year}{count + 1:06d}"


def _gen_trade_no(db: Session, session: AuctionSession, seq: int) -> str:
    return f"AT{session.id:06d}{seq:04d}"


# ---------------------------------------------------------------------------
# 场次管理
# ---------------------------------------------------------------------------


def create_session(
    db: Session,
    year: int,
    *,
    name: str = "",
    price_floor: float = 0.0,
    price_ceiling: float = 0.0,
    bid_start_at: datetime | None = None,
    bid_end_at: datetime | None = None,
    remark: str = "",
    created_by: int | None = None,
) -> AuctionSession:
    """监管创建竞价场次（默认 open 申报中）。"""
    floor = round(float(price_floor or 0), 2)
    ceiling = round(float(price_ceiling or 0), 2)
    if floor < 0 or ceiling < 0:
        raise AuctionError("价格边界不能为负数")
    if floor > 0 and ceiling > 0 and floor > ceiling:
        raise AuctionError("价格下限不能高于上限")
    if bid_start_at and bid_end_at and bid_start_at >= bid_end_at:
        raise AuctionError("申报开始时间必须早于截止时间")

    with transactional(db):
        session = AuctionSession(
            session_no=_gen_session_no(db, year),
            name=(name or "").strip()[:128],
            year=year,
            status=OPEN,
            price_floor=floor,
            price_ceiling=ceiling,
            bid_start_at=bid_start_at,
            bid_end_at=bid_end_at,
            remark=(remark or "").strip()[:256],
            created_by=created_by,
        )
        db.add(session)
        db.flush()
        db.refresh(session)
    return session


def _transit_session(
    db: Session, session_id: int, expected: tuple[str, ...], new_status: str
) -> int:
    """场次状态条件 UPDATE：仅当前置状态命中时流转，返回影响行数。

    并发撮合/结算/撤销由数据库行更新做最后抢占，杜绝重复撮合/重复结算。
    """
    result = db.execute(
        update(AuctionSession)
        .where(AuctionSession.id == session_id)
        .where(AuctionSession.status.in_(expected))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def cancel_session(db: Session, session_id: int, operator_id: int, reason: str = "") -> AuctionSession:
    """监管撤销申报中场次：释放全部有效卖单的交易占用，报价单统一撤销。

    matched/settled 场次不可撤销；重复撤销幂等返回当前场次。
    """
    session = _get_session(db, session_id)
    if session.status in (SETTLED, CANCELLED):
        db.refresh(session)
        return session
    if session.status == MATCHED:
        raise AuctionError("场次已撮合并进入待结算状态，不能撤销，请执行结算")

    active_bids = (
        db.query(AuctionBid)
        .filter(AuctionBid.session_id == session_id, AuctionBid.status == ACTIVE)
        .all()
    )
    seller_ids = [b.company_id for b in active_bids if b.side == SELL]
    keys = [auction_write_key(), auction_session_key(session_id)] + _account_keys(db, seller_ids, session.year)

    with locked_accounts(keys):
        session = _reload_in_lock(db, session_id)
        if session.status in (SETTLED, CANCELLED):
            db.refresh(session)
            return session
        if session.status != OPEN:
            raise AuctionError("仅申报中的场次可以撤销")

        reason = (reason or "").strip()[:256]
        try:
            with transactional(db):
                if _transit_session(db, session_id, (OPEN,), CANCELLED) != 1:
                    raise AuctionError("场次状态已变化，撤销失败，请刷新后重试")

                # 释放全部有效卖单占用（open 阶段 filled_quantity 恒为 0，整单释放）
                seller_totals: dict[int, float] = {}
                for bid in active_bids:
                    if bid.side != SELL:
                        continue
                    seller_totals[bid.company_id] = round(
                        seller_totals.get(bid.company_id, 0.0) + float(bid.quantity), 4
                    )

                for company_id, amount in seller_totals.items():
                    account = lock_row_for_write(db, _get_account(db, company_id, session.year).id)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, account.id, 0, 0, -amount
                    )
                    db.add(
                        AllowanceTransaction(
                            account_id=account.id,
                            company_id=company_id,
                            tx_type="auction_release",
                            amount=round(amount, 4),
                            counterparty=f"集中竞价 {session.session_no}",
                            price=None,
                            tx_date=_today(),
                            balance_after=round(balance_after, 4),
                            frozen_after=round(frozen_after, 4),
                            reserved_after=round(reserved_after, 4),
                            remark=f"场次 {session.session_no} 撤销，释放卖单占用配额 {amount} 吨",
                        )
                    )

                for bid in active_bids:
                    bid.status = BID_CANCELLED
                    bid.cancel_reason = reason or "场次撤销"
                    bid.cancelled_by = operator_id
                    bid.cancelled_at = datetime.utcnow()

                session.cancel_reason = reason
                session.cancelled_by = operator_id
                session.cancelled_at = datetime.utcnow()
                db.flush()
                db.refresh(session)
        except InsufficientBalanceError:
            raise AuctionError("释放卖单占用失败，账本状态异常，场次撤销已回滚")
        return session


# ---------------------------------------------------------------------------
# 报价与撤单
# ---------------------------------------------------------------------------


def place_bid(
    db: Session,
    session_id: int,
    company_id: int,
    side: str,
    price: float,
    quantity: float,
    *,
    remark: str = "",
    idempotency_key: str | None = None,
    created_by: int | None = None,
) -> AuctionBid:
    """买方/卖方在申报中场次报价。

    卖单报价成功即原子占用自由可用配额（持仓不变，reserved 增加），
    履约冻结与其他订单/场次占用的配额不能重复申报卖出；买单只登记意向。
    同一企业同一场次不得同时持有买单与卖单（防止自买自卖操纵价格）。
    """
    if side not in (BUY, SELL):
        raise AuctionError("报价方向非法（buy/sell）")
    quantity = round(float(quantity), 4)
    price = round(float(price), 2)
    if quantity <= 0:
        raise AuctionError("申报数量必须为正数")
    if price < 0:
        raise AuctionError("申报价格不能为负数")
    if not db.get(Company, company_id):
        raise AuctionError("报价企业不存在")

    session = _get_session(db, session_id)
    if session.status != OPEN:
        raise AuctionError("场次不在申报中，不能提交报价")
    if float(session.price_floor or 0) > 0 and price < float(session.price_floor):
        raise AuctionError(f"申报价格低于场次价格下限 {session.price_floor} 元/t")
    if float(session.price_ceiling or 0) > 0 and price > float(session.price_ceiling):
        raise AuctionError(f"申报价格高于场次价格上限 {session.price_ceiling} 元/t")

    account = _get_account(db, company_id, session.year)
    keys = [auction_write_key(), auction_session_key(session_id), account_lock_key(account.id),
            company_clear_key(company_id, session.year)]

    with locked_accounts(keys):
        session = _reload_in_lock(db, session_id)
        if idempotency_key:
            existing = (
                db.query(AuctionBid)
                .filter(AuctionBid.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        # 锁内复查场次状态：等锁期间可能已被撮合/撤销
        if session.status != OPEN:
            raise AuctionError("场次不在申报中，不能提交报价")

        # 同场次防对敲：已有反向有效报价时拒绝
        opposite = (
            db.query(AuctionBid.id)
            .filter(
                AuctionBid.session_id == session_id,
                AuctionBid.company_id == company_id,
                AuctionBid.side == (SELL if side == BUY else BUY),
                AuctionBid.status == ACTIVE,
            )
            .first()
        )
        if opposite:
            raise AuctionError("本企业已在该场次持有反向报价，不能同时买卖")

        try:
            with transactional(db):
                db.flush()
                if side == SELL:
                    # 抢占账户行写锁后校验自由可用，占用 UPDATE 由数据库兜底
                    account = lock_row_for_write(db, account.id)
                    available = round(
                        float(account.current_balance)
                        - float(account.frozen_balance)
                        - float(account.reserved_balance),
                        4,
                    )
                    if available < quantity:
                        raise InsufficientBalanceError("卖方可用配额不足")

                bid = AuctionBid(
                    session_id=session_id,
                    company_id=company_id,
                    year=session.year,
                    side=side,
                    price=price,
                    quantity=quantity,
                    filled_quantity=0,
                    status=ACTIVE,
                    remark=(remark or "").strip()[:256],
                    created_by=created_by,
                    idempotency_key=idempotency_key,
                )
                db.add(bid)
                db.flush()

                if side == SELL:
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, account.id, 0, 0, quantity
                    )
                    db.add(
                        AllowanceTransaction(
                            account_id=account.id,
                            company_id=company_id,
                            tx_type="auction_reserve",
                            amount=quantity,
                            counterparty=f"集中竞价 {session.session_no}",
                            price=price,
                            tx_date=_today(),
                            balance_after=round(balance_after, 4),
                            frozen_after=round(frozen_after, 4),
                            reserved_after=round(reserved_after, 4),
                            remark=(
                                f"场次 {session.session_no} 卖单申报，交易占用配额 {quantity} 吨"
                            ),
                        )
                    )
                db.refresh(bid)
        except IntegrityError as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(AuctionBid)
                    .filter(AuctionBid.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        except InsufficientBalanceError:
            raise AuctionError(
                f"卖方自由可用配额不足（需 {quantity} 吨，已扣除履约冻结与全部交易占用）"
            )
        return bid


def cancel_bid(
    db: Session,
    bid_id: int,
    operator_company_id: int | None,
    reason: str = "",
) -> AuctionBid:
    """撤销报价单：open 期间报价企业可撤自己的单；监管可撤销任意单。

    ``operator_company_id`` 为 None 表示监管操作；否则必须与报价单归属一致。
    卖单撤单立即释放其交易占用；已成交部分（open 阶段恒为 0）不在此处理。
    """
    bid = db.get(AuctionBid, bid_id)
    if bid is None:
        raise AuctionError("报价单不存在")
    if operator_company_id is not None and bid.company_id != operator_company_id:
        raise AuctionError("无权撤销其他企业的报价单")

    session = _get_session(db, bid.session_id)
    if session.status != OPEN:
        raise AuctionError("场次已截止申报，报价单不能撤销")
    if bid.status != ACTIVE:
        db.refresh(bid)
        return bid

    account = _get_account(db, bid.company_id, bid.year)
    keys = [auction_write_key(), auction_session_key(session.id), account_lock_key(account.id),
            company_clear_key(bid.company_id, bid.year)]

    with locked_accounts(keys):
        db.expire_all()
        bid = db.get(AuctionBid, bid_id)
        session = _get_session(db, bid.session_id)
        if bid.status != ACTIVE:
            db.refresh(bid)
            return bid
        if session.status != OPEN:
            raise AuctionError("场次已截止申报，报价单不能撤销")

        reason = (reason or "").strip()[:256]
        try:
            with transactional(db):
                # 事务内原子复查：报价单仍为 active 且场次仍为 open 才允许撤销。
                # 锁外检查与本 UPDATE 之间可能恰好被撮合 open→matched，
                # 条件 UPDATE 行锁是最后防线，杜绝“已撮合报价仍被撤单”的竞态。
                if _transit_session(db, bid.session_id, (OPEN,), OPEN) != 1:
                    raise AuctionError("场次已截止申报，报价单不能撤销")
                result = db.execute(
                    update(AuctionBid)
                    .where(AuctionBid.id == bid_id, AuctionBid.status == ACTIVE)
                    .values(status=BID_CANCELLED)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise AuctionError("报价单状态已变化，撤单失败，请刷新后重试")

                if bid.side == SELL:
                    account = lock_row_for_write(db, account.id)
                    amount = round(float(bid.quantity), 4)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, account.id, 0, 0, -amount
                    )
                    db.add(
                        AllowanceTransaction(
                            account_id=account.id,
                            company_id=bid.company_id,
                            tx_type="auction_release",
                            amount=amount,
                            counterparty=f"集中竞价 {session.session_no}",
                            price=round(float(bid.price), 2),
                            tx_date=_today(),
                            balance_after=round(balance_after, 4),
                            frozen_after=round(frozen_after, 4),
                            reserved_after=round(reserved_after, 4),
                            remark=f"场次 {session.session_no} 卖单撤销，释放占用配额 {amount} 吨",
                        )
                    )

                bid.status = BID_CANCELLED
                bid.cancel_reason = reason
                bid.cancelled_by = operator_company_id
                bid.cancelled_at = datetime.utcnow()
                db.flush()
                db.refresh(bid)
        except InsufficientBalanceError:
            raise AuctionError("释放交易占用失败，账本状态异常，撤单已回滚")
        return bid


# ---------------------------------------------------------------------------
# 集合竞价撮合
# ---------------------------------------------------------------------------


def _aggregate_curve(bids: list[AuctionBid], side: str) -> dict[float, float]:
    """按价位汇总有效报价量：{价格: 该价位申报总量}。"""
    curve: dict[float, float] = {}
    for bid in bids:
        if bid.side != side:
            continue
        curve[round(float(bid.price), 2)] = round(
            curve.get(round(float(bid.price), 2), 0.0) + float(bid.quantity), 4
        )
    return curve


def _reference_price(
    db: Session,
    session: AuctionSession,
    highest_buy: float,
    lowest_sell: float,
) -> float:
    """并列成交价时的参考价：上一场同年度成交价 → 边界中点 → 买卖中点。"""
    prev = (
        db.query(AuctionSession)
        .filter(
            AuctionSession.year == session.year,
            AuctionSession.status == SETTLED,
            AuctionSession.clear_price.isnot(None),
            AuctionSession.id != session.id,
        )
        .order_by(AuctionSession.settled_at.desc())
        .first()
    )
    if prev is not None:
        return float(prev.clear_price)
    floor = float(session.price_floor or 0)
    ceiling = float(session.price_ceiling or 0)
    if floor > 0 and ceiling > 0:
        return round((floor + ceiling) / 2, 2)
    return round((highest_buy + lowest_sell) / 2, 2)


def _determine_clear_price(
    db: Session, session: AuctionSession, active_bids: list[AuctionBid]
) -> tuple[float | None, float]:
    """最大成交量原则确定统一成交价，返回 ``(成交价, 成交量)``；无成交返回 (None, 0)。"""
    buy_curve = _aggregate_curve(active_bids, BUY)
    sell_curve = _aggregate_curve(active_bids, SELL)
    if not buy_curve or not sell_curve:
        return None, 0.0

    candidates = sorted(set(buy_curve) | set(sell_curve))
    buy_prices = sorted(buy_curve, reverse=True)
    sell_prices = sorted(sell_curve)

    def demand(p: float) -> float:
        return round(sum(q for price, q in buy_curve.items() if price + 1e-9 >= p), 4)

    def supply(p: float) -> float:
        return round(sum(q for price, q in sell_curve.items() if price <= p + 1e-9), 4)

    evaluated = [(p, demand(p), supply(p)) for p in candidates]
    feasible = [(p, d, s, round(min(d, s), 4)) for p, d, s in evaluated]
    best_volume = max(v for _, _, _, v in feasible)
    if best_volume <= 0:
        return None, 0.0

    # 第一优先：可成交量最大；第二优先：买卖未平衡量最小
    tied = [(p, d, s) for p, d, s, v in feasible if abs(v - best_volume) < 1e-9]
    min_imbalance = min(abs(d - s) for _, d, s in tied)
    tied = [(p, d, s) for p, d, s in tied if abs(abs(d - s) - min_imbalance) < 1e-9]
    if len(tied) == 1:
        return tied[0][0], best_volume

    # 第三优先：最接近参考价
    reference = _reference_price(db, session, buy_prices[0], sell_prices[0])
    best = min(tied, key=lambda item: (abs(item[0] - reference), item[0]))
    return best[0], best_volume


def match_session(db: Session, session_id: int) -> AuctionSession:
    """监管触发集合竞价：确定统一成交价并逐笔生成成交记录。

    撮合只调整报价单成交数量、场次状态与成交记录，不动任何配额账户；
    资金/配额交割统一在 :func:`settle_session` 完成。
    open→matched 条件抢占保证并发撮合只有一方生效。
    """
    session = _get_session(db, session_id)
    if session.status in (MATCHED, SETTLED):
        db.refresh(session)
        return session
    if session.status == CANCELLED:
        raise AuctionError("场次已撤销，不能撮合")

    # 撮合必须与撤单/报价完全互斥：除场次键外，还要持有全部报价企业的账户键与
    # 清缴键（与 place_bid/cancel_bid/settle_session 同一锁集合，内部排序去重）。
    # 否则撮合在锁外读到的 active 报价单可能在其事务提交前被撤单释放占用，
    # 造成“已撤销卖单被撮合成交”的脏账。
    bid_company_ids = [
        row[0]
        for row in db.query(AuctionBid.company_id)
        .filter(AuctionBid.session_id == session_id, AuctionBid.status == ACTIVE)
        .distinct()
        .all()
    ]
    keys = [auction_write_key(), auction_session_key(session_id)] + _account_keys(db, bid_company_ids, session.year)

    with locked_accounts(keys):
        session = _reload_in_lock(db, session_id)
        if session.status in (MATCHED, SETTLED):
            db.refresh(session)
            return session
        if session.status != OPEN:
            raise AuctionError("仅申报中的场次可以撮合")

        # 锁内做最终状态判断；有效报价在写事务内、抢到库级写锁之后才读取
        try:
            with transactional(db):
                # 1) 先做场次状态条件 UPDATE（第一条写语句，立即抢到 SQLite 库级
                #    写锁 / 行锁）：此后本事务读到的报价单在提交前不会被并发撤单改动。
                if _transit_session(db, session_id, (OPEN,), MATCHED) != 1:
                    raise AuctionError("场次状态已变化，撮合失败，请刷新后重试")

                # 2) 写锁内重读有效报价单，消除“读报价 → 抢写锁”之间的撤单窗口
                active_bids = (
                    db.query(AuctionBid)
                    .filter(AuctionBid.session_id == session_id, AuctionBid.status == ACTIVE)
                    .all()
                )
                clear_price, volume = _determine_clear_price(db, session, active_bids)
                session = _get_session(db, session_id)

                if volume <= 0 or clear_price is None:
                    session.clear_price = None
                    session.matched_volume = 0
                    session.matched_at = datetime.utcnow()
                    db.flush()
                    db.refresh(session)
                    return session

                # 统一成交价下按价格优先、时间优先排序逐笔配对
                eligible_sells = sorted(
                    (b for b in active_bids if b.side == SELL and float(b.price) <= float(clear_price) + 1e-9),
                    key=lambda b: (float(b.price), b.id),
                )
                eligible_buys = sorted(
                    (b for b in active_bids if b.side == BUY and float(b.price) + 1e-9 >= float(clear_price)),
                    key=lambda b: (-float(b.price), b.id),
                )
                remaining: dict[int, float] = {b.id: float(b.quantity) for b in active_bids}

                seq = 0
                buy_idx = 0
                for sell in eligible_sells:
                    sell_left = remaining[sell.id]
                    while sell_left > 1e-9 and buy_idx < len(eligible_buys):
                        buy = eligible_buys[buy_idx]
                        buy_left = remaining[buy.id]
                        if buy_left <= 1e-9:
                            buy_idx += 1
                            continue
                        qty = round(min(sell_left, buy_left), 4)
                        if qty <= 0:
                            break
                        seq += 1
                        db.add(
                            AuctionTrade(
                                trade_no=_gen_trade_no(db, session, seq),
                                session_id=session_id,
                                year=session.year,
                                buy_bid_id=buy.id,
                                sell_bid_id=sell.id,
                                buyer_id=buy.company_id,
                                seller_id=sell.company_id,
                                price=round(float(clear_price), 2),
                                quantity=qty,
                                status=PENDING,
                            )
                        )
                        remaining[sell.id] = round(sell_left - qty, 4)
                        remaining[buy.id] = round(buy_left - qty, 4)
                        sell_left = remaining[sell.id]
                        if remaining[buy.id] <= 1e-9:
                            buy_idx += 1

                total_matched = 0.0
                for bid in active_bids:
                    filled = round(float(bid.quantity) - remaining[bid.id], 4)
                    bid.filled_quantity = filled
                    if filled <= 0:
                        # 未成交单保留 active，结算时终结并（卖单）释放占用
                        bid.status = ACTIVE
                    elif filled + 1e-9 >= float(bid.quantity):
                        bid.status = FILLED
                    else:
                        bid.status = PARTIAL
                    total_matched = round(total_matched + (filled if bid.side == BUY else 0), 4)

                session.clear_price = round(float(clear_price), 2)
                session.matched_volume = total_matched
                session.matched_at = datetime.utcnow()
                db.flush()
                db.refresh(session)
        except AuctionError:
            raise
        return session


# ---------------------------------------------------------------------------
# 统一结算
# ---------------------------------------------------------------------------


def _add_settlement_tx(
    db: Session,
    account: AllowanceAccount,
    *,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    reserved_after: float,
    counterparty: str,
    remark: str,
    price: float,
    trade: AuctionTrade,
) -> None:
    db.add(
        AllowanceTransaction(
            account_id=account.id,
            company_id=account.company_id,
            tx_type=tx_type,
            amount=round(amount, 4),
            counterparty=counterparty,
            price=round(price, 2),
            tx_date=_today(),
            balance_after=round(balance_after, 4),
            frozen_after=round(frozen_after, 4),
            reserved_after=round(reserved_after, 4),
            auction_trade_id=trade.id,
            remark=remark,
        )
    )


def settle_session(db: Session, session_id: int) -> AuctionSession:
    """监管统一结算：成交配额出库/到账、流水、未成交占用释放与履约缺口回写。

    全部成交、全部释放与买方履约核销在单一事务、单一锁集合内完成：
    - 卖方：每笔成交 current/reserved 同减（占用转为真正出库）；
    - 买方：每笔成交 current 同增；
    - 卖方未成交（含部分成交剩余）占用逐企业释放；
    - 买方按企业合并：到账全部入账后自动核销其同年度履约缺口一次
      （先冻结核销、后到账补缴），复用清缴内核；
    - matched→settled 条件抢占保证并发结算只有一方成功。
    """
    session = _get_session(db, session_id)
    if session.status == SETTLED:
        db.refresh(session)
        return session
    if session.status == CANCELLED:
        raise AuctionError("场次已撤销，不能结算")
    if session.status != MATCHED:
        raise AuctionError("场次尚未撮合，不能结算")

    bids = (
        db.query(AuctionBid)
        .filter(AuctionBid.session_id == session_id)
        .filter(AuctionBid.status.in_([ACTIVE, FILLED, PARTIAL]))
        .all()
    )
    participant_ids = [b.company_id for b in bids]
    keys = [auction_write_key(), auction_session_key(session_id)] + _account_keys(db, participant_ids, session.year)

    with locked_accounts(keys):
        session = _reload_in_lock(db, session_id)
        if session.status == SETTLED:
            db.refresh(session)
            return session
        if session.status != MATCHED:
            raise AuctionError("场次未处于已撮合状态，不能结算")

        trades = (
            db.query(AuctionTrade)
            .filter(AuctionTrade.session_id == session_id, AuctionTrade.status == PENDING)
            .order_by(AuctionTrade.id.asc())
            .all()
        )
        buyer_bought: dict[int, float] = {}
        settled_volume = 0.0

        try:
            with transactional(db):
                if _transit_session(db, session_id, (MATCHED,), SETTLED) != 1:
                    raise AuctionError("场次状态已变化，结算失败，请刷新后重试")

                # 先 flush 状态抢占结果，避免后续 expire_all 丢弃待写入状态
                db.flush()

                for trade in trades:
                    seller_account = lock_row_for_write(
                        db, _get_account(db, trade.seller_id, session.year).id
                    )
                    buyer_account = lock_row_for_write(
                        db, _get_account(db, trade.buyer_id, session.year).id
                    )
                    qty = round(float(trade.quantity), 4)
                    price = round(float(trade.price), 2)
                    buyer_name = _company_name(db, trade.buyer_id)
                    seller_name = _company_name(db, trade.seller_id)

                    # 卖方：占用配额出库（持仓/占用同减，frozen 不变）
                    s_balance, s_frozen, s_reserved = apply_ledger_delta(
                        db, seller_account.id, -qty, 0, -qty
                    )
                    _add_settlement_tx(
                        db,
                        seller_account,
                        tx_type="auction_deliver_out",
                        amount=qty,
                        balance_after=s_balance,
                        frozen_after=s_frozen,
                        reserved_after=s_reserved,
                        counterparty=buyer_name,
                        remark=f"场次 {session.session_no} 竞价成交，向{buyer_name}划出 {qty} 吨",
                        price=price,
                        trade=trade,
                    )

                    # 买方：配额到账（不动 frozen，买方冻结义务由清缴核销处理）
                    b_balance, b_frozen, b_reserved = apply_ledger_delta(
                        db, buyer_account.id, qty, 0, 0
                    )
                    _add_settlement_tx(
                        db,
                        buyer_account,
                        tx_type="auction_deliver_in",
                        amount=qty,
                        balance_after=b_balance,
                        frozen_after=b_frozen,
                        reserved_after=b_reserved,
                        counterparty=seller_name,
                        remark=f"场次 {session.session_no} 竞价成交，从{seller_name}受让 {qty} 吨",
                        price=price,
                        trade=trade,
                    )

                    buyer_bought[trade.buyer_id] = round(
                        buyer_bought.get(trade.buyer_id, 0.0) + qty, 4
                    )
                    settled_volume = round(settled_volume + qty, 4)
                    # 注意：循环内 lock_row_for_write/apply_ledger_delta 会 expire_all，
                    # 不能用 ORM 属性赋值（会被随后的过期重载丢弃），成交状态在循环后
                    # 用 Core 条件 UPDATE 统一落库。

                # 释放卖单未成交占用（部分成交只释放剩余量；未成交卖单释放整单）
                release_totals: dict[int, float] = {}
                for bid in bids:
                    if bid.side != SELL:
                        continue
                    unfilled = round(float(bid.quantity) - float(bid.filled_quantity), 4)
                    if unfilled > 0:
                        release_totals[bid.company_id] = round(
                            release_totals.get(bid.company_id, 0.0) + unfilled, 4
                        )
                for company_id, amount in release_totals.items():
                    account = lock_row_for_write(db, _get_account(db, company_id, session.year).id)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, account.id, 0, 0, -amount
                    )
                    db.add(
                        AllowanceTransaction(
                            account_id=account.id,
                            company_id=company_id,
                            tx_type="auction_release",
                            amount=amount,
                            counterparty=f"集中竞价 {session.session_no}",
                            price=None,
                            tx_date=_today(),
                            balance_after=round(balance_after, 4),
                            frozen_after=round(frozen_after, 4),
                            reserved_after=round(reserved_after, 4),
                            remark=f"场次 {session.session_no} 结算，释放未成交卖单占用 {amount} 吨",
                        )
                    )

                # 终结报价单状态：active 且有成交→partial（撮合并列兜底），
                # active 且零成交→unfilled；FILLED 已在撮合阶段写定。
                # 改完立即 flush：后续买方履约核销内部会 expire_all，
                # autoflush=False 下不先落库的脏属性会被过期重载丢弃。
                for bid in bids:
                    filled = round(float(bid.filled_quantity), 4)
                    if bid.status == ACTIVE:
                        bid.status = PARTIAL if filled > 0 else UNFILLED
                    # FILLED / PARTIAL 已是撮合阶段写入的终态
                db.flush()

                # 全部成交状态用条件 UPDATE 一次性置为 settled（绕过 expire_all 丢脏属性）
                settled_at = datetime.utcnow()
                db.execute(
                    update(AuctionTrade)
                    .where(AuctionTrade.session_id == session_id)
                    .where(AuctionTrade.status == PENDING)
                    .values(status=TRADE_SETTLED, settled_at=settled_at)
                    .execution_options(synchronize_session=False)
                )

                # 买方年度配额闭环：本场全部到账后，按企业合并核销一次履约缺口
                for buyer_id, total_bought in buyer_bought.items():
                    settle_buyer_deficit_on_auction(db, session, buyer_id, total_bought)

                session.settled_volume = settled_volume
                session.settled_at = settled_at
                db.flush()
                db.refresh(session)
        except InsufficientBalanceError:
            raise AuctionError("配额账本状态异常，结算失败并已整体回滚")
        return session
