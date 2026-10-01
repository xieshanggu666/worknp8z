"""集中竞价市场 API 集成测试：角色边界、场次/报价/撮合/结算全流程、权限审计。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuditLog,
    AuctionBid,
    AuctionTrade,
    Company,
    ComplianceRecord,
    User,
)
from app.services.quota_service import allocate_quota


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="E-001", name="企业甲（卖方）", industry="电力", region="华东")
    c2 = Company(code="E-002", name="企业乙（买方）", industry="水泥", region="华北")
    c3 = Company(code="E-003", name="企业丙", industry="钢铁", region="华南")
    db.add_all([c1, c2, c3])
    db.flush()
    users = [
        User(username="ent1", display_name="甲", role="enterprise", company_id=c1.id,
             password_hash=pwd_hash, salt=salt),
        User(username="ent2", display_name="乙", role="enterprise", company_id=c2.id,
             password_hash=pwd_hash, salt=salt),
        User(username="ent3", display_name="丙", role="enterprise", company_id=c3.id,
             password_hash=pwd_hash, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd_hash, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd_hash, salt=salt),
    ]
    db.add_all(users)
    db.flush()
    allocate_quota(db, c1.id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, c2.id, 2025, baseline=500, allocation_amount=500)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id, "c3": c3.id}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids, TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def _create_open_session(client, floor=0.0, ceiling=0.0):
    res = client.post("/api/auctions", json={
        "year": 2025, "name": "测试场次", "price_floor": floor, "price_ceiling": ceiling,
    }, headers={"Idempotency-Key": "x"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


# ---------------------------------------------------------------------------
# 角色边界
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_only_admin_can_create_session(self, ctx):
        client, _, _Session = ctx
        login(client, "ent1")
        res = client.post("/api/auctions", json={"year": 2025})
        assert res.status_code == 403
        login(client, "verifier")
        res = client.post("/api/auctions", json={"year": 2025})
        assert res.status_code == 403

    def test_denied_actions_are_audited(self, ctx):
        client, _, _Session = ctx
        login(client, "ent1")
        client.post("/api/auctions", json={"year": 2025})  # 403 拒绝
        login(client, "admin")
        logs = client.get("/api/auctions/audit-logs").json()
        denied = [l for l in logs if l["result"] == "denied"]
        assert any(l["action"] == "auction.session.create" for l in denied)

    def test_enterprise_cannot_view_audit_logs(self, ctx):
        client, _, _Session = ctx
        login(client, "ent1")
        assert client.get("/api/auctions/audit-logs").status_code == 403

    def test_verifier_readonly(self, ctx):
        client, ids, _Session = ctx
        sid = _create_open_session(_as_admin(client))
        login(client, "verifier")
        res = client.post(f"/api/auctions/{sid}/bids", json={
            "side": "buy", "price": 80, "quantity": 10,
        })
        assert res.status_code == 403
        # 只读接口可访问
        assert client.get("/api/auctions").status_code == 200
        assert client.get(f"/api/auctions/{sid}/trades").status_code == 200


def _as_admin(client):
    login(client, "admin")
    return client


class TestBidBoundary:
    def test_enterprise_must_bid_as_self(self, ctx):
        client, ids, _Session = ctx
        sid = _create_open_session(_as_admin(client))
        login(client, "ent1")
        # 显式替别的企业报价即使带上 company_id 也只能是自己
        res = client.post(f"/api/auctions/{sid}/bids", json={
            "company_id": ids["c2"], "side": "sell", "price": 80, "quantity": 100,
        })
        assert res.status_code == 200
        body = res.json()
        # 服务端忽略企业传入的 company_id，强制落为本企业
        assert body["company_id"] == ids["c1"]

    def test_admin_bid_requires_company_id(self, ctx):
        client, ids, _Session = ctx
        sid = _create_open_session(_as_admin(client))
        res = client.post(f"/api/auctions/{sid}/bids", json={
            "side": "buy", "price": 80, "quantity": 10,
        })
        assert res.status_code == 400

    def test_enterprise_sees_only_own_bids(self, ctx):
        client, ids, _Session = ctx
        sid = _create_open_session(_as_admin(client))
        login(client, "ent1")
        client.post(f"/api/auctions/{sid}/bids", json={"side": "sell", "price": 80, "quantity": 100})
        login(client, "ent2")
        client.post(f"/api/auctions/{sid}/bids", json={"side": "buy", "price": 90, "quantity": 100})
        login(client, "ent1")
        bids = client.get(f"/api/auctions/{sid}/bids").json()
        assert len(bids) == 1
        assert bids[0]["company_id"] == ids["c1"]

    def test_enterprise_trades_scoped(self, ctx):
        """企业在成交记录里只能看到本企业参与的成交。"""
        client, ids, _Session = ctx
        sid = _create_open_session(_as_admin(client))
        # 管理端代 c1/c2 报价并撮合
        client.post(f"/api/auctions/{sid}/bids", json={
            "company_id": ids["c1"], "side": "sell", "price": 80, "quantity": 100})
        client.post(f"/api/auctions/{sid}/bids", json={
            "company_id": ids["c2"], "side": "buy", "price": 90, "quantity": 100})
        client.post(f"/api/auctions/{sid}/match")
        client.post(f"/api/auctions/{sid}/settle")
        login(client, "ent3")  # 未参与
        assert client.get(f"/api/auctions/{sid}/trades").json() == []
        login(client, "ent1")
        assert len(client.get(f"/api/auctions/{sid}/trades").json()) == 1


# ---------------------------------------------------------------------------
# 完整业务流程
# ---------------------------------------------------------------------------


def test_full_auction_flow_with_audit(ctx):
    client, ids, _Session = ctx
    sid = _create_open_session(_as_admin(client))

    login(client, "ent1")
    res = client.post(f"/api/auctions/{sid}/bids", json={
        "side": "sell", "price": 80, "quantity": 400,
    }, headers={"Idempotency-Key": "sell-1"})
    assert res.status_code == 200
    # 重复提交同幂等键只产生一张单
    client.post(f"/api/auctions/{sid}/bids", json={
        "side": "sell", "price": 99, "quantity": 999,
    }, headers={"Idempotency-Key": "sell-1"})
    # 卖单占用体现在账户
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["reserved_balance"] == 400
    assert acc["available_balance"] == 600

    login(client, "ent2")
    client.post(f"/api/auctions/{sid}/bids", json={
        "side": "buy", "price": 85, "quantity": 300,
    })

    # 企业不能撮合/结算
    assert client.post(f"/api/auctions/{sid}/match").status_code == 403
    assert client.post(f"/api/auctions/{sid}/settle").status_code == 403

    login(client, "admin")
    matched = client.post(f"/api/auctions/{sid}/match").json()
    assert matched["status"] == "matched"
    assert matched["clear_price"] == 80
    assert matched["matched_volume"] == 300

    # 撮合后企业撤单被拒（场次已截止申报）
    login(client, "ent1")
    own_bids = client.get(f"/api/auctions/{sid}/bids").json()
    bid_id = own_bids[0]["id"]
    res = client.post(f"/api/auctions/bids/{bid_id}/cancel", json={"reason": "x"})
    assert res.status_code == 400

    # 结算
    login(client, "admin")
    settled = client.post(f"/api/auctions/{sid}/settle").json()
    assert settled["status"] == "settled"
    assert settled["settled_volume"] == 300

    # 账户：卖方 1000-300=700，占用清零（100 未成交释放）；买方 500+300=800
    acc1 = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    acc2 = client.get(f"/api/companies/{ids['c2']}/account?year=2025").json()
    assert acc1["current_balance"] == 700 and acc1["reserved_balance"] == 0
    assert acc2["current_balance"] == 800

    # 成交记录两条角色都能看到自己的；监管看到 1 笔
    trades = client.get(f"/api/auctions/{sid}/trades").json()
    assert len(trades) == 1
    assert trades[0]["quantity"] == 300
    assert trades[0]["amount"] == 300 * 80
    assert trades[0]["status"] == "settled"

    # 报价单终态：卖单 partial（成交 300/400），买单 filled
    bids = client.get(f"/api/auctions/{sid}/bids").json()
    statuses = {(b["side"]): b["status"] for b in bids}
    assert statuses["sell"] == "partial"
    assert statuses["buy"] == "filled"

    # 审计日志覆盖全部关键动作
    logs = client.get("/api/auctions/audit-logs").json()
    actions = {l["action"] for l in logs if l["result"] == "success"}
    assert {
        "auction.session.create", "auction.bid.place",
        "auction.session.match", "auction.session.settle",
    } <= actions


def test_settle_writes_back_buyer_deficit(ctx):
    client, ids, TestingSession = ctx
    sid = _create_open_session(_as_admin(client))
    # 造买方缺口记录：核证 700，已清缴 500，缺口 200
    setup_db = TestingSession()
    setup_db.add(ComplianceRecord(
        company_id=ids["c2"], year=2025, verified_emission=700, cleared_amount=500,
        frozen_amount=0, deficit=200, status="deficit", deadline="2025-12-31", is_active=1,
    ))
    setup_db.commit()
    setup_db.close()

    client.post(f"/api/auctions/{sid}/bids", json={
        "company_id": ids["c1"], "side": "sell", "price": 75, "quantity": 300})
    client.post(f"/api/auctions/{sid}/bids", json={
        "company_id": ids["c2"], "side": "buy", "price": 80, "quantity": 300})
    client.post(f"/api/auctions/{sid}/match")
    client.post(f"/api/auctions/{sid}/settle")

    compliance = client.get("/api/compliance?year=2025").json()
    rec = [r for r in compliance if r["company_id"] == ids["c2"]][0]
    assert rec["status"] == "compliant"
    assert rec["deficit"] == 0
    assert rec["cleared_amount"] == 700
    # 买方：500 + 300 - 200（缺口补缴）= 600
    acc = client.get(f"/api/companies/{ids['c2']}/account?year=2025").json()
    assert acc["current_balance"] == 600


def test_cancel_session_releases_reserves(ctx):
    client, ids, _Session = ctx
    sid = _create_open_session(_as_admin(client))
    login(client, "ent1")
    client.post(f"/api/auctions/{sid}/bids", json={"side": "sell", "price": 80, "quantity": 250})
    login(client, "admin")
    res = client.post(f"/api/auctions/{sid}/cancel", json={"reason": "监管撤销"})
    assert res.json()["status"] == "cancelled"
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["reserved_balance"] == 0
    # 已撤销场次不能撮合
    assert client.post(f"/api/auctions/{sid}/match").status_code == 400


def test_bid_cancel_endpoint_by_owner(ctx):
    client, ids, _Session = ctx
    sid = _create_open_session(_as_admin(client))
    login(client, "ent1")
    bid = client.post(f"/api/auctions/{sid}/bids", json={
        "side": "sell", "price": 80, "quantity": 120,
    }).json()
    res = client.post(f"/api/auctions/bids/{bid['id']}/cancel", json={"reason": "改价重报"})
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["reserved_balance"] == 0

    # 撤单动作有审计
    login(client, "admin")
    logs = client.get("/api/auctions/audit-logs", params={"action": "auction.bid.cancel"}).json()
    assert any(l["result"] == "success" for l in logs)
