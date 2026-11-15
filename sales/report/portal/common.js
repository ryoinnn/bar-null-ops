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

  root.BN = Object.freeze({
    IDS, SERVER, OPT, env, todayBiz, dayDiff, fail, errText, writeDenied, canManage, gen,
    fieldsOf, txt, num, dateOf, readAll, writeTasks, coverage,
    version: () => version,
    fixed: Object.freeze({ parse: parseFixed, month: fixedMonth, FIELDS: FIXED_FIELDS }),
  });
})(typeof window !== "undefined" ? window : globalThis);
