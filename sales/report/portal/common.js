/* Bar null ポータルの共通部品（window.BN）。
   index.html が shared.css と一緒に読み込み、各ページ（pages/*.html）より先に実行する。
   ここに置くもの: 読むプロジェクトの ID、環境（本番／テスト）、Asana の読み書き、固定費の計算、営業日。
   各ページは、これらを自分で持たずに BN を使う（同じものを複数のページに書かない）。
   約束は sales/docs/profit/contract.md。ID は sales/asana_ids.json と同じ値。テストは sales/tests/js/。 */
(function (root) {
  "use strict";

  /* ---------- 読むプロジェクト（Asana）。ID を変えるときはここだけ直す ---------- */
  const IDS = {
    slips:      { prod: "1219600211224482", test: "1219599891506891" },   // 伝票（売上）
    attendance: { prod: "1220540459660621", test: "1220538844505828" },   // 勤怠（contract.md §2-1）
    ledger:     { prod: "1220540292450542", test: "1220538673010390" },   // スタッフ台帳（§2-2）
    payroll:    { prod: "1220540272766453", test: "1220538739321087" },   // 給与（§2-3）
    purchase:   { prod: "1220540183336163", test: "1220538960472686" },   // 仕入（§3）
    supplies:   { prod: "1220539775332256", test: "1220539120594121" },   // 備品・消耗品（§3b）
    fixed:      { prod: "1220539995299165", test: "1220539920443902" },   // 固定費（§3c）
  };
  const SERVER = "Asana";
  const OPT = "name,permalink_url,custom_fields.name,custom_fields.number_value,custom_fields.date_value,custom_fields.enum_value.name,custom_fields.text_value";
  const PAGE_SIZE = 100, MAX_PAGES = 100;   // 1プロジェクト 10,000 件まで。超えたら黙って切らずにエラーにする

  /* ---------- 環境（本番／テスト）。全ページで1つ ---------- */
  const ENV_KEY = "barnull-env";
  const ls = {
    get(k) { try { return root.localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { root.localStorage.setItem(k, v); } catch (_) {} },
  };
  let envNow = ls.get(ENV_KEY) === "test" ? "test" : "prod";
  const env = {
    get: () => envNow,
    set(v) {
      const next = v === "test" ? "test" : "prod";
      if (next === envNow) return;
      envNow = next; ls.set(ENV_KEY, next);
      if (root.dispatchEvent && root.CustomEvent) root.dispatchEvent(new root.CustomEvent("portal:env", { detail: { env: next } }));
    },
  };

  /* ---------- 日付 ---------- */
  /* 営業日（6:00〜翌6:00、日本時間）の今日。YYYY-MM-DD */
  function todayBiz(now) {
    const j = new Date((now == null ? Date.now() : now) + 9 * 3600e3);
    if (j.getUTCHours() < 6) j.setUTCDate(j.getUTCDate() - 1);
    return j.toISOString().slice(0, 10);
  }
  const dayDiff = (a, b) => Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 864e5);

  /* ---------- エラー ---------- */
  const fail = (code, message) => ({ code, message: message || code });
  /* 画面に出す文面。コードごとに、直し方が分かるように書く。action: "save" なら保存の失敗の文面 */
  function errText(err, what, action) {
    const code = (err && err.code) || "", w = what || "Asana", save = action === "save";
    if (code === "not_granted" || code === "consent_required" || code === "consent_denied" || code === "not_in_manifest" || code === "approval_required")
      return `${w} の${save ? "書き込み" : "読み取り"}が許可されていません。ページの権限メニューから許可してください。`;
    if (code === "server_not_connected" || code === "needs_reauth" || code === "selection_required" || code === "server_not_found")
      return `claude.ai の設定で ${w} を接続（または再接続）してください。`;
    if (code === "blocked_by_policy") return `${w} の利用が組織の設定で止められています。`;
    if (code === "bad_response") return `${w} から想定と違う応答が返りました。数字は表示していません。時間をおいて読み込み直してください。`;
    if (code === "too_many") return `記録が ${PAGE_SIZE * MAX_PAGES} 件を超えたため、読み込みを止めました（途中までの数字は表示しません）。`;
    if (code === "missing_field") return `Asana の項目「${(err && err.message) || ""}」が見つかりません。項目名が変わっていないか確認してください。`;
    if (code === "env_changed") return "本番／テストを切り替えたため、保存を止めました。もう一度やり直してください。";
    if (save) return `${w} に保存できませんでした。読み直した内容を確かめてから、やり直してください。（${(err && err.message) || code || "不明なエラー"}）`;
    return `${w} を読み込めませんでした。時間をおいて開き直してください。（${(err && err.message) || code || "不明なエラー"}）`;
  }

  /* コスト管理・利益を見せてよい人か（持ち主か Editor）。isOwner・canEdit は Promise を返すので、必ず待つ（待たないと常に真になる） */
  async function canManage(user) {
    try { return !!user && ((await user.isOwner()) === true || (await user.canEdit()) === true); } catch (_) { return false; }
  }

  /* 書き込みがこのページ・この人に許可されていないときのコード（保存ボタンを止めて、Asana での編集を案内する） */
  const WRITE_DENIED = ["not_granted", "not_in_manifest", "approval_required", "consent_required", "blocked_by_policy", "capability_disabled", "capability_removed"];
  const writeDenied = (err) => WRITE_DENIED.includes(err && err.code);

  /* ---------- 読み込みの世代（古い読み込みの結果を捨てるため） ----------
     const live = BN.gen("staff"); … await …; if (!live()) return;
     同じ名前で次の gen() が呼ばれると、前の live() は false になる。 */
  const gens = {};
  function gen(name) { const n = gens[name] = (gens[name] || 0) + 1; return () => gens[name] === n; }

  /* ---------- Asana の読み込み ---------- */
  const fieldsOf = (t) => { const f = {}; ((t && t.custom_fields) || []).forEach((c) => { f[c.name] = c; }); return f; };
  const txt = (c) => String((c && (c.text_value != null ? c.text_value : (c.enum_value && c.enum_value.name))) || "").trim();
  const num = (c) => (c && typeof c.number_value === "number") ? c.number_value : null;
  const dateOf = (c) => { const d = c && c.date_value && c.date_value.date; return d ? String(d).slice(0, 10) : null; };

  let lastWrite = 0, version = 0;   // 書き込みのあとは、キャッシュを使わずに読み直す
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  /* once ＝ { used }。再試行は、1回の readAll（1プロジェクトの読み込み）につき1回まで */
  async function callRead(mcp, input, refresh, once) {
    const opts = { cache: refresh ? { refresh: true } : { staleTime: 60000 } };
    try { return await mcp.callTool(SERVER, "get_tasks", input, opts); }
    catch (err) {
      // 再試行してよいと印の付いた失敗だけ、やり直す。待ち時間の指定が長いとき（5 秒超）は、待たずに失敗として知らせる
      if (!err || !err.retryable || once.used || err.retryAfterMs > 5000) throw err;
      once.used = true;
      await sleep(err.retryAfterMs || 600 + Math.random() * 600);
      return mcp.callTool(SERVER, "get_tasks", input, opts);
    }
  }
  /* プロジェクトの全タスクを読む。
     opts.refresh: キャッシュを使わない／opts.required: 無ければエラーにする項目名／opts.opt: opt_fields（既定は OPT）
     応答の形が違う・件数が上限を超えた・必須の項目が無いときは、空として返さずに例外にする（0 円と表示させない）。 */
  async function readAll(mcp, project, opts) {
    const o = opts || {};
    if (!mcp) throw fail("not_granted");
    if (!project) throw fail("bad_response", "プロジェクトの ID がありません");
    const refresh = !!o.refresh || Date.now() - lastWrite < 120000;
    const out = [], once = { used: false }; let offset = null;
    for (let i = 0; ; i++) {
      if (i >= MAX_PAGES) throw fail("too_many");
      const input = { project, limit: PAGE_SIZE, opt_fields: o.opt || OPT };
      if (offset) input.offset = offset;
      const res = await callRead(mcp, input, refresh, once);
      const p = res && res.payload;
      if (!p || typeof p !== "object" || !Array.isArray(p.data)) throw fail("bad_response");
      out.push(...p.data);
      offset = p.next_page && p.next_page.offset;
      if (!offset) break;
    }
    if (o.required && out.length) {
      const have = new Set(); out.slice(0, 20).forEach((t) => (t.custom_fields || []).forEach((c) => have.add(c.name)));
      const miss = o.required.filter((n) => !have.has(n));
      if (miss.length) throw fail("missing_field", miss.join("・"));
    }
    return out;
  }

  /* ---------- Asana への書き込み（update_tasks） ----------
     tasks: [{task: gid, …}]（1回 50 件まで）。env: 画面のデータを読んだときの環境（今の環境と違えば書かない）。
     返り値 "ok" ＝ 全件の成功を確認／"uncertain" ＝ 結果を確認できない（読み直して確かめる）。失敗は例外。
     どの場合も、呼んだ側は読み直して実際の状態を表示する。 */
  async function writeTasks(mcp, tasks, dataEnv) {
    if (dataEnv && dataEnv !== envNow) throw fail("env_changed");
    if (!mcp) throw fail("not_granted");
    if (!tasks.length) return "ok";
    lastWrite = Date.now(); version++;
    let res;
    try { res = await mcp.callTool(SERVER, "update_tasks", { tasks }, { cache: false }); }
    finally {
      lastWrite = Date.now();
      try { if (mcp.invalidate) await mcp.invalidate(SERVER, "get_tasks"); } catch (_) {}
    }
    const p = res && res.payload;
    if (p && typeof p === "object") {
      if (Array.isArray(p.failed) && p.failed.length) throw fail("tool_error", `${p.failed.length}件を保存できませんでした`);
      if (Array.isArray(p.succeeded) && p.succeeded.length === tasks.length) return "ok";
    }
    return "uncertain";
  }

  /* ---------- 仕入・備品消耗品の「記録あり」の判定（contract.md §5 の 1〜4） ----------
     kind: "purchase" | "supplies"。first・last ＝ その種類の最初・いちばん新しい記録の営業日（無ければ null）。
     a ＝ 期間の開始日、b ＝ 期間の終了日と今日の早いほう。
     返り値 s: "none"（未記録）／"partial"（一部のみ。from ＝ 記録の始まり、または lag ＝ 取り込みの遅れ）／"full"（記録済み。期間内が 0 件でも 0 円の記録済み） */
  const COST_SINCE = { purchase: "2026-08-01", supplies: "2026-08-01" };   // 取り込みを始めた日
  const COST_LAG = { purchase: 10, supplies: 14 };   // いちばん新しい記録が、この日数以上前なら取り込みが止まっているおそれ
  function coverage(kind, first, last, a, b) {
    if (!first || first > b) return { s: "none", last: last || null };
    if (first > a && a < COST_SINCE[kind]) return { s: "partial", from: first, last };
    if (dayDiff(last, b) >= COST_LAG[kind]) return { s: "partial", lag: true, last };
    return { s: "full", last };
  }

  /* ---------- 固定費（contract.md §3c。sales/src/barnull_sales/fixed.py の month_amounts と同じ結果にする） ---------- */
  const YM = /^\d{4}-\d{2}$/;
  const monthsBetween = (a, b) => (Number(b.slice(0, 4)) - Number(a.slice(0, 4))) * 12 + Number(b.slice(5, 7)) - Number(a.slice(5, 7));
  function parseFixed(t) {
    const f = fieldsOf(t);
    return { name: String(t.name || "").trim(), url: t.permalink_url || "", category: txt(f["固定費区分"]) || "その他", amount: num(f["金額（税込）"]),
             months: num(f["按分月数"]), start: txt(f["適用開始月"]), end: txt(f["適用終了月"]) || null,
             kind: txt(f["固定費種別"]) || "通常", status: txt(f["固定費ステータス"]) || "有効", store: txt(f["店舗"]) };
  }
  const fixedActive = (t, m) => t.status !== "対象外" && !t.store && YM.test(t.start) && t.start <= m && (!t.end || m <= t.end) && t.amount != null;
  function fixedItemMonth(t, m) {   // 月額 ＝ 金額 ÷ 按分月数（切り捨て）。区切りの最後の月に端数を足す
    const n = Math.max(Math.trunc(Number(t.months) || 1), 1), total = Math.trunc(t.amount), base = Math.floor(total / n), k = monthsBetween(t.start, m) % n;
    return base + (k === n - 1 ? total - base * n : 0);
  }
  /* 月 m（YYYY-MM）の固定費。[{name, monthly, category, kind, task}]。空 ＝ 未設定（0 円ではない） */
  function fixedMonth(tasks, m) {
    const picked = {};
    tasks.forEach((t) => {
      if (!fixedActive(t, m)) return;
      const cur = picked[t.name];
      if (!cur) picked[t.name] = t;
      else if (cur.kind !== "実績" && t.kind === "実績") picked[t.name] = t;                 // 実績があれば実績
      else if (cur.kind === t.kind && t.start > cur.start) picked[t.name] = t;               // 同じ種別なら適用開始月が新しいもの
    });
    return Object.values(picked).map((t) => ({ name: t.name, monthly: fixedItemMonth(t, m), category: t.category, kind: t.kind, task: t }));
  }
  const FIXED_FIELDS = ["金額（税込）", "適用開始月", "固定費ステータス"];

  /* ---------- 月ごとの売上・コスト（contract.md §1b・§4・§5。コスト管理とホームのダッシュボードが同じものを使う） ----------
     const L = BN.ledger.create(env); await BN.ledger.load(mcp, L, { live, step, cost });
     cost: true なら費用（固定費・給与・仕入・備品消耗品）も読む（持ち主・Editor だけ）。false なら伝票だけ（閲覧だけの人）。
     BN.ledger.month(L, "YYYY-MM") → その月の売上・費目ごとの値と状態。読めなかった費目は値を null にする（0 円にしない） */
  const VAR_KEYS = ["labor", "purchase", "supplies", "fee"];
  const COST_KINDS = ["fixed", "payroll", "purchase", "supplies"];
  const daysIn = (m) => new Date(Date.UTC(+m.slice(0, 4), +m.slice(5, 7), 0)).getUTCDate();
  function ledgerCreate(env) {
    const L = { env, slips: null, rentals: null, daily: null, lastSlip: null, error: null,
      payroll: null, purchase: null, supplies: null, fixedTasks: null, purchaseFirst: null, purchaseLast: null, suppliesFirst: null, suppliesLast: null };
    COST_KINDS.forEach((k) => { L[k + "State"] = "wait"; L[k + "Error"] = null; });
    return L;
  }
  async function ledgerLoad(mcp, L, o) {
    const live = (o && o.live) || (() => true), step = (o && o.step) || (() => {}), withCost = !(o && o.cost === false);
    if (!withCost) COST_KINDS.forEach((k) => { L[k + "State"] = "off"; });
    const read = (key, required) => readAll(mcp, IDS[key][L.env], { required });
    try {   // 伝票: 通常営業は伝票金額（取消＝対象外を除き、未収を含む）、レンタルは RS- の「入金予定額」（月次確定値）
      const tasks = await read("slips", ["営業日", "伝票金額", "伝票ステータス", "伝票ID", "決済手数料", "入金予定額"]);
      if (!live()) return false;
      const slips = {}, rentals = {}, daily = {}; let last = null;
      tasks.forEach((t) => {
        const f = fieldsOf(t), date = dateOf(f["営業日"]); if (!date) return;
        if (txt(f["伝票ステータス"]) === "対象外") return;
        const m = date.slice(0, 7);
        if (txt(f["伝票ID"]).startsWith("RS-")) { rentals[m] = (rentals[m] || 0) + (num(f["入金予定額"]) || 0); return; }
        if (!last || date > last) last = date;
        const amt = num(f["伝票金額"]); if (amt == null) return;
        const x = slips[m] = slips[m] || { sales: 0, fee: 0, n: 0 }; x.sales += amt; x.fee += num(f["決済手数料"]) || 0; x.n++;
        const d = daily[date] = daily[date] || { sales: 0, n: 0 }; d.sales += amt; d.n++;
      });
      L.slips = slips; L.rentals = rentals; L.daily = daily; L.lastSlip = last;
    } catch (e) { if (!live()) return false; L.error = e; }
    step();
    if (!withCost) return true;
    try {   // 固定費（§3c）
      const tasks = await read("fixed", FIXED_FIELDS);
      if (!live()) return false;
      L.fixedTasks = tasks.map(parseFixed); L.fixedState = "ok";
    } catch (e) { if (!live()) return false; L.fixedState = "error"; L.fixedError = e; }
    step();
    try {   // 人件費 ＝ 給与の合計（§2-5）。「給与」が空の記録は時給の分を数え、その月は「一部のみ」にする
      const tasks = await read("payroll", ["対象月", "給与", "時給の分"]);
      if (!live()) return false;
      const pay = {};
      tasks.forEach((t) => {
        const f = fieldsOf(t), m = txt(f["対象月"]); if (!YM.test(m)) return;
        const x = pay[m] = pay[m] || { total: 0, n: 0, est: false, partial: false }, v = num(f["給与"]);
        if (v == null) x.partial = true;
        x.total += v != null ? v : (num(f["時給の分"]) || 0); x.n++;
        if (txt(f["給与ステータス"]) === "見込み") x.est = true;
      });
      L.payroll = pay; L.payrollState = "ok";
    } catch (e) { if (!live()) return false; L.payrollState = "error"; L.payrollError = e; }
    step();
    try {   // 仕入（§3）: 仕入記録ID が P- の伝票。対象外を除き、店舗が空のもの。金額は「仕入金額（税込）」（返品はマイナス）
      const tasks = await read("purchase", ["仕入記録ID", "営業日", "仕入ステータス", "仕入金額（税込）", "店舗"]);
      if (!live()) return false;
      const pur = {}; let first = null, last = null;
      tasks.forEach((t) => {
        const f = fieldsOf(t), date = dateOf(f["営業日"]);
        if (!txt(f["仕入記録ID"]).startsWith("P-") || !date || txt(f["仕入ステータス"]) === "対象外" || txt(f["店舗"])) return;
        const x = pur[date.slice(0, 7)] = pur[date.slice(0, 7)] || { total: 0, partial: false }, v = num(f["仕入金額（税込）"]);
        if (v == null) x.partial = true; else x.total += v;
        if (!last || date > last) last = date;
        if (!first || date < first) first = date;
      });
      L.purchase = pur; L.purchaseFirst = first; L.purchaseLast = last; L.purchaseState = "ok";
    } catch (e) { if (!live()) return false; L.purchaseState = "error"; L.purchaseError = e; }
    step();
    try {   // 備品・消耗品（§3b）: 営業日がある、備品ステータスが対象外でない、店舗が空。金額は「金額（税込）」（返金はマイナス）
      const tasks = await read("supplies", ["営業日", "備品ステータス", "金額（税込）", "店舗"]);
      if (!live()) return false;
      const sup = {}; let first = null, last = null;
      tasks.forEach((t) => {
        const f = fieldsOf(t), date = dateOf(f["営業日"]);
        if (!date || txt(f["備品ステータス"]) === "対象外" || txt(f["店舗"])) return;
        const m = date.slice(0, 7), x = sup[m] = sup[m] || { total: 0, kinds: {}, partial: false }, v = num(f["金額（税込）"]), k = txt(f["備品区分"]) || "備品・消耗品";
        if (v == null) x.partial = true; else { x.total += v; x.kinds[k] = (x.kinds[k] || 0) + v; }
        if (!last || date > last) last = date;
        if (!first || date < first) first = date;
      });
      L.supplies = sup; L.suppliesFirst = first; L.suppliesLast = last; L.suppliesState = "ok";
    } catch (e) { if (!live()) return false; L.suppliesState = "error"; L.suppliesError = e; }
    step();
    return true;
  }
  /* 月 m の固定費。s: ok／none（項目が無い＝未設定）／wait／error／off */
  function ledgerFixed(L, m) {
    if (L.fixedState !== "ok") return { s: L.fixedState, total: null, items: [] };
    const items = fixedMonth(L.fixedTasks, m);
    if (!items.length) return { s: "none", total: null, items: [] };
    return { s: "ok", total: items.reduce((x, i) => x + i.monthly, 0), items };
  }
  /* 仕入・備品消耗品の月の状態（§5 の 1〜4）。金額が空の記録がある月は「一部のみ」 */
  function ledgerRecorded(L, kind, m, rec, today) {
    const end = `${m}-${String(daysIn(m)).padStart(2, "0")}`;
    const cov = coverage(kind, L[kind + "First"], L[kind + "Last"], `${m}-01`, end < today ? end : today);
    if (cov.s === "none") return { s: "none", v: null };
    return { s: cov.s === "partial" || (rec && rec.partial) ? "partial" : "ok", v: rec ? rec.total : 0, kinds: rec && rec.kinds };
  }
  /* 月 m の売上・コスト。c[費目] = { s: ok／est（見込み）／partial／none／wait／error／off, v: 金額 or null }。c.rent はレンタルの確定の状態。
     complete ＝ 変動費の全費目と固定費がそろった（見込みを含む）。settled ＝ さらにレンタルも確定。
     profit は settled の月だけ（§5。そろわない月は利益を断定しない）。ref ＝ 費用がそろい、レンタルだけ未確定の月の参考値 */
  function ledgerMonth(L, m, now) {
    const today = todayBiz(now);
    const sl = L.slips ? (L.slips[m] || { sales: 0, fee: 0, n: 0 }) : null, rent = L.rentals ? L.rentals[m] : undefined;
    const pay = L.payroll ? L.payroll[m] : null, pur = L.purchase ? L.purchase[m] : null, sup = L.supplies ? L.supplies[m] : null;
    const fx = ledgerFixed(L, m);
    const c = {
      labor: L.payrollState === "ok" ? (pay ? { s: pay.partial ? "partial" : pay.est ? "est" : "ok", v: pay.total } : { s: "none", v: null }) : { s: L.payrollState, v: null },
      purchase: L.purchaseState === "ok" ? ledgerRecorded(L, "purchase", m, pur, today) : { s: L.purchaseState, v: null },
      supplies: L.suppliesState === "ok" ? ledgerRecorded(L, "supplies", m, sup, today) : { s: L.suppliesState, v: null },
      fee: sl ? { s: "ok", v: sl.fee } : { s: L.error ? "error" : "wait", v: null },
      fixed: { s: fx.s, v: fx.total, f: fx },
    };
    const okS = (s) => s === "ok" || s === "est";
    const anyVar = VAR_KEYS.some((k) => c[k].v != null), any = anyVar || c.fixed.v != null;
    const varKnown = VAR_KEYS.reduce((x, k) => x + (c[k].v || 0), 0), known = varKnown + (c.fixed.v || 0);
    const complete = VAR_KEYS.every((k) => okS(c[k].s)) && okS(c.fixed.s);
    const revenue = sl ? sl.sales + (rent || 0) : null;
    /* レンタルの店舗収益の状態（§5）。その月の確定値がある・より後の月に確定値がある（その月はレンタル無し＝0円）なら ok。
       いちばん新しい確定月より後の月は「未確定」で、利益を断定しない（参考値だけ） */
    const rentMonths = L.rentals ? Object.keys(L.rentals) : [];
    const rentS = !L.rentals ? (L.error ? "error" : "wait") : rent != null || rentMonths.some((x) => x > m) ? "ok" : "none";
    c.rent = { s: rentS, v: rentS === "ok" ? (rent || 0) : null };
    const settled = complete && rentS === "ok";
    return { m, c, sales: sl ? sl.sales : null, slipCount: sl ? sl.n : null, rent, revenue,
      varKnown: anyVar ? varKnown : null, fixed: c.fixed.v, known: any ? known : null, complete, settled, est: c.labor.s === "est",
      profit: settled && revenue != null ? revenue - known : null,
      ref: complete && revenue != null ? revenue - known : null };   // 費用はそろい、レンタルだけ未確定の月の参考値（費用が欠けた月は大きく出て誤解を招くので出さない）
  }
  const LEDGER_STATE = { ok: "記録済み", est: "見込み", partial: "一部のみ", none: "記録なし", wait: "読み込み中", error: "読めませんでした", off: "—" };

  /* ポータルの版（X.Y.Z。2026-11-16 本人指定。契約 §1b）。公開のたびに上げる。
     X: 画面の構成・見た目を大きく変える、契約の互換が崩れる／Y: 画面・機能を足す／Z: 修正・文言・小さな見た目。
     version()（書き込みの回数）とは別のもの */
  const PORTAL_VERSION = "3.0.0";

  root.BN = Object.freeze({
    VERSION: PORTAL_VERSION,
    IDS, SERVER, OPT, env, todayBiz, dayDiff, fail, errText, writeDenied, canManage, gen,
    fieldsOf, txt, num, dateOf, readAll, writeTasks, coverage,
    version: () => version,
    fixed: Object.freeze({ parse: parseFixed, month: fixedMonth, FIELDS: FIXED_FIELDS }),
    ledger: Object.freeze({ create: ledgerCreate, load: ledgerLoad, month: ledgerMonth, fixed: ledgerFixed, VAR: VAR_KEYS, STATE: LEDGER_STATE, daysIn }),
  });
})(typeof window !== "undefined" ? window : globalThis);
