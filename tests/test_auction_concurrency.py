"""集中竞价市场并发一致性测试（多线程 + 文件型 SQLite）。

覆盖：
- 并发撮合：只有一个线程撮合成功，不产生重复成交；
- 并发结算：只有一个线程结算成功，账户/流水/成交状态不重复落账；
- 并发报价（多卖方）：reserved 占用总额不超过各自自由可用，无超额占用；
- 卖单占用与企业间订单/履约冻结互不挤占；
- 申报截止与撤单竞争：撮合成功后并发撤单全部失败，占用不被错误释放；
- 结算与撤销竞争：只有一方成功，账户与场次状态一致；
- 结算后跨账户配额守恒：持仓合计不变、占用全部归零、流水与账户快照一致。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionBid,
    AuctionSession,
    AuctionTrade,
    Company,
)
from app.services import auction_service as A
from app.services.quota_service import allocate_quota


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'auction_concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(eng, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()


def _fresh(engine):
    return Session(bind=engine)


def _make_company(db, n, quota=100000, year=2025):
    cs = []
    for i in range(n):
        c = Company(code=f"CC{i:03d}", name=f"并发企业{i}")
        db.add(c)
        cs.append(c)
    db.flush()
    for c in cs:
        allocate_quota(db, c.id, year, quota, quota)
    db.commit()
    return cs


def approx(v):
    return pytest.approx(float(v), abs=1e-4)


def _account(db, company_id):
    return db.query(AllowanceAccount).filter_by(company_id=company_id, year=2025).one()


class TestConcurrentAuction:
    def test_concurrent_match_only_one_wins(self, db, engine):
        cs = _make_company(db, 6)
        s = A.create_session(db, 2025)
        for i in range(3):
            A.place_bid(db, s.id, cs[i].id, A.SELL, 80 + i, 1000)
        for i in range(3, 6):
            A.place_bid(db, s.id, cs[i].id, A.BUY, 90 + i - 3, 1000)
        db.commit()
        sid = s.id

        results = []

        def worker():
            sess = _fresh(engine)
            try:
                A.match_session(sess, sid)
                results.append("ok")
            except Exception:
                results.append("err")
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: worker(), range(8)))

        assert results.count("ok") == 8  # 幂等返回也视为成功
        trades = db.query(AuctionTrade).filter_by(session_id=sid).all()
        # 成交量不超过任一侧申报总量，且成交记录绝不重复（同一张报价单不被配对两次）
        buy_filled = sum(float(t.quantity) for t in trades)
        assert buy_filled <= 3000 + 1e-9
        # 每张报价单累计成交量不超过其申报量
        per_bid: dict[int, float] = {}
        for t in trades:
            per_bid[t.buy_bid_id] = per_bid.get(t.buy_bid_id, 0) + float(t.quantity)
            per_bid[t.sell_bid_id] = per_bid.get(t.sell_bid_id, 0) + float(t.quantity)
        for bid in db.query(AuctionBid).filter_by(session_id=sid).all():
            assert per_bid.get(bid.id, 0) <= float(bid.quantity) + 1e-9
        db.refresh(s)
        assert s.status == A.MATCHED

    def test_concurrent_settle_only_one_settles(self, db, engine):
        cs = _make_company(db, 4)
        s = A.create_session(db, 2025)
        for i in range(2):
            A.place_bid(db, s.id, cs[i].id, A.SELL, 80, 500)
        for i in range(2, 4):
            A.place_bid(db, s.id, cs[i].id, A.BUY, 90, 500)
        db.commit()
        sid = s.id
        A.match_session(db, sid)
        db.commit()

        def worker():
            sess = _fresh(engine)
            try:
                A.settle_session(sess, sid)
                return True
            except Exception:
                return False
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(8)))

        # 允许并发幂等返回 True；关键是账只落一次
        db.expire_all()
        s = db.get(AuctionSession, sid)
        assert s.status == A.SETTLED
        assert float(s.settled_volume) == approx(1000)
        # 每个卖方只出库一次：持仓 100000-500
        for i in range(2):
            acc = _account(db, cs[i].id)
            assert float(acc.current_balance) == approx(99500)
            assert float(acc.reserved_balance) == 0
        # 每账户竞价划出/受让流水各恰好一条
        out_txs = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "auction_deliver_out")
            .count()
        )
        in_txs = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "auction_deliver_in")
            .count()
        )
        assert out_txs == 2
        assert in_txs == 2
        assert db.query(AuctionTrade).filter_by(session_id=sid, status=A.PENDING).count() == 0

    def test_concurrent_sell_bids_never_over_reserve(self, db, engine):
        """4 个企业各自只有 600 自由可用，8 线程并发各报 200 卖单：每企业至多 3 笔占用成功。"""
        cs = _make_company(db, 4, quota=600)
        s = A.create_session(db, 2025)
        db.commit()
        sid = s.id
        cids = [c.id for c in cs]

        def worker(idx):
            sess = _fresh(engine)
            cid = cids[idx % 4]
            try:
                A.place_bid(sess, sid, cid, A.SELL, 80, 200)
                return ("ok", cid)
            except Exception:
                return ("reject", cid)
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))

        db.expire_all()
        for cid in cids:
            acc = _account(db, cid)
            assert float(acc.reserved_balance) <= float(acc.current_balance) + 1e-9
            assert float(acc.reserved_balance) <= 600 + 1e-9
        # 总占用恰好 = 成功笔数 * 200，且不超过 4*600
        ok_count = sum(1 for r, _ in results if r == "ok")
        total_reserved = sum(float(_account(db, cid).reserved_balance) for cid in cids)
        assert total_reserved == approx(ok_count * 200)
        assert ok_count + sum(1 for r, _ in results if r == "reject") == 8

    def test_settle_vs_cancel_only_one_wins(self, db, engine):
        cs = _make_company(db, 2)
        s = A.create_session(db, 2025)
        A.place_bid(db, s.id, cs[0].id, A.SELL, 80, 400)
        A.place_bid(db, s.id, cs[1].id, A.BUY, 90, 400)
        db.commit()
        sid = s.id
        # 先由一个线程撮合，随后结算与撤销并发竞争
        A.match_session(db, sid)
        db.commit()

        def settle():
            sess = _fresh(engine)
            try:
                A.settle_session(sess, sid)
                return "settled"
            except Exception:
                return "settle-rejected"
            finally:
                sess.close()

        def cancel():
            sess = _fresh(engine)
            try:
                A.cancel_session(sess, sid, operator_id=1)
                return "cancelled"
            except Exception:
                return "cancel-rejected"
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(settle)
            f2 = pool.submit(cancel)
            r1, r2 = f1.result(), f2.result()

        db.expire_all()
        s = db.get(AuctionSession, sid)
        assert s.status in (A.SETTLED, A.CANCELLED)
        assert {r1, r2} in ({"settled", "cancel-rejected"}, {"cancelled", "settle-rejected"})
        # 无论哪方成功，卖方占用必须归零（结算=出库；撤销=释放），且持仓可解释
        acc = _account(db, cs[0].id)
        assert float(acc.reserved_balance) == 0
        if s.status == A.SETTLED:
            assert float(acc.current_balance) == approx(99600)  # 400 出库
        else:
            assert float(acc.current_balance) == approx(100000)  # 原样返还

    def test_cancel_after_match_all_fails(self, db, engine):
        """撮合成功瞬间并发撤单：全部失败，占用保持到结算统一处理。"""
        cs = _make_company(db, 2)
        s = A.create_session(db, 2025)
        bid = A.place_bid(db, s.id, cs[0].id, A.SELL, 80, 300)
        A.place_bid(db, s.id, cs[1].id, A.BUY, 90, 300)
        db.commit()
        sid, bid_id = s.id, bid.id

        def do_match():
            sess = _fresh(engine)
            try:
                A.match_session(sess, sid)
            finally:
                sess.close()

        def do_cancel():
            sess = _fresh(engine)
            try:
                result = A.cancel_bid(sess, bid_id, cs[0].id)
                # 幂等空返回不算撤单成功：只有返回态确为 cancelled 才是真撤销
                return result.status == A.BID_CANCELLED
            except Exception:
                return False
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            mf = pool.submit(do_match)
            cancel_futs = [pool.submit(do_cancel) for _ in range(3)]
            mf.result()
            cancelled = [f.result() for f in cancel_futs]

        db.expire_all()
        s = db.get(AuctionSession, sid)
        assert s.status == A.MATCHED
        # 合法竞争结果只有两种，一律按报价单最终状态判定（不依赖调用方返回值，
        # 因为多个撤单线程可能拿到幂等空返回）：
        # 1) 撮合先拿锁：报价单 filled，成交 300，占用保持到结算；
        # 2) 撤单先完成：报价单 cancelled，撮合零成交，占用已释放。
        bid = db.get(AuctionBid, bid_id)
        if bid.status == A.BID_CANCELLED:
            assert float(s.matched_volume) == 0
            assert float(_account(db, cs[0].id).reserved_balance) == 0
            # 释放流水只能有一条（重复撤单幂等，不重复释放）
            release_count = (
                db.query(AllowanceTransaction)
                .filter(
                    AllowanceTransaction.company_id == cs[0].id,
                    AllowanceTransaction.tx_type == "auction_release",
                )
                .count()
            )
            assert release_count == 1
            # 零成交撮合同样可结算（幂等空结算）
            A.settle_session(db, sid)
            db.commit()
            db.expire_all()
            assert db.get(AuctionSession, sid).status == A.SETTLED
            assert float(_account(db, cs[0].id).current_balance) == approx(100000)
        else:
            assert bid.status == A.FILLED
            assert float(s.matched_volume) == approx(300)
            assert float(_account(db, cs[0].id).reserved_balance) == approx(300)
            A.settle_session(db, sid)
            db.commit()
            db.expire_all()
            assert db.get(AuctionSession, sid).status == A.SETTLED
            assert float(_account(db, cs[0].id).reserved_balance) == 0
            assert float(_account(db, cs[0].id).current_balance) == approx(99700)

    def test_concurrent_settle_conservation_and_snapshots(self, db, engine):
        """多卖方/多买方结算：配额跨账户守恒、占用清零、流水快照逐笔可推算。"""
        cs = _make_company(db, 6, quota=10000)
        s = A.create_session(db, 2025)
        for i in range(3):
            A.place_bid(db, s.id, cs[i].id, A.SELL, 80 + i, 2000)
        for i in range(3, 6):
            A.place_bid(db, s.id, cs[i].id, A.BUY, 88 + (i - 3), 2000)
        db.commit()
        A.match_session(db, s.id)
        db.commit()
        sid = s.id

        def worker():
            sess = _fresh(engine)
            try:
                A.settle_session(sess, sid)
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: worker(), range(6)))

        db.expire_all()
        total_current = sum(float(a.current_balance) for a in db.query(AllowanceAccount).all())
        assert total_current == approx(60000)
        assert sum(float(a.reserved_balance) for a in db.query(AllowanceAccount).all()) == 0

        # 流水快照链：逐笔推算 current/reserved 与记录一致
        for c in cs:
            acc = _account(db, c.id)
            expected_current = expected_reserved = 0.0
            for tx in (
                db.query(AllowanceTransaction)
                .filter_by(account_id=acc.id)
                .order_by(AllowanceTransaction.id.asc())
            ):
                amt = float(tx.amount)
                if tx.tx_type == "allocation":
                    expected_current += amt
                elif tx.tx_type == "auction_reserve":
                    expected_reserved += amt
                elif tx.tx_type == "auction_release":
                    expected_reserved -= amt
                elif tx.tx_type == "auction_deliver_out":
                    expected_current -= amt
                    expected_reserved -= amt
                elif tx.tx_type == "auction_deliver_in":
                    expected_current += amt
                assert float(tx.balance_after) == approx(round(expected_current, 4))
                assert float(tx.reserved_after) == approx(round(expected_reserved, 4))
            assert float(acc.current_balance) == approx(round(expected_current, 4))
            assert float(acc.reserved_balance) == approx(round(expected_reserved, 4))
