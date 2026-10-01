"""碳配额集中竞价市场 API：场次、报价、撤单、撮合、结算、成交与权限审计。

权限边界：
- admin（监管）：场次全生命周期、撮合/结算/撤销、代为报价、查看全部数据与审计；
- enterprise（买方/卖方）：申报中报价/撤本企业单，只看本企业报价与本企业成交，
  场次列表/详情/成交汇总可见；
- verifier（核查员）：只读查看，不能报价/撮合/结算。
全部敏感操作成功后写审计；越权调用即使被拒绝也独立落一条 denied 审计。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import (
    AuctionBid,
    AuctionSession,
    AuctionTrade,
    Company,
    ComplianceRecord,
    User,
)
from app.schemas import AuctionBidIn, AuctionCancelIn, AuctionSessionIn
from app.services import audit_service
from app.services.auction_service import (
    AuctionError,
    BUY,
    SELL,
    cancel_bid,
    cancel_session,
    create_session,
    match_session,
    place_bid,
    settle_session,
)

router = APIRouter(prefix="/api/auctions", tags=["auctions"])


# ---------------------------------------------------------------------------
# 审计辅助
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host or ""
    return ""


def _audit(
    db: Session,
    user: User,
    request: Request,
    action: str,
    *,
    target_type: str = "",
    target_id=None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
):
    return audit_service.record_audit(
        db,
        user,
        action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        result=result,
        ip=_client_ip(request),
        commit=commit,
    )


def _deny(db: Session, user: User, request: Request, action: str, detail: str, target_id=None):
    """越权/越界拒绝：返回 403 前独立提交一条 denied 审计。"""
    _audit(
        db, user, request, action,
        target_type="session", target_id=target_id,
        detail=detail, result="denied", commit=True,
    )
    raise HTTPException(status_code=403, detail=detail)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式非法：{value}（需 ISO 8601）")
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _serialize_session(db: Session, s: AuctionSession, *, bid_stats: dict | None = None) -> dict:
    payload = {
        "id": s.id,
        "session_no": s.session_no,
        "name": s.name,
        "year": s.year,
        "status": s.status,
        "price_floor": float(s.price_floor or 0),
        "price_ceiling": float(s.price_ceiling or 0),
        "bid_start_at": s.bid_start_at,
        "bid_end_at": s.bid_end_at,
        "clear_price": float(s.clear_price) if s.clear_price is not None else None,
        "matched_volume": float(s.matched_volume or 0),
        "settled_volume": float(s.settled_volume or 0),
        "remark": s.remark,
        "cancel_reason": s.cancel_reason,
        "matched_at": s.matched_at,
        "settled_at": s.settled_at,
        "cancelled_at": s.cancelled_at,
        "created_at": s.created_at,
    }
    if bid_stats is not None:
        payload.update(bid_stats)
    return payload


def _session_bid_stats(db: Session, session_id: int) -> dict:
    rows = (
        db.query(AuctionBid.side, AuctionBid.status, AuctionBid.quantity, AuctionBid.filled_quantity)
        .filter(AuctionBid.session_id == session_id)
        .all()
    )
    buy_qty = sell_qty = buy_count = sell_count = 0.0
    for side, status, qty, filled in rows:
        qty = float(qty)
        if side == BUY:
            buy_qty += qty
            buy_count += 1
        else:
            sell_qty += qty
            sell_count += 1
    return {
        "buy_quantity": round(buy_qty, 4),
        "sell_quantity": round(sell_qty, 4),
        "bid_count": buy_count + sell_count,
    }


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _serialize_bid(db: Session, b: AuctionBid, *, expose_counterparty: bool) -> dict:
    return {
        "id": b.id,
        "session_id": b.session_id,
        "company_id": b.company_id,
        "company_name": _company_name(db, b.company_id),
        "year": b.year,
        "side": b.side,
        "price": float(b.price),
        "quantity": float(b.quantity),
        "filled_quantity": float(b.filled_quantity or 0),
        "unfilled_quantity": round(float(b.quantity) - float(b.filled_quantity or 0), 4),
        "status": b.status,
        "remark": b.remark,
        "cancel_reason": b.cancel_reason,
        "cancelled_at": b.cancelled_at,
        "created_at": b.created_at,
    }


def _serialize_trade(db: Session, t: AuctionTrade, *, expose_counterparty: bool) -> dict:
    return {
        "id": t.id,
        "trade_no": t.trade_no,
        "session_id": t.session_id,
        "year": t.year,
        "buyer_id": t.buyer_id,
        "seller_id": t.seller_id,
        "buyer_name": _company_name(db, t.buyer_id) if expose_counterparty else "-",
        "seller_name": _company_name(db, t.seller_id) if expose_counterparty else "-",
        "buy_bid_id": t.buy_bid_id,
        "sell_bid_id": t.sell_bid_id,
        "price": float(t.price),
        "quantity": float(t.quantity),
        "amount": round(float(t.price) * float(t.quantity), 2),
        "status": t.status,
        "settled_at": t.settled_at,
        "created_at": t.created_at,
    }


def _load_session_or_404(db: Session, session_id: int) -> AuctionSession:
    session = db.get(AuctionSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="竞价场次不存在")
    return session


# ---------------------------------------------------------------------------
# 场次
# ---------------------------------------------------------------------------


@router.get("")
def list_sessions(
    year: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(AuctionSession)
    if year is not None:
        q = q.filter(AuctionSession.year == year)
    if status:
        q = q.filter(AuctionSession.status == status)
    sessions = q.order_by(AuctionSession.id.desc()).all()
    return [_serialize_session(db, s, bid_stats=_session_bid_stats(db, s.id)) for s in sessions]


@router.post("")
def create(
    request: Request,
    data: AuctionSessionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        _deny(db, user, request, "auction.session.create", "仅监管管理员可以创建竞价场次")
    try:
        session = create_session(
            db,
            data.year,
            name=data.name,
            price_floor=data.price_floor,
            price_ceiling=data.price_ceiling,
            bid_start_at=_parse_dt(data.bid_start_at),
            bid_end_at=_parse_dt(data.bid_end_at),
            remark=data.remark,
            created_by=user.id,
        )
        _audit(
            db, user, request, "auction.session.create",
            target_type="session", target_id=session.id,
            detail=f"创建场次 {session.session_no}（{data.year}年度，限价 {data.price_floor}~{data.price_ceiling}）",
        )
        db.commit()
        db.refresh(session)
    except AuctionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session, bid_stats=_session_bid_stats(db, session.id))


@router.get("/audit-logs")
def get_audit_logs(
    request: Request,
    action: str | None = None,
    company_id: int | None = None,
    result: str | None = None,
    target_type: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        _deny(db, user, request, "auction.audit.view", "仅监管管理员可以查看权限审计日志")
    logs = audit_service.list_audit_logs(
        db,
        action=action,
        company_id=company_id,
        target_type=target_type,
        result=result,
        limit=limit,
    )
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "username": log.username,
            "role": log.role,
            "company_id": log.company_id,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "detail": log.detail,
            "result": log.result,
            "ip": log.ip,
            "created_at": log.created_at,
        }
        for log in logs
    ]


@router.get("/{session_id}")
def session_detail(session_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    session = _load_session_or_404(db, session_id)
    return _serialize_session(db, session, bid_stats=_session_bid_stats(db, session_id))


@router.post("/{session_id}/cancel")
def session_cancel(
    session_id: int,
    data: AuctionCancelIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        _deny(db, user, request, "auction.session.cancel", "仅监管管理员可以撤销竞价场次", target_id=session_id)
    session = _load_session_or_404(db, session_id)
    try:
        session = cancel_session(db, session_id, user.id, data.reason)
        _audit(
            db, user, request, "auction.session.cancel",
            target_type="session", target_id=session_id,
            detail=f"撤销场次 {session.session_no}：{data.reason or '无'}",
        )
        db.commit()
        db.refresh(session)
    except AuctionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session, bid_stats=_session_bid_stats(db, session_id))


@router.post("/{session_id}/match")
def session_match(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        _deny(db, user, request, "auction.session.match", "仅监管管理员可以执行撮合", target_id=session_id)
    _load_session_or_404(db, session_id)
    try:
        session = match_session(db, session_id)
        volume = float(session.matched_volume or 0)
        price = float(session.clear_price) if session.clear_price is not None else None
        detail = (
            f"撮合场次 {session.session_no}：成交量 {volume} 吨，统一成交价 "
            + (f"{price} 元/t" if price is not None else "无成交")
        )
        _audit(db, user, request, "auction.session.match", target_type="session",
               target_id=session_id, detail=detail)
        db.commit()
        db.refresh(session)
    except AuctionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session, bid_stats=_session_bid_stats(db, session_id))


@router.post("/{session_id}/settle")
def session_settle(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        _deny(db, user, request, "auction.session.settle", "仅监管管理员可以执行结算", target_id=session_id)
    _load_session_or_404(db, session_id)
    try:
        session = settle_session(db, session_id)
        _audit(
            db, user, request, "auction.session.settle",
            target_type="session", target_id=session_id,
            detail=(
                f"结算场次 {session.session_no}：结算量 {float(session.settled_volume or 0)} 吨，"
                f"统一成交价 {float(session.clear_price or 0)} 元/t，已回写账户/流水/履约缺口"
            ),
        )
        db.commit()
        db.refresh(session)
    except AuctionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    payload = _serialize_session(db, session, bid_stats=_session_bid_stats(db, session_id))
    return payload


# ---------------------------------------------------------------------------
# 报价与撤单
# ---------------------------------------------------------------------------


def _visible_bid_query(db: Session, user: User):
    q = db.query(AuctionBid)
    if user.role == "enterprise":
        q = q.filter(AuctionBid.company_id == user.company_id)
    # admin / verifier 可见全部
    return q


@router.get("/{session_id}/bids")
def list_bids(
    session_id: int,
    side: str | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _load_session_or_404(db, session_id)
    q = _visible_bid_query(db, user).filter(AuctionBid.session_id == session_id)
    if side:
        q = q.filter(AuctionBid.side == side)
    if status_filter:
        q = q.filter(AuctionBid.status == status_filter)
    bids = q.order_by(AuctionBid.id.asc()).all()
    expose = user.role in ("admin", "verifier")
    return [_serialize_bid(db, b, expose_counterparty=expose) for b in bids]


@router.post("/{session_id}/bids")
def create_bid(
    session_id: int,
    request: Request,
    data: AuctionBidIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "enterprise"):
        _deny(db, user, request, "auction.bid.place", "核查员为只读角色，不能参与报价", target_id=session_id)

    # 企业只能代表本企业报价；监管必须显式指定企业
    if user.role == "enterprise":
        company_id = user.company_id
    else:
        company_id = data.company_id
        if company_id is None:
            raise HTTPException(status_code=400, detail="监管代客报价必须指定 company_id")
        if not db.get(Company, company_id):
            raise HTTPException(status_code=404, detail="报价企业不存在")

    _load_session_or_404(db, session_id)
    idem_key = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        bid = place_bid(
            db,
            session_id,
            company_id,
            data.side,
            data.price,
            data.quantity,
            remark=data.remark,
            idempotency_key=idem_key,
            created_by=user.id,
        )
        _audit(
            db, user, request, "auction.bid.place",
            target_type="bid", target_id=bid.id,
            detail=(
                f"场次#{session_id} 企业#{company_id} "
                f"{'买入' if data.side == BUY else '卖出'}报价 {data.quantity} 吨 @ {data.price} 元/t"
                + ("（卖单已占用配额）" if data.side == SELL else "")
            ),
        )
        db.commit()
        db.refresh(bid)
    except AuctionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        db.rollback()
        raise
    return _serialize_bid(db, bid, expose_counterparty=True)


@router.post("/bids/{bid_id}/cancel")
def cancel_bid_endpoint(
    bid_id: int,
    request: Request,
    data: AuctionCancelIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "enterprise"):
        _deny(db, user, request, "auction.bid.cancel", "核查员为只读角色，不能撤单", target_id=bid_id)
    bid = db.get(AuctionBid, bid_id)
    if bid is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    operator_company_id = None if user.role == "admin" else user.company_id
    try:
        bid = cancel_bid(db, bid_id, operator_company_id, data.reason)
        _audit(
            db, user, request, "auction.bid.cancel",
            target_type="bid", target_id=bid_id,
            detail=(
                f"撤回报价单 #{bid_id}（企业#{bid.company_id}，"
                f"{'买单' if bid.side == BUY else '卖单'} {float(bid.quantity)} 吨）：{data.reason or '无'}"
            ),
        )
        db.commit()
        db.refresh(bid)
    except AuctionError as e:
        db.rollback()
        status_code = 403 if "无权" in str(e) else 400
        if status_code == 403:
            _audit(
                db, user, request, "auction.bid.cancel",
                target_type="bid", target_id=bid_id,
                detail=f"越权撤单被拒绝：{e}", result="denied", commit=True,
            )
        raise HTTPException(status_code=status_code, detail=str(e))
    return _serialize_bid(db, bid, expose_counterparty=True)


# ---------------------------------------------------------------------------
# 成交记录（结算状态）
# ---------------------------------------------------------------------------


@router.get("/{session_id}/trades")
def list_trades(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _load_session_or_404(db, session_id)
    q = db.query(AuctionTrade).filter(AuctionTrade.session_id == session_id)
    if user.role == "enterprise":
        q = q.filter(
            (AuctionTrade.buyer_id == user.company_id) | (AuctionTrade.seller_id == user.company_id)
        )
    trades = q.order_by(AuctionTrade.id.asc()).all()
    # 监管/核查可见全部对手方；企业只看到自己的成交（对手方名称可见）
    expose = True
    return [_serialize_trade(db, t, expose_counterparty=expose) for t in trades]

