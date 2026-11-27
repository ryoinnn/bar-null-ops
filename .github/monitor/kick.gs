// null システムの外部監視（GitHub Actions の system-monitor）を 15 分ごとに起こす Google Apps Script（2026-11-27）。
// GitHub の定時実行（schedule）は間引かれ、15 分ごとの指定でも 6〜9 時間おきにしか動かないため。
// 置き場所: 本人の Google アカウントの Apps Script「null 外部監視の起動」（GCP の barnull-sales の外なので、課金停止でも動く）。
// 入れ方は sales/docs/runbook.md「システム監視」の「最初に一度だけ」の 5。このファイルは控え（GitHub からは動かない）。
// スクリプト プロパティ GITHUB_TOKEN: GitHub の fine-grained トークン（リポジトリ ryoinnn/bar-null-ops だけ、Actions: Read and write だけ）。
// 止まると worker の monitor_watch が 3 時間で #null_system-monitor に知らせる（トークンの期限切れなど）。

const REPO = 'ryoinnn/bar-null-ops';
const WORKFLOW = 'system-monitor.yml';

// 15 分ごとのトリガーが呼ぶ。本番の状態（mode=prod）で 1 回動かす。
function kick() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('スクリプト プロパティ GITHUB_TOKEN がありません');
  const res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({ref: 'main', inputs: {mode: 'prod'}}),
      muteHttpExceptions: true,
    });
  const code = res.getResponseCode();
  // 応答の本文は出さない（トークンの情報が混じらないように）
  if (code !== 204 && code !== 200) {
    throw new Error('GitHub が ' + code + ' を返しました（401: トークンの期限切れ・取り消し、403/404: 権限・リポジトリの指定、422: ワークフローが無効）');
  }
}

// 最初に一度だけ手で実行する: 15 分ごとのトリガーを作り直し、その場で 1 回起こす。
function setup() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'kick'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('kick').timeBased().everyMinutes(15).create();
  kick();
}
