# お名前.com メンテナンス/障害RSS → Slack通知（英語翻訳付き）

お名前.comの「メンテナンス」「障害」RSSフィードを15分おきにチェックし、
新着記事があれば **本文を英語に翻訳したうえで全文を** Slackに自動通知するGitHub Actionsワークフローです。
翻訳には Claude API (Anthropic) を使用します。

## 構成

```
.github/workflows/onamae-rss.yml   # 定期実行するワークフロー定義
scripts/notify_slack.py            # RSS取得 & Slack通知スクリプト
state/seen_ids.json                # 通知済み記事IDの記録(自動生成・自動更新)
requirements.txt                   # Python依存パッケージ
```

## セットアップ手順

### 1. Slack Incoming Webhookを作成
1. https://api.slack.com/apps にアクセスし「Create New App」→「From scratch」
2. 通知したいワークスペースを選択
3. 左メニュー「Incoming Webhooks」を有効化
4. 「Add New Webhook to Workspace」で通知先チャンネルを選び、Webhook URLを発行
   （`https://hooks.slack.com/services/XXXX/XXXX/XXXX` のような形式）

### 2. このリポジトリをGitHubにpush
このフォルダの内容をそのまま新規GitHubリポジトリにpushしてください。
（Publicでも Privateでも動作しますが、Privateの場合はActionsの無料枠に上限があるのでご注意ください）

```bash
cd onamae-rss-slack
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin <あなたのリポジトリURL>
git push -u origin main
```

### 3. GitHub Secretsを登録
リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」で
**2つ** のシークレットを登録します。

| Name | Secret |
|------|--------|
| `SLACK_WEBHOOK_URL` | 手順1で発行したWebhook URL |
| `ANTHROPIC_API_KEY` | Claude APIのキー（https://console.anthropic.com で発行） |

`ANTHROPIC_API_KEY` は本文を英語に翻訳するために使います。未設定でも通知自体は動きますが、
その場合は翻訳に失敗し「原文（日本語）のまま」通知されます。

### 4. 動作確認
- 「Actions」タブ →「Onamae.com RSS to Slack」→「Run workflow」で手動実行できます
- 初回実行時は既存記事を「既読」として記録するだけで通知はスキップされます
  （動かした瞬間に過去記事が全部Slackに流れるのを防ぐため）
- 2回目以降の実行から、新着記事のみSlackに通知されます

## カスタマイズ

- **実行頻度を変える**: `.github/workflows/onamae-rss.yml` の `cron` を変更
  （例: 5分おき `*/5 * * * *`、1時間おき `0 * * * *`）
- **通知メッセージの見た目を変える**: `scripts/notify_slack.py` の `build_message()` を編集
  （Slackのblock kit形式にしてリッチな見た目にすることも可能）
- **緊急メンテナンスだけ強調したい**: `URGENT_KEYWORDS` に判定したい単語を追加
- **翻訳のコストを抑えたい**: `scripts/notify_slack.py` の `TRANSLATE_MODEL` を
  `claude-haiku-4-5` に変更（安価・高速。翻訳品質は少し下がります）
- **翻訳をやめたい/別言語にしたい**: `translate_to_english()` の `system` プロンプトを編集

## 注意点

- GitHub Actionsのscheduleは負荷状況により数分遅延することがあります（Slack側への通知が15分より少し遅れる場合があります）
- `state/seen_ids.json` はワークフロー実行時に自動でコミットされます。手動で編集する必要はありません
