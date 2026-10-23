"""売上管理の Google Cloud の準備（gcloud を順に実行する。何度実行しても同じ結果になる）。

    uv run python sales/infra/setup.py STEP [STEP ...] [--dry-run]

ふだんのデプロイは GitHub の main への push（Cloud Build のトリガー sales-deploy が sales/cloudbuild.yaml を動かす）。
このスクリプトは、最初の準備・権限・秘密情報の更新・GitHub を使えないときの手動デプロイに使う。

STEP:
  apis          使う API を有効にする
  registry      Artifact Registry（コンテナ置き場。最新2つだけ残す）
  secrets       秘密情報を、この PC の資格情報マネージャーからサービスごとのシークレットに登録する（古い版は破棄）
  secrets-compat  移行用: secrets と同じだが、sales-config にすべての鍵を残す
  base          Firestore、サービスアカウント、Cloud Tasks のキュー
  iam           サービスアカウントの権限（必要なものだけ）。以前の広すぎる権限は外す（iam-grant と iam-revoke に分けても動かせる）
  square        Square の OAuth トークンの置き場所（square-oauth）
  square-open   Square の許可をやり直す間だけ、webhook がトークンを保存できるようにする（終わったら square-close）
  deployer      Cloud Build がデプロイに使うサービスアカウント（sales-deployer）
  scheduler     定期ジョブ（30分ごと・毎日・毎週）
  trigger       GitHub の main への push でデプロイするトリガー（先に GitHub との接続が必要。運用手順書）
  deploy        手動デプロイ（Cloud Build で cloudbuild.yaml を動かす）
  guard         billing-guard（課金停止）を作る・デプロイし直す
  guard-budget  予算の通知を Pub/Sub につなぐ（組織のドメイン制限を一時的に外す必要がある）
  guard-test    billing-guard に試験用の通知を送る（dry モードのときだけ）
  guard-live    billing-guard を本番モード（実際に請求先アカウントを外す）に切り替える
  migrate       2026-10 の整理の移行（MIGRATE の順に実行）

--dry-run を付けると、実行せずにコマンドを表示するだけ。秘密の値は表示しない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from barnull_sales.config import KEYRING_SERVICE, SECRETS_BY_SERVICE, load_settings  # noqa: E402

CFG = load_settings(ROOT / "sales.toml")
PROJECT = CFG.project
REGION = CFG.region
BILLING_ACCOUNT = "019A83-7F08B4-6F9AEC"
BUDGET_NAME = "barnull-sales-guard"
REPO = "sales"
QUEUE = CFG.tasks_queue
GITHUB_CONNECTION = "github"          # Cloud Build の GitHub 接続名（コンソールで作る）
GITHUB_REPO = "ryoinnn-bar-null-ops"   # 接続にリンクしたリポジトリの名前（コンソールが「所有者-リポジトリ」で付ける）

GCLOUD = os.environ.get("GCLOUD", str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK"
                                      / "google-cloud-sdk" / "bin" / "gcloud.cmd"))
APIS = [
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "pubsub.googleapis.com",
    "cloudbilling.googleapis.com", "billingbudgets.googleapis.com", "cloudresourcemanager.googleapis.com",
    "secretmanager.googleapis.com", "iam.googleapis.com", "firestore.googleapis.com", "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
]


def sa(name: str) -> str:
    return f"{name}@{PROJECT}.iam.gserviceaccount.com"


WEBHOOK, WORKER, INVOKER, GUARD, DEPLOYER = (sa(n) for n in ("sales-webhook", "sales-worker", "sales-invoker",
                                                             "billing-guard", "sales-deployer"))
DRY = "--dry-run" in sys.argv


def gcloud(*args: str, stdin: str | None = None, capture: bool = False, check: bool = True) -> str:
    """gcloud を実行する。capture しないときは出力をそのまま画面に流す（再認証のパスワード入力にその場で答えられるように）。"""
    cmd = [GCLOUD, *args, f"--project={PROJECT}"]
    print(("[dry-run] " if DRY else "$ ") + "gcloud " + " ".join(args) + ("  < (秘密の値)" if stdin is not None else ""))
    if DRY:
        return ""
    retry_iam = "add-iam-policy-binding" in args   # 作った直後のサービスアカウントは反映に時間がかかる
    for attempt in range(6 if retry_iam else 1):
        sys.stdout.flush()
        if capture or stdin is not None:
            r = subprocess.run(cmd, input=stdin, text=True, capture_output=True, encoding="utf-8")
        else:
            r = subprocess.run(cmd, text=True, encoding="utf-8")
        if r.returncode == 0 or not retry_iam or attempt == 5:
            break
        print("  （15秒後に再試行）")
        time.sleep(15)
    if check and r.returncode != 0:
        if capture and r.stderr:
            print(r.stderr.strip()[-1500:])
        sys.exit(f"失敗しました（終了コード {r.returncode}）")
    return (r.stdout or "").strip() if capture else ""


def exists(*args: str) -> bool:
    if DRY:
        return False
    return subprocess.run([GCLOUD, *args, f"--project={PROJECT}"], capture_output=True, text=True,
                          encoding="utf-8").returncode == 0


def project_number() -> str:
    return gcloud("projects", "describe", PROJECT, "--format=value(projectNumber)", capture=True) or "PROJECT_NUMBER"


def ensure_sa(email: str) -> None:
    name = email.split("@")[0]
    if not exists("iam", "service-accounts", "describe", email):
        gcloud("iam", "service-accounts", "create", name, f"--display-name={name}")


def bind(kind: str, target: str, member: str, role: str, *extra: str) -> None:
    gcloud(*kind.split(), "add-iam-policy-binding", target, f"--member=serviceAccount:{member}", f"--role={role}",
           *extra, "--format=none")


def unbind(kind: str, target: str, member: str, role: str, *extra: str) -> None:
    """権限を外す（付いていなければ何もしない）。"""
    gcloud(*kind.split(), "remove-iam-policy-binding", target, f"--member=serviceAccount:{member}", f"--role={role}",
           *extra, "--format=none", check=False, capture=True)


# ---------------------------------------------------------------- 基本
def step_apis() -> None:
    gcloud("services", "enable", *APIS)


def step_registry() -> None:
    if not exists("artifacts", "repositories", "describe", REPO, f"--location={REGION}"):
        gcloud("artifacts", "repositories", "create", REPO, "--repository-format=docker", f"--location={REGION}",
               "--description=Bar null sales")
    policy = [
        {"name": "delete-all", "action": {"type": "Delete"}, "condition": {"tagState": "any"}},
        {"name": "keep-latest-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(policy, f)
    gcloud("artifacts", "repositories", "set-cleanup-policies", REPO, f"--location={REGION}", f"--policy={f.name}",
           "--no-dry-run")


def _keyring_values(names: tuple[str, ...]) -> dict:
    import keyring

    out = {}
    for n in names:
        v = (keyring.get_password(KEYRING_SERVICE, n) or "").strip()
        if not v and n == "ANTHROPIC_API_KEY":   # 売上管理用のキーが無ければ予約対応のキー（2026-10-10 本人の選択）
            v = (keyring.get_password("bar-null-ops", n) or "").strip()
        if v:
            out[n] = v
    missing = [n for n in names if n not in out]
    if missing:
        sys.exit(f"資格情報マネージャーに無い秘密情報があります: {', '.join(missing)}（scripts/set_secret.py で登録）")
    return out


def step_secrets(compat: bool = False) -> None:
    """サービスごとに必要な秘密情報だけを入れる（公開している sales-webhook から他の鍵を読めないように）。

    compat: 移行中だけ、sales-config にすべての鍵を残す（新しい版をデプロイするまで、今の webhook が読むため）。
    """
    groups = dict(SECRETS_BY_SERVICE)
    if compat:
        groups["sales-config"] = tuple(sorted({n for names in groups.values() for n in names}))
    for secret, names in groups.items():
        print(f"  {secret}: {', '.join(names)}")
        if not exists("secrets", "describe", secret):
            gcloud("secrets", "create", secret, "--replication-policy=automatic")
        gcloud("secrets", "versions", "add", secret, "--data-file=-", stdin=json.dumps(_keyring_values(names)))
        if DRY:
            continue
        out = gcloud("secrets", "versions", "list", secret, "--filter=state:ENABLED", "--sort-by=~createTime",
                     "--format=value(name)", capture=True)
        for v in [x.rsplit("/", 1)[-1] for x in out.splitlines() if x.strip()][1:]:   # 最新以外を破棄（無料枠に収める）
            gcloud("secrets", "versions", "destroy", v, f"--secret={secret}", "--quiet", "--format=none")


def step_base() -> None:
    """Firestore、サービスアカウント、Cloud Tasks のキュー（worker は1件ずつ処理するので、同時に送るのも1件）。"""
    if not exists("firestore", "databases", "describe", "--database=(default)"):
        gcloud("firestore", "databases", "create", f"--location={REGION}", "--type=firestore-native")
    for email in (WEBHOOK, WORKER, INVOKER):
        ensure_sa(email)
    verb = "update" if exists("tasks", "queues", "describe", QUEUE, f"--location={REGION}") else "create"
    gcloud("tasks", "queues", verb, QUEUE, f"--location={REGION}", "--max-dispatches-per-second=1",
           "--max-concurrent-dispatches=1", f"--max-attempts={CFG.tasks_max_attempts}", "--min-backoff=10s",
           "--max-backoff=600s")


def step_iam_grant() -> None:
    """権限は「誰が・何に・何をできるか」を最小にする。"""
    # sales-webhook: LINE のイベントをキュー sales-events に積む（プロジェクト全体ではなくキューだけ）
    bind("tasks queues", QUEUE, WEBHOOK, "roles/cloudtasks.enqueuer", f"--location={REGION}")
    bind("iam service-accounts", INVOKER, WEBHOOK, "roles/iam.serviceAccountUser")   # タスクに worker 呼び出しの ID を付ける
    bind("secrets", "sales-webhook-config", WEBHOOK, "roles/secretmanager.secretAccessor")
    # sales-worker: Firestore と秘密情報
    bind("projects", PROJECT, WORKER, "roles/datastore.user", "--condition=None")
    bind("secrets", "sales-config", WORKER, "roles/secretmanager.secretAccessor")
    # sales-invoker: worker を呼ぶ（Cloud Tasks と Cloud Scheduler が使う ID）
    bind("run services", "sales-worker", INVOKER, "roles/run.invoker", f"--region={REGION}")
    # billing-guard: Slack のトークンだけ
    if exists("iam", "service-accounts", "describe", GUARD):
        bind("secrets", "billing-guard-config", GUARD, "roles/secretmanager.secretAccessor")


def step_iam_revoke() -> None:
    """以前に付けた広すぎる権限を外す（新しいシークレットを使う版をデプロイした後で）。"""
    unbind("projects", PROJECT, WEBHOOK, "roles/cloudtasks.enqueuer", "--condition=None")
    unbind("secrets", "sales-config", WEBHOOK, "roles/secretmanager.secretAccessor")
    unbind("secrets", "sales-config", GUARD, "roles/secretmanager.secretAccessor")


def step_iam() -> None:
    step_iam_grant()
    step_iam_revoke()


def step_square() -> None:
    """Square の OAuth トークンの置き場所。worker は読んで更新する。webhook は普段は触れない（square-open の間だけ書ける）。"""
    if not exists("secrets", "describe", "square-oauth"):
        gcloud("secrets", "create", "square-oauth", "--replication-policy=automatic")
    for role in ("roles/secretmanager.secretVersionManager", "roles/secretmanager.secretAccessor"):
        bind("secrets", "square-oauth", WORKER, role)
    step_square_close()


def step_square_open() -> None:
    """Square の許可をやり直す間だけ、webhook が新しいトークンを保存できるようにする（運用手順書）。"""
    bind("secrets", "square-oauth", WEBHOOK, "roles/secretmanager.secretVersionManager")


def step_square_close() -> None:
    for role in ("roles/secretmanager.secretVersionManager", "roles/secretmanager.secretAccessor"):
        unbind("secrets", "square-oauth", WEBHOOK, role)


def step_deployer() -> None:
    """Cloud Build がデプロイに使うサービスアカウント（イメージの保存、Cloud Run の更新、ログ）。"""
    ensure_sa(DEPLOYER)
    bind("projects", PROJECT, DEPLOYER, "roles/logging.logWriter", "--condition=None")
    # Cloud Run の管理は2つのサービスにだけ（billing-guard など他のサービスは触れない）
    for service in ("sales-worker", "sales-webhook"):
        bind("run services", service, DEPLOYER, "roles/run.admin", f"--region={REGION}")
    unbind("projects", PROJECT, DEPLOYER, "roles/run.admin", "--condition=None")
    bind("artifacts repositories", REPO, DEPLOYER, "roles/artifactregistry.writer", f"--location={REGION}")
    for target in (WORKER, WEBHOOK):   # デプロイしたサービスを、それぞれのサービスアカウントで動かすため
        bind("iam service-accounts", target, DEPLOYER, "roles/iam.serviceAccountUser")
    # 手動デプロイ（setup.py deploy）で送ったソースを読むため
    bucket = f"gs://{PROJECT}_{REGION}_cloudbuild"
    if exists("storage", "buckets", "describe", bucket):
        bind("storage buckets", bucket, DEPLOYER, "roles/storage.objectViewer")


def _service_url(name: str) -> str:
    return gcloud("run", "services", "describe", name, f"--region={REGION}", "--format=value(status.url)",
                  capture=True) or f"https://{name}-xxxx.run.app"


def step_scheduler() -> None:
    """定期ジョブ（Cloud Scheduler の無料枠3本）。worker は1件ずつ処理するので、時刻をずらし、混んでいたらやり直す。"""
    url = _service_url("sales-worker")
    jobs = {"sales-reconcile": ("*/30 * * * *", "/jobs/reconcile"),   # 30分ごと: 突合・入金の確認
            "sales-daily": ("5 10 * * *", "/jobs/daily"),             # 毎日 10:05: 入金未確認・未収リマインド
            "sales-weekly": ("10 9 * * 1", "/jobs/weekly")}           # 毎週月曜 9:10: 売上週報
    for name, (schedule, path) in jobs.items():
        verb = "update" if exists("scheduler", "jobs", "describe", name, f"--location={REGION}") else "create"
        gcloud("scheduler", "jobs", verb, "http", name, f"--location={REGION}", f"--schedule={schedule}",
               "--time-zone=Asia/Tokyo", f"--uri={url}{path}", "--http-method=POST",
               f"--oidc-service-account-email={INVOKER}", f"--oidc-token-audience={url}", "--attempt-deadline=600s",
               "--max-retry-attempts=3", "--min-backoff=60s", "--max-backoff=300s")


def step_trigger() -> None:
    repo = f"projects/{PROJECT}/locations/{REGION}/connections/{GITHUB_CONNECTION}/repositories/{GITHUB_REPO}"
    if exists("builds", "triggers", "describe", "sales-deploy", f"--region={REGION}"):
        print("  トリガー sales-deploy は作成済み")
        return
    gcloud("builds", "triggers", "create", "github", "--name=sales-deploy", f"--region={REGION}",
           f"--repository={repo}", "--branch-pattern=^main$", "--build-config=sales/cloudbuild.yaml",
           "--included-files=sales/**", f"--service-account=projects/{PROJECT}/serviceAccounts/{DEPLOYER}")


def step_deploy() -> None:
    """GitHub を使わずにデプロイする（sales/ を送って cloudbuild.yaml を動かす）。"""
    tag = "manual-" + time.strftime("%Y%m%d-%H%M%S")
    gcloud("builds", "submit", str(ROOT), f"--config={ROOT / 'cloudbuild.yaml'}", f"--region={REGION}",
           f"--substitutions=_DIR=.,_TAG={tag}", f"--service-account=projects/{PROJECT}/serviceAccounts/{DEPLOYER}",
           "--default-buckets-behavior=regional-user-owned-bucket")


# ---------------------------------------------------------------- 課金停止（billing-guard）
def _guard_mode() -> str:
    if DRY or not exists("run", "services", "describe", "billing-guard", f"--region={REGION}"):
        return "dry"
    out = gcloud("run", "services", "describe", "billing-guard", f"--region={REGION}", "--format=json", capture=True)
    env = json.loads(out)["spec"]["template"]["spec"]["containers"][0].get("env", [])
    return next((e.get("value") for e in env if e.get("name") == "GUARD_MODE"), "dry")


def step_guard() -> None:
    """billing-guard を作る・デプロイし直す（モードは変えない。新規なら dry）。"""
    ensure_sa(GUARD)
    # 請求先アカウントを外すのに必要な最小の権限（プロジェクトの請求先管理者）
    bind("projects", PROJECT, GUARD, "roles/billing.projectManager", "--condition=None")
    bind("secrets", "billing-guard-config", GUARD, "roles/secretmanager.secretAccessor")
    if not exists("pubsub", "topics", "describe", "billing-guard"):
        gcloud("pubsub", "topics", "create", "billing-guard")
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPO}/billing-guard:{time.strftime('%Y%m%d-%H%M%S')}"
    gcloud("builds", "submit", str(ROOT / "billing_guard"), f"--tag={image}", f"--region={REGION}",
           "--default-buckets-behavior=regional-user-owned-bucket")
    mode = _guard_mode()
    gcloud("run", "deploy", "billing-guard", f"--image={image}", f"--region={REGION}", f"--service-account={GUARD}",
           "--no-allow-unauthenticated", "--max-instances=1", "--memory=256Mi",
           f"--set-env-vars=GCP_PROJECT={PROJECT},GUARD_MODE={mode},THRESHOLD_JPY=1,"
           f"SLACK_CHANNEL={CFG.slack_channel_prod if mode == 'live' else CFG.slack_channel_test},"
           f"SLACK_ALERT_MENTION={CFG.slack_alert_mention}",
           "--set-secrets=SALES_CONFIG=billing-guard-config:latest")
    bind("run services", "billing-guard", GUARD, "roles/run.invoker", f"--region={REGION}")
    url = _service_url("billing-guard")
    if not exists("pubsub", "subscriptions", "describe", "billing-guard-push"):
        gcloud("pubsub", "subscriptions", "create", "billing-guard-push", "--topic=billing-guard",
               f"--push-endpoint={url}/pubsub", f"--push-auth-service-account={GUARD}", "--ack-deadline=60")


def _budget_id() -> str:
    out = gcloud("billing", "budgets", "list", f"--billing-account={BILLING_ACCOUNT}", "--format=json", capture=True)
    for b in json.loads(out or "[]"):
        if b.get("displayName") == BUDGET_NAME:
            return b["name"].rsplit("/", 1)[-1]
    if DRY:
        return "BUDGET_ID"
    sys.exit(f"予算「{BUDGET_NAME}」が見つかりません")


def step_guard_budget() -> None:
    """予算の通知を Pub/Sub トピックにつなぐ（Google の予算通知用アカウントに発行の権限を付ける）。"""
    gcloud("pubsub", "topics", "add-iam-policy-binding", "billing-guard",
           "--member=serviceAccount:billing-budget-alert@system.gserviceaccount.com",
           "--role=roles/pubsub.publisher", "--format=none")
    gcloud("billing", "budgets", "update", _budget_id(), f"--billing-account={BILLING_ACCOUNT}",
           f"--notifications-rule-pubsub-topic=projects/{PROJECT}/topics/billing-guard")


def step_guard_test() -> None:
    if _guard_mode() != "dry":
        sys.exit("billing-guard が dry モードではないので、試験用の通知は送りません（実際に止まってしまうため）")
    for cost in (0, 5):
        msg = {"budgetDisplayName": BUDGET_NAME, "costAmount": cost, "budgetAmount": 100, "currencyCode": "JPY",
               "costIntervalStart": "2026-10-01T07:00:00Z", "alertThresholdExceeded": 0.01}
        gcloud("pubsub", "topics", "publish", "billing-guard", f"--message={json.dumps(msg)}")
    print("Slack のテスト用チャンネルに「課金停止テスト（dry）」が1件だけ出れば正常（費用 0 円の通知では何もしない）")


def step_guard_live() -> None:
    gcloud("run", "services", "update", "billing-guard", f"--region={REGION}",
           f"--update-env-vars=GUARD_MODE=live,SLACK_CHANNEL={CFG.slack_channel_prod}")


STEPS = {"apis": step_apis, "registry": step_registry, "secrets": step_secrets,
         "secrets-compat": lambda: step_secrets(compat=True), "base": step_base, "iam": step_iam,
         "iam-grant": step_iam_grant, "iam-revoke": step_iam_revoke,
         "square": step_square, "square-open": step_square_open, "square-close": step_square_close, "deployer": step_deployer, "scheduler": step_scheduler, "trigger": step_trigger,
         "deploy": step_deploy, "guard": step_guard, "guard-budget": step_guard_budget, "guard-test": step_guard_test,
         "guard-live": step_guard_live}
# 移行の順番（途中で止まっても今のサービスが動き続けるように）:
#   新しいシークレットを作る（sales-config には今の webhook が読む鍵も残す）→ 読む権限を付ける → 新しい版をデプロイ
#   → 古い権限を外す → sales-config を worker の鍵だけにする
MIGRATE = ("secrets-compat", "base", "iam-grant", "guard", "deployer", "deploy", "iam-revoke", "secrets", "square",
           "scheduler")


def main() -> None:
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    if names == ["migrate"]:
        names = list(MIGRATE)
    if not names or any(n not in STEPS for n in names):
        sys.exit(__doc__)
    for n in names:
        print(f"\n== {n}")
        STEPS[n]()
    print("\n完了しました")


if __name__ == "__main__":
    main()
