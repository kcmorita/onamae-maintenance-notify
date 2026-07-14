#!/usr/bin/env python3
"""
お名前.com の「メンテナンス」「障害」RSSフィードを取得し、
未通知の新着記事があればSlackにWebhook経由で通知するスクリプト。

既通知の記事IDは state/seen_ids.json に保存し、重複通知を防ぐ。
"""

import json
import os
import sys
from pathlib import Path

import feedparser
import requests

FEEDS = {
    "メンテナンス": "https://www.onamae.com/news/rss.xml?c=maintenance&g=domain",
    "障害": "https://www.onamae.com/news/rss.xml?c=incident&g=domain",
}

# 緊急度が高そうなキーワードには絵文字を付ける（お好みで調整可）
URGENT_KEYWORDS = ["緊急"]

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


def build_message(category: str, title: str, link: str) -> dict:
    is_urgent = any(kw in title for kw in URGENT_KEYWORDS)
    prefix = "🚨" if is_urgent else "🔧" if category == "メンテナンス" else "⚠️"
    text = f"{prefix} *[{category}]* {title}\n{link}"
    return {"text": text}


def notify_slack(payload: dict) -> None:
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("環境変数 SLACK_WEBHOOK_URL が設定されていません")
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


def main() -> int:
    seen = load_seen()
    new_seen = set(seen)
    notified_count = 0
    first_run = len(seen) == 0

    for category, url in FEEDS.items():
        feed = feedparser.parse(url)
        if feed.bozo:
            print(f"[WARN] フィード解析でエラー: {category} ({url}): {feed.bozo_exception}", file=sys.stderr)

        for entry in feed.entries:
            article_id = entry.get("link") or entry.get("id")
            if not article_id:
                continue

            if article_id in seen:
                continue

            new_seen.add(article_id)

            # 初回実行時は既存記事を全部通知すると大量に流れるので、
            # 初回はスキップして「既知」として記録するだけにする
            if first_run:
                continue

            title = entry.get("title", "(タイトルなし)")
            payload = build_message(category, title, article_id)
            notify_slack(payload)
            notified_count += 1
            print(f"通知しました: [{category}] {title}")

    save_seen(new_seen)

    if first_run:
        print(f"初回実行のため通知はスキップし、{len(new_seen)}件を既知として記録しました。")
    else:
        print(f"完了: {notified_count}件の新着を通知しました。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
