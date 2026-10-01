"""集中竞价市场服务层测试。

覆盖：
- 场次状态机：创建/撮合/结算/撤销的合法与非法流转；
- 报价：卖单占用自由可用配额、买单不占用、冻结/占用不可重复卖出、价格边界、
  同企业反向报价防对敲、幂等键去重、撤单释放占用；
- 集合竞价定价：最大成交量原则、并列时最小未平衡量与参考价规则、价格/时间优先
  配对、全部成交/部分成交/未成交、零可成交量；
- 结算：卖方出库/买方到账、未成交占用释放、流水快照链、成交与场次状态一致、
  买方履约缺口自动回写（足额/部分）；
- 场次撤销释放全部占用；
- 撮合/结算幂等与非法状态拒绝。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionBid,
    AuctionSession,
    AuctionTrade,
    Company,
    ComplianceRecord,
)
from app.services import auction_service as A
from app.services.quota_service import allocate_quota


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()


@pytest.fixture()
def companies(db):
    cs = []
    for i in range(4):
        c = Company(code=f"C{i:03d}", name=f"企业{i}")
        db.add(c)
        cs.append(c)
    db.flush()
    return cs


def _account(db, company_id, year=2025):
    return (
        db.query(AllowanceAccount)
        .filter_by(company_id=company_id, year=year)
        .first()
    )


def _alloc(db, company, amount):
    allocate_quota(db, company.id, 2025, amount, amount)
    db.commit()


def approx(v):
    return pytest.approx(float(v), abs=1e-4)


# ---------------------------------------------------------------------------
# 场次
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_create_defaults_open(self, db, companies):
        s = A.create_session(db, 2025, name="Q4 竞价")
        assert s.status == A.OPEN
        assert s.session_no.startswith("AS2025")

    def test_invalid_price_bounds_rejected(self, db, companies):
        with pytest.raises(A.AuctionError):
            A.create_session(db, 2025, price_floor=100, price_ceiling=90)
        with pytest.raises(A.AuctionError):
            A.create_session(db, 2025, price_floor=-1)

    def test_match_then_settle_flow(self, db, companies):
        _alloc(db, companies[0], 500)
        _alloc(db, companies[1], 500)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 200)
        A.place_bid(db, s.id, companies[1].id, A.BUY, 85, 100)
        db.commit()

        A.match_session(db, s.id)
        db.refresh(s)
        assert s.status == A.MATCHED
        assert float(s.clear_price) == approx(80)
        assert float(s.matched_volume) == approx(100)

        # matched 阶段不能再报价/撤单
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[1].id, A.BUY, 86, 10)

        A.settle_session(db, s.id)
        db.refresh(s)
        assert s.status == A.SETTLED
        assert float(s.settled_volume) == approx(100)

    def test_match_and_settle_idempotent(self, db, companies):
        _alloc(db, companies[0], 500)
        _alloc(db, companies[1], 500)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 100)
        A.place_bid(db, s.id, companies[1].id, A.BUY, 90, 100)
        db.commit()
        A.match_session(db, s.id)
        A.match_session(db, s.id)  # 重复撮合不产生重复成交
        assert db.query(AuctionTrade).count() == 1
        A.settle_session(db, s.id)
        A.settle_session(db, s.id)  # 重复结算幂等
        sell_acc = _account(db, companies[0].id)
        buy_acc = _account(db, companies[1].id)
        assert float(sell_acc.current_balance) == approx(400)
        assert float(buy_acc.current_balance) == approx(600)

    def test_cancel_open_session_releases_all_reserves(self, db, companies):
        _alloc(db, companies[0], 1000)
        _alloc(db, companies[1], 1000)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 300)
        A.place_bid(db, s.id, companies[1].id, A.SELL, 90, 200)
        db.commit()
        A.cancel_session(db, s.id, operator_id=1, reason="系统测试")
        db.refresh(s)
        assert s.status == A.CANCELLED
        for c in companies[:2]:
            acc = _account(db, c.id)
            assert float(acc.reserved_balance) == 0
        assert db.query(AuctionBid).filter(AuctionBid.status != A.BID_CANCELLED).count() == 0

    def test_cancel_matched_session_rejected(self, db, companies):
        _alloc(db, companies[0], 500)
        _alloc(db, companies[1], 500)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 100)
        A.place_bid(db, s.id, companies[1].id, A.BUY, 90, 100)
        db.commit()
        A.match_session(db, s.id)
        with pytest.raises(A.AuctionError):
            A.cancel_session(db, s.id, operator_id=1)
        # matched 场次占用仍在，只有结算才释放/出库
        assert float(_account(db, companies[0].id).reserved_balance) == approx(100)


# ---------------------------------------------------------------------------
# 报价与撤单
# ---------------------------------------------------------------------------


class TestBids:
    def test_sell_bid_reserves_available_quota(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025)
        bid = A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 400)
        assert bid.status == A.ACTIVE
        acc = _account(db, companies[0].id)
        assert float(acc.current_balance) == approx(1000)
        assert float(acc.reserved_balance) == approx(400)
        # 持仓不变但自由可用只剩 600，再报 700 卖单被拒
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[0].id, A.SELL, 81, 700)

    def test_buy_bid_does_not_reserve(self, db, companies):
        _alloc(db, companies[1], 100)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[1].id, A.BUY, 90, 9999)
        acc = _account(db, companies[1].id)
        assert float(acc.reserved_balance) == 0

    def test_price_bounds_enforced(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025, price_floor=50, price_ceiling=100)
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[0].id, A.SELL, 49, 10)
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[0].id, A.SELL, 101, 10)
        # 边界价合法
        A.place_bid(db, s.id, companies[0].id, A.SELL, 50, 10)

    def test_opposite_side_same_company_rejected(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 100)
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[0].id, A.BUY, 80, 100)

    def test_idempotent_bid(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025)
        first = A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 100, idempotency_key="bid-key-1")
        again = A.place_bid(db, s.id, companies[0].id, A.SELL, 99, 999, idempotency_key="bid-key-1")
        assert first.id == again.id
        assert float(_account(db, companies[0].id).reserved_balance) == approx(100)

    def test_cancel_sell_bid_releases_reserve(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025)
        bid = A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 300)
        db.commit()
        A.cancel_bid(db, bid.id, companies[0].id, "不卖了")
        db.refresh(bid)
        assert bid.status == A.BID_CANCELLED
        assert float(_account(db, companies[0].id).reserved_balance) == 0

    def test_enterprise_cannot_cancel_others_bid(self, db, companies):
        _alloc(db, companies[0], 1000)
        s = A.create_session(db, 2025)
        bid = A.place_bid(db, s.id, companies[0].id, A.SELL, 80, 100)
        with pytest.raises(A.AuctionError):
            A.cancel_bid(db, bid.id, companies[1].id)
        # 监管（operator_company_id=None）可撤任意单
        A.cancel_bid(db, bid.id, None)
        db.refresh(bid)
        assert bid.status == A.BID_CANCELLED

    def test_bid_requires_account(self, db, companies):
        s = A.create_session(db, 2025)
        with pytest.raises(A.AuctionError):
            A.place_bid(db, s.id, companies[0].id, A.BUY, 80, 10)


# ---------------------------------------------------------------------------
# 集合竞价定价
# ---------------------------------------------------------------------------


def _seed_auction(db, companies, sells, buys, floor=0, ceiling=0):
    """sells/buys: [(company_index, price, quantity), ...]"""
    s = A.create_session(db, 2025, price_floor=floor, price_ceiling=ceiling)
    for idx, price, qty in sells:
        A.place_bid(db, s.id, companies[idx].id, A.SELL, price, qty)
    for idx, price, qty in buys:
        A.place_bid(db, s.id, companies[idx].id, A.BUY, price, qty)
    db.commit()
    A.match_session(db, s.id)
    db.refresh(s)
    return s


class TestClearingPrice:
    def test_max_volume_price_with_partial_and_unfilled(self, db, companies):
        for c in companies:
            _alloc(db, c, 1000)
        s = _seed_auction(
            db, companies,
            sells=[(0, 80, 100), (1, 90, 200), (2, 100, 300)],
            buys=[(3, 100, 150)],
        )
        # p=80: V=100; p=90: V=150; p=100: V=150 但未平衡量更大 -> 90
        assert float(s.clear_price) == approx(90)
        assert float(s.matched_volume) == approx(150)
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).order_by(AuctionTrade.id).all()
        # S0@80 先成交 100，再从 S1@90 成交 50
        assert float(trades[0].quantity) == approx(100)
        assert trades[0].seller_id == companies[0].id
        assert float(trades[1].quantity) == approx(50)
        assert trades[1].seller_id == companies[1].id

        A.settle_session(db, s.id)
        # 卖方余额：S0 900；S1 950（50 出库 + 150 释放）；S2 1000（300 全释放）
        assert float(_account(db, companies[0].id).current_balance) == approx(900)
        assert float(_account(db, companies[1].id).current_balance) == approx(950)
        assert float(_account(db, companies[1].id).reserved_balance) == 0
        assert float(_account(db, companies[2].id).current_balance) == approx(1000)
        assert float(_account(db, companies[2].id).reserved_balance) == 0
        assert float(_account(db, companies[3].id).current_balance) == approx(1150)

        statuses = {b.id: b.status for b in db.query(AuctionBid).filter_by(session_id=s.id)}
        sell_bids = db.query(AuctionBid).filter_by(session_id=s.id, side=A.SELL).order_by(AuctionBid.id).all()
        assert sell_bids[0].status == A.FILLED
        assert sell_bids[1].status == A.PARTIAL
        assert sell_bids[2].status == A.UNFILLED

    def test_no_cross_no_trade(self, db, companies):
        for c in companies:
            _alloc(db, c, 1000)
        s = _seed_auction(db, companies, sells=[(0, 100, 100)], buys=[(1, 90, 100)])
        assert s.clear_price is None
        assert float(s.matched_volume) == 0
        assert db.query(AuctionTrade).filter_by(session_id=s.id).count() == 0
        # 无成交撮合后仍可结算：释放卖单占用
        A.settle_session(db, s.id)
        db.refresh(s)
        assert s.status == A.SETTLED
        assert float(_account(db, companies[0].id).reserved_balance) == 0

    def test_time_priority_at_same_price(self, db, companies):
        for c in companies:
            _alloc(db, c, 1000)
        s = _seed_auction(
            db, companies,
            sells=[(0, 80, 100), (1, 80, 100)],
            buys=[(2, 90, 150)],
        )
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).order_by(AuctionTrade.id).all()
        # 同价先报先得：S0 全成 100，S1 只成 50
        assert trades[0].seller_id == companies[0].id
        assert float(trades[0].quantity) == approx(100)
        assert trades[1].seller_id == companies[1].id
        assert float(trades[1].quantity) == approx(50)

    def test_tie_break_by_imbalance(self, db, companies):
        """两个候选价成交量相同，取买卖未平衡量更小的价格。"""
        for c in companies:
            _alloc(db, c, 1000)
        # p=80: D=100 S=100 V=100 imbalance 0；p=90: D=100 S=200 V=100 imbalance 100
        s = _seed_auction(
            db, companies,
            sells=[(0, 80, 100), (1, 90, 100)],
            buys=[(2, 90, 100)],
        )
        assert float(s.clear_price) == approx(80)


# ---------------------------------------------------------------------------
# 结算账本与履约回写
# ---------------------------------------------------------------------------


def _deficit_record(db, company, emission, cleared=0):
    rec = ComplianceRecord(
        company_id=company.id, year=2025, verified_emission=emission,
        cleared_amount=cleared, frozen_amount=0, deficit=emission - cleared,
        status="deficit" if emission - cleared > 0 else "compliant",
        deadline="2025-12-31", is_active=1,
    )
    db.add(rec)
    db.commit()
    return rec


class TestSettlement:
    def test_ledger_and_tx_snapshots_consistent(self, db, companies):
        _alloc(db, companies[0], 1000)
        _alloc(db, companies[1], 1000)
        s = _seed_auction(
            db, companies,
            sells=[(0, 80, 400)],
            buys=[(1, 85, 300)],
        )
        A.settle_session(db, s.id)
        db.commit()

        for c in companies[:2]:
            acc = _account(db, c.id)
            txs = (
                db.query(AllowanceTransaction)
                .filter_by(account_id=acc.id)
                .order_by(AllowanceTransaction.id.asc())
                .all()
            )
            expected_current = expected_reserved = 0.0
            for tx in txs:
                amt = float(tx.amount)
                dc = dr = 0
                if tx.tx_type == "allocation":
                    dc = amt
                elif tx.tx_type == "auction_reserve":
                    dr = amt
                elif tx.tx_type == "auction_release":
                    dr = -amt
                elif tx.tx_type == "auction_deliver_out":
                    dc = -amt
                    dr = -amt
                elif tx.tx_type == "auction_deliver_in":
                    dc = amt
                expected_current = round(expected_current + dc, 4)
                expected_reserved = round(expected_reserved + dr, 4)
                assert float(tx.balance_after) == approx(expected_current)
                assert float(tx.reserved_after) == approx(expected_reserved)
            assert float(acc.current_balance) == approx(expected_current)
            assert float(acc.reserved_balance) == approx(expected_reserved)

    def test_buyer_deficit_fully_cleared(self, db, companies):
        _alloc(db, companies[0], 1000)
        _alloc(db, companies[1], 100)  # 买方只有 100
        _deficit_record(db, companies[1], emission=300, cleared=100)  # 缺口 200
        s = _seed_auction(db, companies, sells=[(0, 70, 500)], buys=[(1, 80, 300)])
        A.settle_session(db, s.id)
        db.commit()

        rec = db.query(ComplianceRecord).filter_by(company_id=companies[1].id, is_active=1).one()
        db.expire(rec)
        rec = db.get(ComplianceRecord, rec.id)
        assert rec.status == "compliant"
        assert float(rec.cleared_amount) == approx(300)
        assert float(rec.deficit) == approx(0)
        acc = _account(db, companies[1].id)
        # 到账 300 后 400，补缴缺口 200，剩 200
        assert float(acc.current_balance) == approx(200)
        assert float(acc.reserved_balance) == 0
        # 补缴流水已记录
        assert db.query(AllowanceTransaction).filter_by(
            account_id=acc.id, tx_type="auction_deficit_clear"
        ).count() == 1

    def test_buyer_deficit_partial_remains(self, db, companies):
        _alloc(db, companies[0], 1000)
        _alloc(db, companies[1], 0)
        _deficit_record(db, companies[1], emission=300, cleared=0)  # 缺口 300
        s = _seed_auction(db, companies, sells=[(0, 70, 100)], buys=[(1, 80, 100)])
        A.settle_session(db, s.id)
        db.commit()
        rec = db.query(ComplianceRecord).filter_by(company_id=companies[1].id, is_active=1).one()
        db.expire(rec)
        rec = db.get(ComplianceRecord, rec.id)
        assert rec.status == "deficit"
        assert float(rec.deficit) == approx(200)
        assert float(_account(db, companies[1].id).current_balance) == approx(0)

    def test_settle_before_match_rejected(self, db, companies):
        s = A.create_session(db, 2025)
        with pytest.raises(A.AuctionError):
            A.settle_session(db, s.id)

    def test_trades_settled_flag(self, db, companies):
        _alloc(db, companies[0], 1000)
        _alloc(db, companies[1], 1000)
        s = _seed_auction(db, companies, sells=[(0, 80, 100)], buys=[(1, 90, 100)])
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).all()
        assert all(t.status == A.PENDING for t in trades)
        A.settle_session(db, s.id)
        db.expire_all()
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).all()
        assert all(t.status == A.TRADE_SETTLED and t.settled_at for t in trades)

    def test_partial_fill_settlement_persists_trade_and_bid_status(self, db, companies):
        """回归：卖单部分成交、未成交占用释放（额外 lock_row_for_write/expire_all）
        不能导致成交状态/报价单终态丢失。"""
        for c in companies:
            _alloc(db, c, 1000)
        s = _seed_auction(db, companies, sells=[(0, 80, 400)], buys=[(1, 85, 300)])
        A.settle_session(db, s.id)
        db.commit()
        db.expire_all()
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).all()
        assert len(trades) == 1
        assert trades[0].status == A.TRADE_SETTLED
        assert trades[0].settled_at is not None
        sell_bid = db.query(AuctionBid).filter_by(session_id=s.id, side=A.SELL).one()
        assert sell_bid.status == A.PARTIAL
        assert float(sell_bid.filled_quantity) == approx(300)
        buy_bid = db.query(AuctionBid).filter_by(session_id=s.id, side=A.BUY).one()
        assert buy_bid.status == A.FILLED

    def test_conservation_of_allowances(self, db, companies):
        """结算前后全部企业持仓之和守恒（出库=到账；占用归零）。"""
        for c in companies:
            _alloc(db, c, 1000)
        s = _seed_auction(
            db, companies,
            sells=[(0, 80, 300), (1, 90, 200)],
            buys=[(2, 100, 250), (3, 85, 100)],
        )
        before = sum(float(a.current_balance) for a in db.query(AllowanceAccount).all())
        A.settle_session(db, s.id)
        db.commit()
        db.expire_all()
        after = sum(float(a.current_balance) for a in db.query(AllowanceAccount).all())
        assert before == approx(after)
        assert sum(float(a.reserved_balance) for a in db.query(AllowanceAccount).all()) == 0
        assert sum(float(a.frozen_balance) for a in db.query(AllowanceAccount).all()) == 0
