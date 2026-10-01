const auctionStatusMap = {
  open: ["info", "申报中"],
  matched: ["warn", "已撮合待结算"],
  settled: ["ok", "已结算"],
  cancelled: ["muted", "已撤销"],
};
const bidStatusMap = {
  active: ["info", "有效"],
  filled: ["ok", "全部成交"],
  partial: ["warn", "部分成交"],
  unfilled: ["muted", "未成交"],
  cancelled: ["muted", "已撤销"],
};
const sideMap = { buy: "买入", sell: "卖出" };

views.AuctionsView = () => {
  const user = window.__user;
  const isAdmin = user.role === "admin";
  const [sessions, setSessions] = React.useState([]);
  const [selId, setSelId] = React.useState(null);
  const [detail, setDetail] = React.useState(null);
  const [bids, setBids] = React.useState([]);
  const [trades, setTrades] = React.useState([]);
  const [logs, setLogs] = React.useState([]);
  const [tab, setTab] = React.useState("bids");
  const [yearFilter, setYearFilter] = React.useState("");
  const [companies, setCompanies] = React.useState([]);
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [submitting, setSubmitting] = React.useState(false);
  const [sessionForm, setSessionForm] = React.useState({
    year: 2025, name: "", floor: "", ceiling: "", remark: "",
  });
  const [bidForm, setBidForm] = React.useState({ side: "buy", price: "", quantity: "", remark: "" });

  const loadSessions = React.useCallback(async () => {
    const p = new URLSearchParams();
    if (yearFilter) p.set("year", yearFilter);
    try { setSessions(await api.get(`/api/auctions?${p.toString()}`)); }
    catch (e) { setMsg({ type: "err", text: e.message }); }
  }, [yearFilter]);

  React.useEffect(() => { api.get("/api/companies").then(setCompanies).catch(() => {}); }, []);
  React.useEffect(() => { loadSessions(); }, [loadSessions]);

  const loadDetail = React.useCallback(async (id) => {
    if (!id) { setDetail(null); setBids([]); setTrades([]); return; }
    const [d, bs, ts] = await Promise.all([
      api.get(`/api/auctions/${id}`),
      api.get(`/api/auctions/${id}/bids`),
      api.get(`/api/auctions/${id}/trades`),
    ]);
    setDetail(d); setBids(bs); setTrades(ts);
    if (isAdmin) {
      api.get("/api/auctions/audit-logs", { }).catch(() => {});
    }
  }, [isAdmin]);

  React.useEffect(() => {
    if (selId) loadDetail(selId);
    else { setDetail(null); setBids([]); setTrades([]); }
  }, [selId, loadDetail]);

  const loadLogs = async () => {
    try { setLogs(await api.get("/api/auctions/audit-logs?limit=300")); }
    catch (e) { setMsg({ type: "err", text: e.message }); }
  };
  React.useEffect(() => { if (isAdmin && tab === "logs") loadLogs(); }, [isAdmin, tab]);

  const setS = (k) => (e) => setSessionForm({ ...sessionForm, [k]: e.target.value });
  const setB = (k) => (e) => setBidForm({ ...bidForm, [k]: e.target.value });

  const createSession = async (e) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const r = await api.post("/api/auctions", {
        year: Number(sessionForm.year),
        name: sessionForm.name,
        price_floor: Number(sessionForm.floor || 0),
        price_ceiling: Number(sessionForm.ceiling || 0),
        remark: sessionForm.remark,
      }, api.idemKey());
      setMsg({ type: "ok", text: `场次 ${r.session_no} 已创建，进入申报阶段` });
      setSessionForm({ year: sessionForm.year, name: "", floor: "", ceiling: "", remark: "" });
      await loadSessions();
      setSelId(r.id);
    } catch (err) { setMsg({ type: "err", text: err.message }); }
    finally { setSubmitting(false); }
  };

  const placeBid = async (e) => {
    e.preventDefault();
    if (submitting || !detail) return;
    if (!bidForm.price || !bidForm.quantity) { setMsg({ type: "err", text: "请填写报价与数量" }); return; }
    setSubmitting(true);
    try {
      const body = {
        side: bidForm.side,
        price: Number(bidForm.price),
        quantity: Number(bidForm.quantity),
        remark: bidForm.remark,
      };
      if (isAdmin) {
        const cid = Number(prompt("监管代客报价：请输入企业 ID（" + companies.map((c) => `${c.id}=${c.name}`).join("，") + "）"));
        if (!cid) { setSubmitting(false); return; }
        body.company_id = cid;
      }
      const r = await api.post(`/api/auctions/${detail.id}/bids`, body, api.idemKey());
      setMsg({
        type: "ok",
        text: `报价单 #${r.id} 已提交${r.side === "sell" ? "，对应配额已转为交易占用" : ""}`,
      });
      setBidForm({ side: bidForm.side, price: "", quantity: "", remark: "" });
      await loadDetail(detail.id);
      await loadSessions();
    } catch (err) { setMsg({ type: "err", text: err.message }); }
    finally { setSubmitting(false); }
  };

  const adminAction = async (action, label, needReason = false) => {
    if (!detail) return;
    if (!confirm(`确认对场次 ${detail.session_no} 执行「${label}」？`)) return;
    let body = null;
    if (needReason) {
      const reason = prompt("撤销原因（可留空）") || "";
      if (reason === null) return;
      body = { reason };
    }
    try {
      const r = await api.post(`/api/auctions/${detail.id}/${action}`, body || {}, api.idemKey());
      const extra = action === "match"
        ? (r.clear_price ? `；统一成交价 ${fmtNum(r.clear_price)} 元/t，成交量 ${fmtNum(r.matched_volume, 4)} 吨` : "；无可成交报价")
        : action === "settle" ? `；结算 ${fmtNum(r.settled_volume, 4)} 吨，账户/流水/履约缺口已回写` : "";
      setMsg({ type: "ok", text: `场次 ${r.session_no} 已${label}${extra}` });
      await loadDetail(detail.id);
      await loadSessions();
    } catch (err) { setMsg({ type: "err", text: err.message }); }
  };

  const cancelBid = async (b) => {
    const reason = prompt("撤单原因（可留空）") || "";
    if (reason === null) return;
    try {
      await api.post(`/api/auctions/bids/${b.id}/cancel`, { reason }, api.idemKey());
      setMsg({ type: "ok", text: `报价单 #${b.id} 已撤销${b.side === "sell" ? "，占用配额已释放" : ""}` });
      await loadDetail(detail.id);
      await loadSessions();
    } catch (err) { setMsg({ type: "err", text: err.message }); }
  };

  const myBids = bids;
  const canBid = detail && detail.status === "open" && user.role !== "verifier";
  const companyName = (id) => companies.find((c) => c.id === id)?.name || id;

  return html`
    <div class="panel">
      <h3>竞价场次</h3>
      <div class="filter-bar">
        <div class="field"><label>年度</label>
          <select value=${yearFilter} onChange=${(e) => setYearFilter(e.target.value)}>
            <option value="">全部</option>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option key=${y} value=${y}>${y}</option>`)}
          </select>
        </div>
      </div>
      <table>
        <thead><tr>
          <th>场次号</th><th>名称</th><th>年度</th><th>状态</th>
          <th>买/卖申报 (t)</th><th>统一成交价</th><th>撮合量 (t)</th><th>结算量 (t)</th><th></th>
        </tr></thead>
        <tbody>
          ${sessions.map((s) => html`
            <tr key=${s.id} style=${selId === s.id ? { background: "rgba(76,158,217,0.12)" } : {}}>
              <td class="mono">${s.session_no}</td>
              <td>${s.name || "-"}</td>
              <td>${s.year}</td>
              <td>${html([StatusBadge(s.status)])}</td>
              <td>${fmtNum(s.buy_quantity, 0)} / ${fmtNum(s.sell_quantity, 0)}</td>
              <td>${s.clear_price !== null ? fmtNum(s.clear_price) + " 元" : "-"}</td>
              <td>${fmtNum(s.matched_volume, 4)}</td>
              <td>${fmtNum(s.settled_volume, 4)}</td>
              <td><button class="btn sm" onClick=${() => setSelId(selId === s.id ? null : s.id)}>
                ${selId === s.id ? "收起" : "查看"}
              </button></td>
            </tr>`)}
          ${sessions.length === 0 && html`<tr><td colspan="9" class="empty">暂无竞价场次</td></tr>`}
        </tbody>
      </table>
    </div>

    ${isAdmin && html`
    <div class="panel">
      <h3>创建竞价场次（监管）</h3>
      <form class="form-grid" onSubmit=${createSession}>
        <div class="field"><label>年度</label>
          <select value=${sessionForm.year} onChange=${setS("year")}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option key=${y} value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>场次名称</label><input value=${sessionForm.name} onChange=${setS("name")} placeholder="如 2025 年度第四季度集中竞价" /></div>
        <div class="field"><label>报价下限 (元/t)</label><input type="number" min="0" step="0.01" value=${sessionForm.floor} onChange=${setS("floor")} placeholder="0=不限" /></div>
        <div class="field"><label>报价上限 (元/t)</label><input type="number" min="0" step="0.01" value=${sessionForm.ceiling} onChange=${setS("ceiling")} placeholder="0=不限" /></div>
        <div class="field"><label>备注</label><input value=${sessionForm.remark} onChange=${setS("remark")} /></div>
        <div class="actions"><button class="btn" type="submit" disabled=${submitting}>${submitting ? "提交中…" : "创建场次（开放申报）"}</button></div>
      </form>
    </div>`}

    ${detail && html`
    <div class="panel">
      <h3>场次 ${detail.session_no} ${detail.name ? "· " + detail.name : ""}
        <span style=${{ marginLeft: 8 }}>${html([StatusBadge(detail.status)])}</span>
      </h3>
      <div class="cards">
        <div class="card"><div class="label">年度</div><div class="value">${detail.year}</div></div>
        <div class="card"><div class="label">报价区间</div><div class="value">${fmtNum(detail.price_floor)} ~ ${fmtNum(detail.price_ceiling)} 元</div></div>
        <div class="card"><div class="label">统一成交价</div><div class="value">${detail.clear_price !== null ? fmtNum(detail.clear_price) + " 元/t" : "-"}</div></div>
        <div class="card"><div class="label">撮合/结算量</div><div class="value">${fmtNum(detail.matched_volume, 4)} / ${fmtNum(detail.settled_volume, 4)} t</div></div>
      </div>

      <div class="filter-bar" style=${{ marginTop: 12 }}>
        <button class=${`btn sm ${tab === "bids" ? "" : "muted"}`} onClick=${() => setTab("bids")}>报价单（${bids.length}）</button>
        <button class=${`btn sm ${tab === "trades" ? "" : "muted"}`} onClick=${() => setTab("trades")}>成交记录（${trades.length}）</button>
        ${isAdmin && html`<button class=${`btn sm ${tab === "logs" ? "" : "muted"}`} onClick=${() => setTab("logs")}>权限审计</button>`}
        <span style=${{ marginLeft: "auto" }}>
          ${isAdmin && detail.status === "open" && html`
            <button class="btn sm" onClick=${() => adminAction("match", "撮合")}>执行撮合</button>
            <button class="btn sm danger" onClick=${() => adminAction("cancel", "撤销场次", true)}>撤销场次</button>`}
          ${isAdmin && detail.status === "matched" && html`
            <button class="btn" onClick=${() => adminAction("settle", "统一结算")}>统一结算</button>`}
        </span>
      </div>

      ${canBid && html`
      <form class="form-grid" onSubmit=${placeBid} style=${{ marginTop: 12 }}>
        <div class="field"><label>方向</label>
          <select value=${bidForm.side} onChange=${setB("side")}>
            <option value="buy">买方（求购，不占用配额）</option>
            <option value="sell">卖方（卖出，报价即占用配额）</option>
          </select>
        </div>
        <div class="field"><label>报价 (元/t)</label><input type="number" min="0" step="0.01" value=${bidForm.price} onChange=${setB("price")} required /></div>
        <div class="field"><label>数量 (t)</label><input type="number" min="0" step="0.0001" value=${bidForm.quantity} onChange=${setB("quantity")} required /></div>
        <div class="field"><label>备注</label><input value=${bidForm.remark} onChange=${setB("remark")} /></div>
        <div class="actions"><button class="btn" type="submit" disabled=${submitting}>${submitting ? "提交中…" : "提交报价"}</button></div>
      </form>
      <div class="empty" style=${{ textAlign: "left", marginTop: 6 }}>
        卖单报价成功后对应配额转为<b>交易占用</b>（持仓不变、不可再卖/被冻结），
        成交后随统一结算出库，未成交或撤单自动释放；买单仅登记意向。
        同一企业在同一场次不能同时持有买单与卖单。
      </div>`}

      ${tab === "bids" && html`
      <table style=${{ marginTop: 10 }}>
        <thead><tr><th>#</th><th>企业</th><th>方向</th><th>报价</th><th>申报 (t)</th><th>已成交 (t)</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          ${myBids.map((b) => html`
            <tr key=${b.id}>
              <td>${b.id}</td>
              <td>${b.company_name}</td>
              <td>${b.side === "buy" ? html`<span class="badge info">买入</span>` : html`<span class="badge warn">卖出</span>`}</td>
              <td>${fmtNum(b.price)} 元</td>
              <td>${fmtNum(b.quantity, 4)}</td>
              <td>${fmtNum(b.filled_quantity, 4)}</td>
              <td>${(() => { const m = bidStatusMap[b.status] || ["muted", b.status]; return html`<span class="badge ${m[0]}">${m[1]}</span>`; })()}</td>
              <td>${b.status === "active" && detail.status === "open" && (isAdmin || b.company_id === user.company_id)
                ? html`<button class="btn sm danger" onClick=${() => cancelBid(b)}>撤单</button>`
                : html`<span class="muted">-</span>`}</td>
            </tr>`)}
          ${myBids.length === 0 && html`<tr><td colspan="8" class="empty">暂无报价单</td></tr>`}
        </tbody>
      </table>`}

      ${tab === "trades" && html`
      <table style=${{ marginTop: 10 }}>
        <thead><tr><th>成交号</th><th>卖方</th><th>买方</th><th>成交价</th><th>数量 (t)</th><th>成交额 (元)</th><th>结算状态</th><th>时间</th></tr></thead>
        <tbody>
          ${trades.map((t) => html`
            <tr key=${t.id}>
              <td class="mono">${t.trade_no}</td>
              <td>${t.seller_name}</td>
              <td>${t.buyer_name}</td>
              <td>${fmtNum(t.price)} 元</td>
              <td>${fmtNum(t.quantity, 4)}</td>
              <td>${fmtNum(t.amount)}</td>
              <td>${t.status === "settled" ? html`<span class="badge ok">已结算</span>` : html`<span class="badge warn">待结算</span>`}</td>
              <td>${(t.settled_at || t.created_at || "").toString().slice(0, 19).replace("T", " ")}</td>
            </tr>`)}
          ${trades.length === 0 && html`<tr><td colspan="8" class="empty">${detail.status === "open" ? "尚未撮合" : "本场无成交"}</td></tr>`}
        </tbody>
      </table>`}

      ${tab === "logs" && isAdmin && html`
      <table style=${{ marginTop: 10 }}>
        <thead><tr><th>时间</th><th>用户</th><th>角色</th><th>动作</th><th>对象</th><th>结果</th><th>详情</th><th>IP</th></tr></thead>
        <tbody>
          ${logs.map((l) => html`
            <tr key=${l.id}>
              <td class="mono">${(l.created_at || "").toString().slice(0, 19).replace("T", " ")}</td>
              <td>${l.username}</td>
              <td>${l.role}</td>
              <td class="mono" style=${{ fontSize: 12 }}>${l.action}</td>
              <td>${l.target_type}${l.target_id ? "#" + l.target_id : ""}</td>
              <td>${l.result === "success" ? html`<span class="badge ok">成功</span>` : html`<span class="badge danger">拒绝</span>`}</td>
              <td style=${{ fontSize: 12 }}>${l.detail}</td>
              <td>${l.ip}</td>
            </tr>`)}
          ${logs.length === 0 && html`<tr><td colspan="8" class="empty">暂无审计日志</td></tr>`}
        </tbody>
      </table>`}
    </div>`}

    ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
  `;
};
