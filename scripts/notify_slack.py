#!/usr/bin/env python3
"""
お名前.com の「メンテナンス」「障害」RSSフィードを取得し、
未通知の新着記事があれば、本文を英語に翻訳したうえで全文をSlackに通知するスクリプト。

翻訳は無料の Google 翻訳エンドポイント（deep-translator 経由）を使用する。
APIキーは不要・費用もかからない。
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

import feedparser
import requests
from deep_translator import GoogleTranslator

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

# Google翻訳の無料エンドポイントは1回あたり約5000文字が上限。安全側に分割する。
TRANSLATE_CHUNK_LIMIT = 4500

# Slackのtextフィールド上限（40,000字）に対する安全マージン
SLACK_TEXT_LIMIT = 3800

# Slack通知のリトライ設定
SLACK_TIMEOUT = 15          # 1回あたりのタイムアウト（秒）
SLACK_MAX_ATTEMPTS = 4      # 最大試行回数（1回目 + リトライ3回）

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_FILE = STATE_DIR / "seen_ids.json"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")


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


def _chunk_text(text: str, limit: int = TRANSLATE_CHUNK_LIMIT) -> list:
    """翻訳エンドポイントの文字数上限に収まるよう、なるべく行単位で分割する。"""
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        # 1行が上限を超える場合はハード分割
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def translate_to_english(title: str, body: str) -> dict:
    """日本語のタイトル・本文を英語に翻訳して {"title", "body"} を返す。
    失敗時は例外を送出する。"""
    translator = GoogleTranslator(source="ja", target="en")

    en_title = title
    if title.strip():
        en_title = translator.translate(title) or title

    en_body = ""
    if body.strip():
        parts = [translator.translate(c) or "" for c in _chunk_text(body)]
        en_body = "".join(parts)

    return {"title": en_title.strip(), "body": en_body.strip()}


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
                translated = translate_to_english(title, body)
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
