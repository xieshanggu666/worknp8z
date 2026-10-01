# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（142 项全部通过，含多线程并发与交割/竞价结算闭环一致性测试）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据（已有旧库先执行迁移脚本）
uvicorn app.main:app --reload  # 启动服务
```

> 升级旧库（新增幂等键列与唯一约束）：`python scripts/migrate_concurrency.py`，可重复执行。
> 新增集中竞价市场（4 张新表与流水关联列）：`python scripts/migrate_auction.py`，可重复执行。

访问 http://127.0.0.1:8000

### 演示账号（密码均为 `123456`）

| 用户名    | 角色       | 说明                       |
|-----------|------------|----------------------------|
| `admin`   | 监管管理员 | 企业/因子/配额/清缴全权限   |
| `verifier`| 核查员     | 核验活动数据、批准 MRV 报告 |
| `elec`    | 控排企业   | 绿能电力集团（边界受限）    |
| `cement`  | 控排企业   | 恒固水泥股份（边界受限）    |

## 功能模块

1. **核算边界管理**：企业注册行业/地区/核算边界说明，范围一/二/三边界配置
2. **排放因子库**：因子编号、有效期、数据来源；修订自动记录版本历史
3. **活动数据台账**：企业按年度/周期录入活动量，核查员核验标记
4. **排放核算引擎**：
   - `activity_factor`：排放量 = 活动量 × 因子值
   - `fuel_combustion`：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12
   - 因子按年度生效区间取值，重复核算幂等（先清后算）
5. **配额管理**：免费配额分配（基准 + 分配量 + 调整量）、配额账户持仓、可用余额、履约冻结额与交易占用额
6. **配额交易台账**：买入/卖出/划转，实时校验**可用余额（持仓 - 履约冻结 - 交易占用）**，逐笔记录持仓、冻结与占用快照
7. **企业间交易订单**：双方挂单 → 双方确认（卖方配额转为**交易占用**）→ 交割（双方配额账户与流水同步）；交割前任一方可撤销并释放占用
8. **履约闭环（年度配额闭环）**：MRV 报告批准后按批准排放快照冻结配额；清缴优先核销冻结配额，不足部分扣减可用配额；缺口年度买入或补充分配后可补缴；**企业间订单交割默认在同一事务内自动核销买方同年度履约缺口**（先冻结核销、再用到账配额补缴），交割完成即见履约状态/配额状态/统计一致更新；挂单可通过 `auto_clear_deficit=false` 关闭联动，改为事后手动清缴；报告冲正会解冻并退还已清缴配额、归档旧履约记录
9. **MRV 报告**：年度范围一二三汇总生成，草稿 → 提交 → 批准状态流转；已批准报告不得直接覆盖，须由监管/核查角色通过冲正接口异常回滚
10. **碳配额集中竞价市场**：监管创建竞价场次（限价/年度），买方/卖方在申报窗口报价；卖单报价即把配额转为**交易占用**，申报中可撤单（释放占用）。监管触发集合竞价（最大成交量原则定统一成交价、价格/时间优先配对），再统一结算：全部成交在单一事务内卖方出库/买方到账、逐笔流水快照、未成交占用释放，并按买方合并自动核销其同年度履约缺口。场次/报价/成交全状态机（open → matched → settled / cancelled），全部敏感操作写权限审计（含越权拒绝）

### 集中竞价市场并发与一致性保障

- **全局写锁 + 场次键 + 账户/清缴键**：所有场次写操作（报价/撤单/撮合/结算/撤销）先取全局 `auction:write` 键进程内串行化，再按 `account: < auction: < clear: < order:` 全局锁序补齐涉及企业的账户键与清缴键；既消除"场次键相同但企业键集合不同"造成的锁交错，又与企业间订单、手动清缴互斥
- **卖单占用账本**：报价即原子 `reserved += 量`（数据库条件保证 `current ≥ frozen + reserved`），与企业间订单共用同一占用账本：履约冻结不能被申报卖出，已申报占用不能被报告冻结/清缴挪用；撮合只定价配对不动账，结算时成交部分 `current/reserved` 同减出库、未成交部分释放
- **状态条件 UPDATE 抢占**：撮合 open→matched、结算 matched→settled、撤销 open→cancelled 用前置状态条件 UPDATE，并发只有一方成功；撮合在写事务内抢到库级写锁后才重读报价单，消除"读报价 → 抢锁"间的撤单窗口；撤单事务内再以条件 UPDATE 复查场次仍为 open
- **结算单一事务**：全部卖方出库、买方到账、占用释放、双方流水、成交状态与买方履约缺口核销同生共死；买方按企业合并核销一次（先冻结核销、后到账自由配额补缴，复用清缴内核），失败整体回滚
- **幂等与防操纵**：报价携带幂等键去重；同一企业同一场次不得同时持有买单与卖单（防对敲）；限价区间强制校验

### 并发一致性保障（清缴 / 交易 / 企业间订单）

- **账户锁定**：进程内按键（账户 / 企业+年度履约 / 订单）串行化余额变更，多键按 `account: < clear: < order:` 全局锁序加锁防死锁；PostgreSQL/MySQL 额外加 `SELECT … FOR UPDATE` 行锁，SQLite 在写事务内以"空更新"抢占库级写锁并设置 `busy_timeout` 等待
- **原子扣减/冻结/占用**：原子 UPDATE 由数据库保证 `frozen ≥ 0`、`reserved ≥ 0`、`current ≥ frozen + reserved`；余额、冻结额、占用额与流水在同一事务提交，异常统一回滚
- **交易占用与履约冻结互不挤占**：企业间订单双方确认后，卖方配额从"可用"转为交易占用（reserved），既不能被重复卖出/划出，也不能被报告批准冻结或清缴补扣挪用；反过来已履约冻结的配额也不能被订单占用。订单撤销释放占用，交割时占用配额出库并向买方入账
- **订单状态抢占**：撤销/交割使用"状态必须为前置状态"的条件 UPDATE，并发交割与撤销只有一方成功，杜绝"已交割又释放占用"的脏账
- **交割即清缴（年度配额闭环）**：交割的卖方出库、买方到账与买方履约缺口核销共用同一事务、同一锁集合（交割锁序天然包含买方 `clear:`/`account:` 键）：先核销冻结配额，再用刚到账的自由可用配额补扣缺口（绝不触碰交易占用），履约记录、配额状态、账户余额与全部流水同生共死；交割与手动清缴并发时由企业年度键串行化，累计清缴不超过核查排放量
- **报告批准与冲正**：批准报告、创建履约记录、冻结配额同事务完成；冲正报告、归档履约、解冻/退还配额、更新配额状态同事务完成
- **重复提交**：流水、履约记录与订单均支持幂等键（请求体 `idempotency_key` 或 `Idempotency-Key` 请求头），双击 / 超时重试只入账一次；前端提交期间禁用按钮并自动生成幂等键
- **数据库兜底约束**：`quotas` 的 (企业, 年度) 唯一约束防止并发分配重复；活跃 `compliance_records` 的 (企业, 年度) 部分唯一索引允许冲正归档后重新批准；`trade_orders` 幂等键唯一约束防止重复挂单

## 数据表（18 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `compliance_records` `mrv_reports` `trade_orders` `auction_sessions` `auction_bids` `auction_trades` `audit_logs`

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（Cookie 会话） |
| GET | `/api/dashboard/stats` | 平台统计 |
| GET/POST | `/api/companies` | 企业列表/创建（admin） |
| POST | `/api/companies/{id}/scopes` | 添加核算边界（admin） |
| GET | `/api/companies/{id}/totals?year=` | 年度范围汇总 |
| GET/POST | `/api/activity` | 活动数据列表/录入 |
| POST | `/api/activity/{id}/verify` | 核验（verifier/admin） |
| GET/POST | `/api/factors` | 因子列表/创建（admin） |
| PUT | `/api/factors/{id}` | 修订因子并记版本（admin） |
| POST | `/api/companies/{id}/calculate?year=` | 触发核算 |
| GET | `/api/companies/{id}/results?year=` | 核算明细 |
| GET/POST | `/api/quotas` | 配额列表/分配（admin） |
| POST | `/api/accounts/{id}/transfer` | 配额交易 |
| GET/POST | `/api/trade-orders` | 企业间订单列表（按参与方隔离）/挂单（发起方即确认，`auto_clear_deficit` 控制交割联动清缴） |
| GET | `/api/trade-orders/{id}` | 订单详情（仅买卖双方/监管） |
| POST | `/api/trade-orders/{id}/confirm` | 参与方确认（双方确认即占用卖方配额） |
| POST | `/api/trade-orders/{id}/cancel` | 交割前撤销（confirmed 释放占用，需原因可选） |
| POST | `/api/trade-orders/{id}/deliver` | 交割：双方账户与流水同步落账，并自动核销买方同年度履约缺口（响应附 `buyer_clearance`） |
| POST | `/api/companies/{id}/clear` | 履约清缴/缺口补缴（admin） |
| GET | `/api/compliance` | 履约记录 |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告 |
| POST | `/api/reports/{id}/submit` / `/approve` | 提交/批准报告（批准即冻结配额） |
| POST | `/api/reports/{id}/reverse` | 冲正已批准报告并回滚冻结/清缴（verifier/admin，需原因） |
| GET/POST | `/api/auctions` | 竞价场次列表 / 创建场次（admin） |
| GET | `/api/auctions/audit-logs` | 竞价市场权限审计日志（仅 admin） |
| GET | `/api/auctions/{id}` | 场次详情（含买卖申报汇总） |
| POST | `/api/auctions/{id}/cancel` | 撤销申报中场次并释放全部卖单占用（admin，需原因） |
| POST | `/api/auctions/{id}/match` | 集合竞价撮合，定统一成交价并生成成交记录（admin） |
| POST | `/api/auctions/{id}/settle` | 统一结算：账户/流水落账、释放未成交占用、核销买方缺口（admin） |
| GET/POST | `/api/auctions/{id}/bids` | 场次报价单（企业仅见本企业）/ 报价（企业代表本企业，admin 代客需 company_id；卖单即占用） |
| POST | `/api/auctions/bids/{id}/cancel` | 撤单（本企业或 admin；open 期间，卖单释放占用） |
| GET | `/api/auctions/{id}/trades` | 成交记录（企业仅见本企业参与的成交） |

## 测试

```bash
python -m pytest tests/ -v   # 142 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口与补缴、交易余额校验、MRV 状态机、API 冒烟、越权防护、企业间订单全状态机（挂单/单方及双方确认/撤销释放/交割双方入账/幂等与非法流转拒绝），以及多线程并发交易/清缴/订单（无超额扣减、占用与冻结互不挤占、流水三类快照链一致、幂等键去重、失败整体回滚、交割与撤销竞争只有一方成功、清缴与交易并发三方一致）；另有交割联动清缴闭环专项测试：足额/部分/超买补缴、纯冻结记录核销、关闭联动后手动清缴、卖方义务不被触动、重复交割只核销一次、两笔订单交割与手动清缴并发后"余额 / 流水 / 履约记录 / 仪表盘统计"四方一致且年度配额守恒（持仓 + 已清缴 = 分配总量）。

集中竞价市场专项（`test_auction_*`，43 项）：场次/报价/撤单/撮合/结算/撤销全状态机与非法流转拒绝；最大成交量定价、并列时最小未平衡量/参考价规则、价格/时间优先配对、全部/部分/未成交、零可成交量；卖单占用即冻结可用、限价/反向报价/余额校验、幂等去重；结算双方入账、未成交释放、流水快照链、配额守恒、买方履约缺口足额/部分回写；角色边界（admin 全权限、企业只代表本企业且数据隔离、verifier 只读）与全部敏感操作（含越权拒绝）的权限审计；多线程并发撮合/结算/报价/撤单（竞争只有一方成功、无重复成交/重复落账、无超额占用、撮合与撤单竞争账本恒一致，连续多轮无抖动）。
