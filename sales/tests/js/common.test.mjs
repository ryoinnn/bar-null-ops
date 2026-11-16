// ポータルの共通部品（report/portal/common.js）のテスト。実行: cd sales; node --test tests/js/common.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const SRC = readFileSync(fileURLToPath(new URL("../../report/portal/common.js", import.meta.url)), "utf8");
function load(stored = {}) {
  const events = [];
  const win = {
    localStorage: { getItem: (k) => (k in stored ? stored[k] : null), setItem: (k, v) => { stored[k] = v; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    dispatchEvent: (e) => events.push(e),
    setTimeout, Date, Math,
  };
  win.window = win;
  vm.runInNewContext(SRC, win);
  return { BN: win.BN, events, stored };
}
const cf = (name, v) => typeof v === "number" ? { name, number_value: v } : v && v.enum ? { name, enum_value: { name: v.enum } } : { name, text_value: v };
const fixedTask = (name, o) => ({ name, custom_fields: [
  cf("金額（税込）", o.amount), cf("按分月数", o.months ?? 1), cf("適用開始月", o.start), cf("適用終了月", o.end ?? null),
  cf("固定費種別", { enum: o.kind || "通常" }), cf("固定費ステータス", { enum: o.status || "有効" }), cf("店舗", o.store ?? null), cf("固定費区分", { enum: o.cat || "その他" }) ] });
const mcpOf = (pages, log = []) => ({
  callTool: async (server, tool, input, opts) => { log.push({ tool, input, opts }); const r = pages.shift(); if (r instanceof Error || (r && r.code)) throw r; return r; },
  invalidate: async () => { log.push({ tool: "invalidate" }); },
});
const j = (x) => JSON.parse(JSON.stringify(x));   // vm の別の世界の配列・オブジェクトを、比べられる形にする
const page = (data, offset) => ({ payload: { data, next_page: offset ? { offset } : null } });

test("営業日: 6:00 より前は前日", () => {
  const { BN } = load();
  assert.equal(BN.todayBiz(Date.UTC(2026, 10, 14, 20, 59)), "2026-11-14");   // 11/15 05:59 JST
  assert.equal(BN.todayBiz(Date.UTC(2026, 10, 14, 21, 0)), "2026-11-15");    // 11/15 06:00 JST
  assert.equal(BN.todayBiz(Date.UTC(2026, 10, 30, 16, 0)), "2026-11-30");    // 12/1 01:00 JST は 11/30 の営業日
  assert.equal(BN.dayDiff("2026-11-28", "2026-12-02"), 4);
});

test("環境: 既定は本番、切り替えを保存して知らせる", () => {
  const a = load();
  assert.equal(a.BN.env.get(), "prod");
  a.BN.env.set("test");
  assert.equal(a.BN.env.get(), "test");
  assert.equal(a.stored["barnull-env"], "test");
  assert.equal(a.events.length, 1);
  a.BN.env.set("test");
  assert.equal(a.events.length, 1, "同じ値では知らせない");
  assert.equal(load({ "barnull-env": "test" }).BN.env.get(), "test");
  assert.equal(load({ "barnull-env": "x" }).BN.env.get(), "prod");
});

test("読み込みの世代: 新しい読み込みが始まると古いものは無効", () => {
  const { BN } = load();
  const a = BN.gen("p"); assert.equal(a(), true);
  const b = BN.gen("p"); assert.equal(a(), false); assert.equal(b(), true);
  assert.equal(BN.gen("q")(), true); assert.equal(b(), true, "別の名前は影響しない");
});

test("readAll: ページをつないで全件を返す", async () => {
  const { BN } = load(); const log = [];
  const out = await BN.readAll(mcpOf([page([{ gid: "1" }], "o1"), page([{ gid: "2" }])], log), "P");
  assert.deepEqual(j(out.map((t) => t.gid)), ["1", "2"]);
  assert.equal(log[1].input.offset, "o1");
  assert.equal(log[0].opts.cache.staleTime, 60000);
});

test("readAll: 形の違う応答は空にせずエラー", async () => {
  const { BN } = load();
  for (const bad of [{ payload: "Unauthorized" }, { payload: {} }, { payload: { data: "x" } }, {}, null])
    await assert.rejects(BN.readAll(mcpOf([bad]), "P"), (e) => e.code === "bad_response");
  // 2ページ目だけ壊れていても、途中までを返さない
  await assert.rejects(BN.readAll(mcpOf([page([{ gid: "1" }], "o1"), { payload: "x" }]), "P"), (e) => e.code === "bad_response");
});

test("readAll: 上限を超えたら打ち切らずにエラー", async () => {
  const { BN } = load();
  const pages = Array.from({ length: 101 }, () => page([{ gid: "x" }], "more"));
  await assert.rejects(BN.readAll(mcpOf(pages), "P"), (e) => e.code === "too_many");
});

test("readAll: 必須の項目が無ければエラー（タスクが 0 件なら通す）", async () => {
  const { BN } = load();
  const t = { custom_fields: [cf("営業日", null)] };
  await assert.rejects(BN.readAll(mcpOf([page([t])]), "P", { required: ["営業日", "伝票金額"] }), (e) => e.code === "missing_field" && e.message === "伝票金額");
  assert.equal((await BN.readAll(mcpOf([page([t])]), "P", { required: ["営業日"] })).length, 1);
  assert.equal((await BN.readAll(mcpOf([page([])]), "P", { required: ["営業日"] })).length, 0);
});

test("readAll: 再試行してよい失敗は1回だけやり直す。それ以外はそのまま失敗", async () => {
  const { BN } = load();
  const out = await BN.readAll(mcpOf([{ code: "server_unavailable", retryable: true, retryAfterMs: 1 }, page([{ gid: "1" }])]), "P");
  assert.equal(out.length, 1);
  await assert.rejects(BN.readAll(mcpOf([{ code: "needs_reauth" }, page([])]), "P"), (e) => e.code === "needs_reauth");
  await assert.rejects(BN.readAll(null, "P"), (e) => e.code === "not_granted");
});

test("writeTasks: 全件の成功だけを ok、確認できなければ uncertain、失敗は例外", async () => {
  const { BN } = load(); const tasks = [{ task: "1" }, { task: "2" }];
  assert.equal(await BN.writeTasks(mcpOf([{ payload: { succeeded: [{}, {}], failed: [] } }]), tasks, "prod"), "ok");
  assert.equal(await BN.writeTasks(mcpOf([{ payload: { succeeded: [{}] } }]), tasks, "prod"), "uncertain");
  assert.equal(await BN.writeTasks(mcpOf([{ payload: "done" }]), tasks, "prod"), "uncertain");
  await assert.rejects(BN.writeTasks(mcpOf([{ payload: { succeeded: [{}], failed: [{}] } }]), tasks, "prod"), (e) => e.code === "tool_error");
  await assert.rejects(BN.writeTasks(mcpOf([{ code: "tool_error" }]), tasks, "prod"), (e) => e.code === "tool_error");
});

test("writeTasks: 画面のデータの環境と今の環境が違えば書かない。書いたあとは読み直す", async () => {
  const { BN } = load(); const log = [];
  await assert.rejects(BN.writeTasks(mcpOf([], log), [{ task: "1" }], "test"), (e) => e.code === "env_changed");
  assert.equal(log.length, 0, "Asana を呼んでいない");
  const v = BN.version();
  await BN.writeTasks(mcpOf([{ payload: { succeeded: [{}] } }], log), [{ task: "1" }], "prod");
  assert.equal(BN.version(), v + 1);
  assert.ok(log.some((l) => l.tool === "invalidate"));
  const log2 = []; await BN.readAll(mcpOf([page([])], log2), "P");
  assert.equal(log2[0].opts.cache.refresh, true, "書き込みの直後はキャッシュを使わない");
});

test("固定費: 値上げ・実績・按分・終了・対象外・店舗（fixed.py の month_amounts と同じ規則）", () => {
  const { BN } = load();
  const tasks = [
    fixedTask("家賃", { amount: 300000, start: "2026-01" }),
    fixedTask("家賃", { amount: 330000, start: "2026-10" }),                         // 値上げ
    fixedTask("電気", { amount: 20000, start: "2026-01" }),
    fixedTask("電気", { amount: 26543, start: "2026-11", end: "2026-11", kind: "実績" }),
    fixedTask("保険", { amount: 100000, months: 12, start: "2026-04" }),              // 年払い: 8333 × 11 ＋ 8337
    fixedTask("リース", { amount: 5000, start: "2026-01", end: "2026-09" }),
    fixedTask("旧契約", { amount: 9999, start: "2026-01", status: "対象外" }),
    fixedTask("福岡家賃", { amount: 8888, start: "2026-01", store: "node" }),
    fixedTask("形式違い", { amount: 7777, start: "2026/01" }),
  ].map(BN.fixed.parse);
  const of = (m) => j(Object.fromEntries(BN.fixed.month(tasks, m).map((i) => [i.name, i.monthly])));
  assert.deepEqual(of("2026-09"), { 家賃: 300000, 電気: 20000, 保険: 8333, リース: 5000 });
  assert.deepEqual(of("2026-10"), { 家賃: 330000, 電気: 20000, 保険: 8333 });
  assert.deepEqual(of("2026-11"), { 家賃: 330000, 電気: 26543, 保険: 8333 });
  assert.deepEqual(of("2026-12"), { 家賃: 330000, 電気: 20000, 保険: 8333 });
  assert.equal(of("2027-03").保険, 8337, "区切りの最後の月に端数");
  assert.equal(of("2027-04").保険, 8333);
  assert.deepEqual(of("2025-12"), {}, "項目の無い月は未設定");
  let year = 0; for (let m = 4; m <= 15; m++) year += of(`${m > 12 ? 2027 : 2026}-${String(m > 12 ? m - 12 : m).padStart(2, "0")}`).保険;
  assert.equal(year, 100000, "12か月の合計は支払額と同じ");
});

test("固定費: 金額が空の項目は数えない。按分月数が空・0 は毎月", () => {
  const { BN } = load();
  const t = [fixedTask("A", { amount: null, start: "2026-01" }), fixedTask("B", { amount: 1000, months: 0, start: "2026-01" })].map(BN.fixed.parse);
  assert.deepEqual(j(BN.fixed.month(t, "2026-02").map((i) => [i.name, i.monthly])), [["B", 1000]]);
});

test("errText: コードごとに直し方の分かる文面", () => {
  const { BN } = load();
  assert.match(BN.errText({ code: "needs_reauth" }), /接続/);
  assert.match(BN.errText({ code: "not_granted" }), /許可/);
  assert.match(BN.errText({ code: "bad_response" }), /想定と違う応答/);
  assert.match(BN.errText({ code: "missing_field", message: "営業日" }), /「営業日」/);
  assert.match(BN.errText({ code: "too_many" }), /10000/);
  assert.match(BN.errText({ code: "zzz", message: "m" }), /読み込めませんでした/);
  assert.match(BN.errText({ code: "tool_error", message: "Forbidden" }, "Asana", "save"), /保存できませんでした.*Forbidden/);
  assert.match(BN.errText({ code: "not_granted" }, "Asana", "save"), /書き込みが許可されていません/);
});

test("readAll: 再試行は1回の読み込みにつき1回まで。待ち時間の指定が長いときは再試行しない", async () => {
  const { BN } = load();
  const again = () => ({ code: "server_unavailable", retryable: true, retryAfterMs: 1 });
  await assert.rejects(BN.readAll(mcpOf([again(), again(), page([])]), "P"), (e) => e.code === "server_unavailable", "2回目の失敗はそのまま失敗");
  // 1ページ目で再試行を使ったら、2ページ目の失敗はやり直さない
  await assert.rejects(BN.readAll(mcpOf([again(), page([{ gid: "1" }], "o1"), again(), page([])]), "P"), (e) => e.code === "server_unavailable");
  const log = [];
  await assert.rejects(BN.readAll(mcpOf([{ code: "server_unavailable", retryable: true, retryAfterMs: 60000 }, page([])], log), "P"), (e) => e.code === "server_unavailable");
  assert.equal(log.length, 1, "長く待つ指定のときは呼び直さない");
});

test("readAll: refresh と opt_fields を呼び出しに渡す", async () => {
  const { BN } = load(); const log = [];
  await BN.readAll(mcpOf([page([])], log), "P", { refresh: true, opt: "name,notes" });
  assert.equal(log[0].opts.cache.refresh, true);
  assert.equal(log[0].input.opt_fields, "name,notes");
  assert.equal(log[0].input.project, "P");
});

test("writeTasks: 失敗しても読み直しの印を付ける。0 件は何もしない", async () => {
  const { BN } = load(); const log = [], v = BN.version();
  await assert.rejects(BN.writeTasks(mcpOf([{ code: "tool_error" }], log), [{ task: "1" }], "prod"));
  assert.equal(BN.version(), v + 1);
  assert.ok(log.some((l) => l.tool === "invalidate"), "失敗しても、書けたかもしれないのでキャッシュを捨てる");
  const log2 = [];
  assert.equal(await BN.writeTasks(mcpOf([], log2), [], "prod"), "ok");
  assert.equal(log2.length, 0);
});

test("canManage: 持ち主か Editor だけ。Promise を待って判定し、分からないときは見せない", async () => {
  const { BN } = load();
  const u = (owner, edit) => ({ isOwner: async () => owner, canEdit: async () => edit });
  assert.equal(await BN.canManage(u(true, false)), true);
  assert.equal(await BN.canManage(u(false, true)), true);
  assert.equal(await BN.canManage(u(false, false)), false, "閲覧だけの人には見せない（Promise をそのまま真と見なさない）");
  assert.equal(await BN.canManage(null), false);
  assert.equal(await BN.canManage({ isOwner: async () => { throw new Error("x"); }, canEdit: async () => true }), false);
  assert.equal(await BN.canManage({ isOwner: async () => null, canEdit: async () => undefined }), false);
});

test("writeDenied: 書き込みが許可されていないコードを見分ける", () => {
  const { BN } = load();
  for (const code of ["not_granted", "not_in_manifest", "approval_required", "blocked_by_policy"]) assert.equal(BN.writeDenied({ code }), true, code);
  for (const code of ["tool_error", "server_unavailable", "env_changed"]) assert.equal(BN.writeDenied({ code }), false, code);
  assert.equal(BN.writeDenied(null), false);
});

test("項目の読み取り: テキスト・選択肢・数値・日付", () => {
  const { BN } = load();
  const f = BN.fieldsOf({ custom_fields: [cf("店舗", " node "), cf("状態", { enum: "対象外" }), cf("金額", 0), cf("空", null),
    { name: "営業日", date_value: { date: "2026-11-15" } }, { name: "日付なし", date_value: null }] });
  assert.equal(BN.txt(f["店舗"]), "node");
  assert.equal(BN.txt(f["状態"]), "対象外");
  assert.equal(BN.txt(f["空"]), ""); assert.equal(BN.txt(undefined), "");
  assert.equal(BN.num(f["金額"]), 0, "0 は 0（空ではない）");
  assert.equal(BN.num(f["空"]), null); assert.equal(BN.num(undefined), null);
  assert.equal(BN.dateOf(f["営業日"]), "2026-11-15"); assert.equal(BN.dateOf(f["日付なし"]), null);
});

test("環境: 不正な値は本番。localStorage が使えなくても動く", () => {
  const a = load(); a.BN.env.set("test"); a.BN.env.set("zzz");
  assert.equal(a.BN.env.get(), "prod");
  const win = { localStorage: { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }, dispatchEvent() {},
    CustomEvent: class { constructor(t, i) { this.detail = i.detail; } }, setTimeout, Date, Math };
  win.window = win; vm.runInNewContext(SRC, win);
  assert.equal(win.BN.env.get(), "prod"); win.BN.env.set("test"); assert.equal(win.BN.env.get(), "test");
});

test("記録ありの判定（contract.md §5 の 1〜4）", () => {
  const { BN } = load(); const c = (...x) => j(BN.coverage(...x));
  // 1. 記録が無い・最初の記録が期間より後 → 未記録
  assert.equal(c("purchase", null, null, "2026-11-01", "2026-11-15").s, "none");
  assert.equal(c("purchase", "2026-12-01", "2026-12-01", "2026-11-01", "2026-11-15").s, "none");
  // 2. 取り込みの開始日（8/1）より前から始まる期間で、最初の記録が期間の途中 → 一部のみ
  assert.deepEqual(c("purchase", "2026-08-06", "2026-11-12", "2026-07-27", "2026-08-09"), { s: "partial", from: "2026-08-06", last: "2026-11-12" });
  // 8/1 以降に始まる期間は、最初の記録が途中でも記録済み（8月を「一部のみ」のままにしない）
  assert.equal(c("purchase", "2026-08-06", "2026-11-12", "2026-08-01", "2026-08-31").s, "full");
  // 3. いちばん新しい記録が、仕入 10 日・備品 14 日以上前 → 一部のみ（取り込みの遅れ）
  assert.equal(c("purchase", "2026-08-06", "2026-11-05", "2026-11-09", "2026-11-14").s, "full", "9 日前は記録済み");
  assert.deepEqual(c("purchase", "2026-08-06", "2026-11-05", "2026-11-09", "2026-11-15"), { s: "partial", lag: true, last: "2026-11-05" });
  assert.equal(c("supplies", "2026-08-05", "2026-11-02", "2026-11-09", "2026-11-15").s, "full", "備品は 13 日前まで記録済み");
  assert.equal(c("supplies", "2026-08-05", "2026-11-01", "2026-11-09", "2026-11-15").s, "partial");
  // 4. 期間内が 0 件でも、前後に記録があれば記録済み（過去の期間は、後の記録があるので遅れにならない）
  assert.equal(c("purchase", "2026-08-06", "2026-11-12", "2026-09-28", "2026-09-30").s, "full");
});

test("固定費: 同じ項目に実績が2つ・通常が同じ開始月で2つあっても、1つだけを数える", () => {
  const { BN } = load();
  const t = [fixedTask("電気", { amount: 100, start: "2026-01" }), fixedTask("電気", { amount: 200, start: "2026-01" }),
    fixedTask("ガス", { amount: 10, start: "2026-01" }), fixedTask("ガス", { amount: 30, start: "2026-03", kind: "実績" }), fixedTask("ガス", { amount: 50, start: "2026-05", kind: "実績" })].map(BN.fixed.parse);
  const of = (m) => j(Object.fromEntries(BN.fixed.month(t, m).map((i) => [i.name, i.monthly])));
  assert.deepEqual(of("2026-02"), { 電気: 100, ガス: 10 }, "同じ開始月なら先にあるもの");
  assert.deepEqual(of("2026-04"), { 電気: 100, ガス: 30 });
  assert.deepEqual(of("2026-06"), { 電気: 100, ガス: 50 }, "実績どうしは開始月が新しいもの");
});

/* ---------- 月ごとの売上・コスト（BN.ledger。コスト管理とホームのダッシュボード） ---------- */
const dt = (name, d) => ({ name, date_value: d ? { date: d } : null });
const slip = (id, d, amt, o = {}) => ({ custom_fields: [cf("伝票ID", id), dt("営業日", d), cf("伝票金額", amt), cf("伝票ステータス", { enum: o.status || "入金済み" }),
  cf("決済手数料", o.fee ?? 0), cf("入金予定額", o.payout ?? null)] });
const payTask = (m, v, o = {}) => ({ custom_fields: [cf("対象月", m), cf("給与", v), cf("時給の分", o.hourly ?? 0), cf("給与ステータス", { enum: o.status || "確定" })] });
const purTask = (id, d, v, o = {}) => ({ custom_fields: [cf("仕入記録ID", id), dt("営業日", d), cf("仕入ステータス", { enum: o.status || "取り込み済み" }), cf("仕入金額（税込）", v), cf("店舗", o.store ?? null)] });
const supTask = (d, v, o = {}) => ({ custom_fields: [dt("営業日", d), cf("備品ステータス", { enum: o.status || "取り込み済み" }), cf("金額（税込）", v), cf("店舗", o.store ?? null), cf("備品区分", { enum: o.kind || "備品・消耗品" })] });

test("ledger: 売上・レンタル・費目を月ごとに集計し、そろった月だけ利益を出す", async () => {
  const { BN } = load(); const log = [];
  const pages = [
    page([slip("S1", "2026-10-03", 50000, { fee: 1500 }), slip("S2", "2026-10-03", 30000), slip("S3", "2026-10-10", 9999, { status: "対象外" }),
          slip("RS-IB-202610", "2026-10-31", 0, { payout: 40000 }), slip("S4", "2026-11-02", 20000)]),
    page([fixedTask("家賃", { amount: 60000, start: "2026-01" })]),
    page([payTask("2026-10", 20000), payTask("2026-11", 5000, { status: "見込み" })]),
    page([purTask("P-1", "2026-10-05", 10000), purTask("M-1", null, 0), purTask("P-2", "2026-10-20", 999, { status: "対象外" }), purTask("P-3", "2026-11-09", 3000)]),
    page([supTask("2026-08-03", 100), supTask("2026-10-07", 2000), supTask("2026-11-08", 500), supTask("2026-10-08", 777, { store: "node" })]),
  ];
  const L = BN.ledger.create("prod"); let steps = 0;
  assert.equal(await BN.ledger.load(mcpOf(pages, log), L, { step: () => steps++ }), true);
  assert.equal(steps, 5);
  assert.deepEqual(j(log.map((l) => l.input.project)), [BN.IDS.slips.prod, BN.IDS.fixed.prod, BN.IDS.payroll.prod, BN.IDS.purchase.prod, BN.IDS.supplies.prod]);
  const now = Date.UTC(2026, 10, 15, 3);   // 2026-11-15 12:00 JST
  const oct = j(BN.ledger.month(L, "2026-10", now));
  assert.equal(oct.sales, 80000); assert.equal(oct.rent, 40000); assert.equal(oct.revenue, 120000); assert.equal(oct.slipCount, 2);
  assert.equal(oct.c.fee.v, 1500); assert.equal(oct.c.labor.v, 20000); assert.equal(oct.c.purchase.v, 10000); assert.equal(oct.c.supplies.v, 2000); assert.equal(oct.fixed, 60000);
  assert.equal(oct.complete, true); assert.equal(oct.known, 93500); assert.equal(oct.profit, 26500);
  assert.deepEqual(j(L.daily["2026-10-03"]), { sales: 80000, n: 2 }, "日ごとの売上（レンタル・取消を除く）");
  const nov = j(BN.ledger.month(L, "2026-11", now));
  assert.equal(nov.c.labor.s, "est"); assert.equal(nov.est, true); assert.equal(nov.complete, true);
  assert.equal(nov.profit, 20000 - (5000 + 3000 + 500 + 0 + 60000));
  const sep = j(BN.ledger.month(L, "2026-09", now));
  assert.equal(sep.c.labor.s, "none"); assert.equal(sep.complete, false); assert.equal(sep.profit, null, "給与の無い月は利益を断定しない");
  assert.equal(sep.c.purchase.s, "none", "最初の仕入より前の月は未記録");
  assert.equal(sep.c.supplies.s, "ok"); assert.equal(sep.c.supplies.v, 0, "前後に記録があれば 0 円の記録済み");
});

test("ledger: 読めなかった費目は null（0 円にしない）。伝票だけの読み込みは費用を読まない", async () => {
  const { BN } = load();
  const L = BN.ledger.create("test");
  await BN.ledger.load(mcpOf([page([slip("S1", "2026-11-02", 20000)]), { code: "not_granted" }, page([payTask("2026-11", 1000)]), page([]), page([])]), L);
  const m = j(BN.ledger.month(L, "2026-11", Date.UTC(2026, 10, 15, 3)));
  assert.equal(m.c.fixed.s, "error"); assert.equal(m.fixed, null); assert.equal(m.complete, false); assert.equal(m.profit, null);
  assert.equal(L.fixedError.code, "not_granted");
  const V = BN.ledger.create("prod"); const log = [];
  await BN.ledger.load(mcpOf([page([slip("S1", "2026-11-02", 20000)])], log), V, { cost: false });
  assert.equal(log.length, 1, "閲覧だけの人は伝票だけを読む");
  const v = j(BN.ledger.month(V, "2026-11", Date.UTC(2026, 10, 15, 3)));
  assert.equal(v.revenue, 20000); assert.equal(v.c.labor.s, "off"); assert.equal(v.profit, null);
  // 新しい読み込みが始まったら、古い結果を書かない
  const S = BN.ledger.create("prod"); let alive = true;
  const mcp = { callTool: async () => { alive = false; return page([slip("S1", "2026-11-02", 1)]); } };
  assert.equal(await BN.ledger.load(mcp, S, { live: () => alive }), false);
  assert.equal(S.slips, null);
});
