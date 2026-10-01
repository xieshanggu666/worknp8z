from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class CompanyIn(BaseModel):
    code: str
    name: str
    industry: str = ""
    region: str = ""
    boundary_desc: str = ""


class ScopeIn(BaseModel):
    scope: str = Field(pattern="^[123]$")
    category: str = ""
    name: str = ""
    description: str = ""


class ActivityIn(BaseModel):
    scope_id: int
    year: int
    period: str = "monthly"
    activity_type: str
    unit: str = ""
    quantity: float
    data_source: str = ""


class FactorIn(BaseModel):
    factor_code: str
    name: str
    scope: str = Field(default="1", pattern="^[123]$")
    unit: str = "tCO2/单位"
    value: float
    source: str = ""
    valid_from: str = ""
    valid_to: str | None = None


class QuotaIn(BaseModel):
    company_id: int
    year: int
    baseline: float = 0
    allocation_amount: float
    adjustment: float = 0


class TransferIn(BaseModel):
    amount: float
    tx_type: str = "sell"
    counterparty: str = ""
    price: float | None = None
    tx_date: str = ""
    remark: str = ""
    # 客户端幂等键：同账户相同键的重复提交只入账一次（也可用 Idempotency-Key 请求头）
    idempotency_key: str | None = None


class TradeOrderIn(BaseModel):
    seller_id: int
    buyer_id: int
    year: int
    amount: float = Field(gt=0)
    price: float = Field(default=0, ge=0)
    # 发起方：seller=卖方挂单 / buyer=买方求购，发起方建单即视为已确认
    initiator: str = Field(default="seller", pattern="^(seller|buyer)$")
    tx_date: str = ""
    remark: str = ""
    idempotency_key: str | None = None
    # 交割时是否自动用买方到账配额核销其同年度履约缺口（默认开启，年度配额闭环）
    auto_clear_deficit: bool = True


class TradeOrderCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)


class ReportReversalIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


# ---------------------------------------------------------------------------
# 集中竞价市场
# ---------------------------------------------------------------------------


class AuctionSessionIn(BaseModel):
    year: int
    name: str = ""
    price_floor: float = Field(default=0, ge=0)
    price_ceiling: float = Field(default=0, ge=0)
    bid_start_at: str | None = None  # ISO 8601，空表示立即开始
    bid_end_at: str | None = None
    remark: str = ""


class AuctionBidIn(BaseModel):
    company_id: int | None = None  # 企业用户忽略并强制为本企业；监管代客报价时必填
    side: str = Field(pattern="^(buy|sell)$")
    price: float = Field(ge=0)
    quantity: float = Field(gt=0)
    remark: str = ""
    idempotency_key: str | None = None


class AuctionCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)

