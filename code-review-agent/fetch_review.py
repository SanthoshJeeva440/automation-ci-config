#!/usr/bin/env python3
"""Fetch the AI review and its inline comments for a PR; render an email attachment."""
from __future__ import annotations

import argparse
import html
import os
from pathlib import Path

import requests

MARKER = "<!-- claude-code-review sha="


def get_all(session: requests.Session, url: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        response = session.get(url, params={"per_page": 100, "page": page}, timeout=30)
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected GitHub API response at {url}")
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return items
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--workflow-result", default="unknown")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")

    base = "https://api.github.com"
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "santhosh-ai-code-review-email",
    })

    reviews_url = f"{base}/repos/{args.repository}/pulls/{args.pr}/reviews"
    reviews = get_all(session, reviews_url)
    ai_reviews = [review for review in reviews if MARKER in str(review.get("body", ""))]
    latest = max(ai_reviews, key=lambda item: item.get("submitted_at") or "", default=None)

    inline_comments: list[dict] = []
    if latest and latest.get("id"):
        comments_url = f"{base}/repos/{args.repository}/pulls/{args.pr}/comments"
        all_comments = get_all(session, comments_url)
        inline_comments = [
            item for item in all_comments
            if item.get("pull_request_review_id") == latest.get("id")
        ]

    if latest:
        review_body = str(latest.get("body") or "No review summary was returned.")
        review_state = str(latest.get("state") or "COMMENTED")
        review_date = str(latest.get("submitted_at") or "")
        reviewer = str((latest.get("user") or {}).get("login", "unknown"))
        commit_sha = str(latest.get("commit_id") or "")
    else:
        review_body = "No AI review was found for this pull request. Check the GitHub Actions AI Code Review job."
        review_state = "NOT FOUND"
        review_date = ""
        reviewer = ""
        commit_sha = ""

    comment_items = []
    for item in inline_comments:
        path = html.escape(str(item.get("path", "")))
        line = html.escape(str(item.get("line") or item.get("original_line") or ""))
        body = html.escape(str(item.get("body", ""))).replace("\n", "<br>")
        comment_items.append(f"<li><b>{path}:{line}</b><p>{body}</p></li>")

    review_html = html.escape(review_body).replace("\n", "<br>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AI review — {html.escape(args.repository)} PR #{args.pr}</title>
<style>body{{font-family:Arial,sans-serif;color:#202638;line-height:1.5;margin:28px}}
main{{max-width:900px;margin:auto}}.meta{{color:#64748b}}.summary{{background:#f4f6fa;padding:18px;border-radius:8px}}
li{{margin:15px 0}}.finding{{border-bottom:1px solid #e5e7eb;padding-bottom:10px}}</style></head>
<body><main><h1>AI Code Review Result</h1>
<p class="meta">{html.escape(args.repository)} PR #{args.pr} · Jenkins workflow result: {html.escape(args.workflow_result)}</p>
<p><b>Review state:</b> {html.escape(review_state)}<br><b>Reviewer:</b> {html.escape(reviewer)}<br>
<b>Review date:</b> {html.escape(review_date)}<br><b>Reviewed commit:</b> {html.escape(commit_sha)}</p>
<h2>Review summary</h2><div class="summary">{review_html}</div>
<h2>Inline findings ({len(inline_comments)})</h2><ul>{''.join(comment_items) or '<li>No inline findings were attached to this AI review.</li>'}</ul>
</main></body></html>"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"Saved AI review email attachment: {args.output}")
    print(f"Review: {review_state}; inline findings: {len(inline_comments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
