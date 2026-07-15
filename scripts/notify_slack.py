#!/usr/bin/env python3
"""
お名前.com の「メンテナンス」「障害」RSSフィードを取得し、
未通知の新着記事があれば、本文を英語に翻訳したうえで全文をSlackに通知するスクリプト。

翻訳には Claude API (Anthropic) を使用する（環境変数 ANTHROPIC_API_KEY が必要）。
既通知の記事IDは state/seen_ids.json に保存し、重複通知を防ぐ。

信頼性のための工夫:
- Slack通知はタイムアウト付きで、失敗時は指数バックオフで最大数回リトライする。
- 通知に成功した記事は「1件ごとに」既読保存する。途中でクラッシュしても、
  成功済みの記事が次回に重複通知されない。
- RSS取得/解析に失敗したフィードはスキップしつつ、その旨をSlackにも警告通知する。
- 翻訳に失敗した場合は原文（日本語）のまま通知し、取りこぼしを防ぐ。
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import feedparser
import requests

FEEDS = {
    "メンテナンス": "https://www.onamae.com/news/rss.xml?c=maintenance&g=domain",
    "障害": "https://www.onamae.com/news/rss.xml?c=incident&g=domain",
}

# カテゴリ名の英語表記（Slack通知に使用）
CATEGORY_EN = {
    "メンテナンス": "Maintenance",
    "障害": "Incident",
}

# 緊急度が高そうなキーワードには絵文字を付ける（原文タイトルで判定）
URGENT_KEYWORDS = ["緊急"]

# 翻訳に使う Claude モデル。コストを抑えたい場合は "claude-haiku-4-5" に変更可。
TRANSLATE_MODEL = "claude-opus-4-8"
TRANSLATE_MAX_TOKENS = 4000
MAX_BODY_CHARS = 6000  # 翻訳にかける本文の最大文字数（トークン浪費を防ぐ）

# Slackのtextフィールド上限（40,000字）に対する安全マージン
SLACK_TEXT_LIMIT = 3800

# Slack通知のリトライ設定
SLACK_TIMEOUT = 15          # 1回あたりのタイムアウト（秒）
SLACK_MAX_ATTEMPTS = 4      # 最大試行回数（1回目 + リトライ3回）

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_FILE = STATE_DIR / "seen_ids.json"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# 翻訳結果の構造化出力スキーマ
TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}


def load_seen() -> set:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def extract_body(entry) -> str:
    """RSSエントリから本文テキストを取り出し、HTMLタグを除去して返す。"""
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "") or ""
    if not raw:
        raw = entry.get("summary", "") or entry.get("description", "") or ""
    # HTMLタグを除去し、エンティティをデコード
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    # 余分な空白を整理
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def translate_to_english(category: str, title: str, body: str) -> dict:
    """日本語のタイトル・本文を英語に翻訳して {"title", "body"} を返す。
    失敗時は RuntimeError を送出する。"""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む
    body_for_translation = body[:MAX_BODY_CHARS]
    source = (
        f"Category: {category}\n"
        f"Title: {title}\n\n"
        f"Body:\n{body_for_translation if body_for_translation else '(no body text)'}"
    )
    system = (
        "You are a professional translator for a web-hosting/domain-registrar company. "
        "Translate the given Japanese maintenance/incident announcement into natural, "
        "professional English. Preserve all dates, times, time zones, domain names, "
        "hostnames, URLs, IP addresses, and technical terms exactly as written. "
        "Do not add commentary. Return the translated title and the translated body."
    )
    resp = client.messages.create(
        model=TRANSLATE_MODEL,
        max_tokens=TRANSLATE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": source}],
        output_config={"format": {"type": "json_schema", "schema": TRANSLATION_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("翻訳レスポンスにテキストが含まれていません")
    data = json.loads(text)
    return {"title": data["title"].strip(), "body": data["body"].strip()}


def build_message(category: str, original_title: str, en_title: str, en_body: str, link: str) -> dict:
    """英語のタイトル・本文・リンクからSlackペイロードを組み立てる。"""
    is_urgent = any(kw in original_title for kw in URGENT_KEYWORDS)
    prefix = "🚨" if is_urgent else "🔧" if category == "メンテナンス" else "⚠️"
    category_en = CATEGORY_EN.get(category, category)

    body = en_body.strip()
    if len(body) > SLACK_TEXT_LIMIT:
        body = body[:SLACK_TEXT_LIMIT].rstrip() + "\n…(truncated)"

    parts = [f"{prefix} *[{category_en}]* {en_title}"]
    if body:
        parts.append(body)
    parts.append(link)
    return {"text": "\n\n".join(parts)}


def notify_slack(payload: dict) -> None:
    """Slackへ通知する。失敗時は指数バックオフでリトライし、最終的に失敗したら例外を送出する。"""
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("環境変数 SLACK_WEBHOOK_URL が設定されていません")

    last_err = None
    for attempt in range(1, SLACK_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=SLACK_TIMEOUT)
            resp.raise_for_status()
            return
        except requests.RequestException as e:
            last_err = e
            print(f"[WARN] Slack通知に失敗 ({attempt}/{SLACK_MAX_ATTEMPTS}): {e}", file=sys.stderr)
            if attempt < SLACK_MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))  # 1秒 → 2秒 → 4秒

    raise RuntimeError(f"Slack通知に{SLACK_MAX_ATTEMPTS}回失敗しました: {last_err}")


def fetch_feed(category: str, url: str):
    """RSSを取得・解析する。取得失敗なら (None, エラーメッセージ) を返す。"""
    feed = feedparser.parse(url)
    status = getattr(feed, "status", None)
    if (status is not None and status >= 400) or (not feed.entries and feed.bozo):
        reason = feed.get("bozo_exception") or f"HTTP status={status}"
        return None, f"[{category}] RSS取得に失敗: {url}（{reason}）"
    return feed, None


def main() -> int:
    if not SLACK_WEBHOOK_URL:
        print("環境変数 SLACK_WEBHOOK_URL が設定されていません", file=sys.stderr)
        return 1

    seen = load_seen()
    new_seen = set(seen)
    notified_count = 0
    first_run = len(seen) == 0
    errors = []

    for category, url in FEEDS.items():
        feed, err = fetch_feed(category, url)
        if err:
            print(f"[WARN] {err}", file=sys.stderr)
            errors.append(err)
            continue

        for entry in feed.entries:
            article_id = entry.get("link") or entry.get("id")
            if not article_id or article_id in new_seen:
                continue

            # 初回実行時は既存記事を全部通知すると大量に流れるので、
            # 初回はスキップして「既知」として記録するだけにする（翻訳もしない）
            if first_run:
                new_seen.add(article_id)
                continue

            title = entry.get("title", "(タイトルなし)")
            body = extract_body(entry)

            # 本文を英語に翻訳。失敗したら原文のまま通知して取りこぼしを防ぐ。
            try:
                translated = translate_to_english(category, title, body)
                en_title, en_body = translated["title"], translated["body"]
            except Exception as e:
                print(f"[WARN] 翻訳に失敗したため原文で通知します: [{category}] {title}: {e}", file=sys.stderr)
                en_title = f"{title}  (translation failed — original Japanese)"
                en_body = body

            payload = build_message(category, title, en_title, en_body, article_id)

            try:
                notify_slack(payload)
            except Exception as e:
                # 通知に失敗した記事は既読にしない（次回リトライされる）。
                # ここまで成功した分は下で保存済みなので重複通知されない。
                print(f"[ERROR] 通知に失敗したため中断します: [{category}] {title}: {e}", file=sys.stderr)
                save_seen(new_seen)
                raise

            # 通知成功 → 1件ごとに既読を確定（途中クラッシュでも重複させない）
            new_seen.add(article_id)
            save_seen(new_seen)
            notified_count += 1
            print(f"通知しました: [{category}] {en_title}")

    # 初回や新着ゼロでも既読状態を書き出しておく
    save_seen(new_seen)

    # RSS取得エラーがあればSlackにも警告して気づけるようにする
    if errors:
        warn_text = "🛑 *[RSS monitor] Failed to fetch feeds*\n" + "\n".join(
            f"• {e}" for e in errors
        )
        try:
            notify_slack({"text": warn_text})
        except Exception as e:
            print(f"[ERROR] 警告通知の送信にも失敗しました: {e}", file=sys.stderr)

    if first_run:
        print(f"初回実行のため通知はスキップし、{len(new_seen)}件を既知として記録しました。")
    else:
        print(f"完了: {notified_count}件の新着を通知しました。")

    # 取得エラーがあった場合はジョブを失敗（赤✗）にして通知＆メールで気づけるようにする
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
