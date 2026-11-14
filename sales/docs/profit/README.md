# 利益管理（売上管理の拡張）

売上管理（`sales/`）を「利益ベース」で見られるようにするための、3つの会話（セッション）の分担と引き継ぎ資料。
作成: 2026-11-14

## 目的
売上レポート（Artifact）に「利益」セクションを足し、次の式で利益を見られるようにする。

```
利益 ＝ 売上 − 変動費（仕入・人件費・決済手数料） − 固定費
```

## 3つのセッションの分担

| セッション | 担当 | 成果物 | 開始用の文面 |
|---|---|---|---|
| A. 売上レポート（このセッションの続き） | 利益セクションの画面、固定費の保持、利益の計算、AI の示唆への反映 | `sales/report/weekly_report.html` | （続けて行う） |
| B. 人件費管理 | LINE で Bot にメンションして勤怠を打つ → Asana に記録 | 勤怠の受け口（worker に追加）、Asana の勤怠プロジェクト | [kickoff_labor.md](kickoff_labor.md) |
| C. 仕入管理 | 酒屋の Web アプリを Claude が操作し、毎週、仕入を取りに行く → Asana に記録 | 週次の取得手順、Asana の仕入プロジェクト | [kickoff_purchasing.md](kickoff_purchasing.md) |

B と C は、それぞれ新しい会話を開き、`kickoff_*.md` の「貼り付ける文面」をそのまま最初のメッセージにする。
作業フォルダは `C:\Users\ryoui\Documents\bar-null-ops`（リポジトリのルート）にする。

## 読む順番（B・C の最初の仕事）
1. [handoff.md](handoff.md) — 現行の売上管理とレポートの仕様、守るルール、運用の作法
2. [contract.md](contract.md) — A・B・C をつなぐデータの約束（Asana の項目、利益の定義、未決事項）
3. `sales/docs/design.md`、`sales/docs/decisions.md`、`sales/docs/weekly_report.md`（必要に応じて）

## つなぎ方の流れ
1. B・C は、データを Asana の専用プロジェクトに記録する（まずテスト用プロジェクトで動かす）。
2. 項目や ID が確定したら、B・C は [contract.md](contract.md) の自分の節を最終版に書き換え、本人に知らせる。
3. A が、確定した項目をレポートに読み込み、利益セクションに反映する。
4. 約束（contract.md）を変えるときは、必ず contract.md を先に直し、A・B・C のどれにも分かるようにする。

## 進め方の注意
- A・B・C は同じリポジトリ（`bar-null-ops`）の main で作業する。自分の担当の外のファイルは、必要な最小限だけ触る。
  触ったら、何をなぜ触ったかをコミットメッセージと contract.md に残す。
- `sales/report/weekly_report.html` は A だけが編集する（B・C は直さない）。B・C がレポートでの見え方を試したいときは、A に頼む。
- コミットしたら、本人に push を依頼する（この環境では Claude の `git push` が許可されていないことがある）。
