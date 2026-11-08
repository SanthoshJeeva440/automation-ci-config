#!/usr/bin/env python3
"""Create a monthly HTML/JSON report for PRs, default-branch commits and CI failures."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml

FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}
AI_REVIEW_MARKER = "<!-- claude-code-review sha="
SEVERITIES = ("critical", "high", "medium", "low", "nit")
REPORT_TZ = ZoneInfo("Asia/Kolkata")


class GitHub:
    def __init__(self, token: str, api_url: str, per_page: int = 100) -> None:
        self.base = api_url.rstrip("/")
        self.per_page = min(max(int(per_page), 1), 100)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "santhosh-monthly-code-review-report",
        })

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(f"{self.base}{path}", params=params, timeout=45)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Expected object from GitHub API {path}")
        return data

    def list_endpoint(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"per_page": self.per_page, "page": page})
            response = self.session.get(f"{self.base}{path}", params=query, timeout=45)
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"Expected list from GitHub API {path}")
            result.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < self.per_page:
                break
            page += 1
        return result

    def search_prs(self, repo: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        query = f"repo:{repo} is:pr created:{start_date}..{end_date}"
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.base}/search/issues",
                params={"q": query, "per_page": self.per_page, "page": page},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("items", [])
            result.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < self.per_page or page >= 10:
                break  # GitHub Search API caps a query at 1,000 results.
            page += 1
        return result


def month_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now = (now or datetime.now(REPORT_TZ)).astimezone(REPORT_TZ)
    first_this_month = datetime(now.year, now.month, 1, tzinfo=REPORT_TZ)
    last_previous_month = first_this_month - timedelta(days=1)
    first_previous_month = datetime(last_previous_month.year, last_previous_month.month, 1, tzinfo=REPORT_TZ)
    return first_previous_month, first_this_month, first_previous_month.strftime("%B %Y")


def collect_repo(client: GitHub, repo: str, start: datetime, end: datetime) -> dict[str, Any]:
    start_iso = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = (end - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    start_date = start.date().isoformat()
    end_date = (end - timedelta(days=1)).date().isoformat()

    prs = client.search_prs(repo, start_date, end_date)

    # Count AI findings from the review-summary marker produced by the local reviewer.
    # The count is limited to AI reviews submitted during this reporting month.
    ai_review_count = 0
    ai_finding_counts = {severity: 0 for severity in SEVERITIES}
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    for pr in prs:
        number = pr.get("number")
        if not number:
            continue
        try:
            reviews = client.list_endpoint(f"/repos/{repo}/pulls/{number}/reviews")
        except requests.HTTPError:
            continue
        for review in reviews:
            body = str(review.get("body") or "")
            submitted = str(review.get("submitted_at") or "")
            if AI_REVIEW_MARKER not in body or not submitted:
                continue
            try:
                submitted_at = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (start_utc <= submitted_at < end_utc):
                continue
            ai_review_count += 1
            for count, severity in re.findall(r"(\d+)\s+(critical|high|medium|low|nit)\b", body.lower()):
                ai_finding_counts[severity] += int(count)

    ai_findings_total = sum(ai_finding_counts.values())

    # The commits endpoint is based on the repository's default branch. It does not
    # count commits stranded on unmerged topic branches.
    default_branch_commits = client.list_endpoint(
        f"/repos/{repo}/commits",
        {"since": start_iso, "until": end_iso},
    )

    workflow_runs: list[dict[str, Any]] = []
    workflow_data_gaps: list[str] = []
    page = 1
    while True:
        try:
            payload = client.get(
                f"/repos/{repo}/actions/runs",
                {"created": f"{start_date}..{end_date}", "per_page": client.per_page, "page": page},
            )
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            workflow_data_gaps.append(f"Actions workflow runs unavailable (HTTP {status}).")
            break
        batch = payload.get("workflow_runs", [])
        workflow_runs.extend(run for run in batch if isinstance(run, dict))
        if len(batch) < client.per_page:
            break
        page += 1

    failed_runs = [run for run in workflow_runs if run.get("conclusion") in FAILURE_CONCLUSIONS]
    failed_review_runs = [
        run for run in failed_runs
        if "review" in str(run.get("name", "")).lower()
        or "ai-code-review" in str((run.get("path") or "")).lower()
    ]

    return {
        "repository": repo,
        "pull_requests_created": len(prs),
        "pull_requests": [
            {
                "number": item.get("number"),
                "title": item.get("title", "(untitled)"),
                "url": item.get("html_url", ""),
                "author": (item.get("user") or {}).get("login", "unknown"),
                "state": item.get("state", "unknown"),
                "created_at": item.get("created_at", ""),
            }
            for item in prs
        ],
        "default_branch_commits": len(default_branch_commits),
        "ai_reviews_submitted": ai_review_count,
        "ai_findings": ai_findings_total,
        "ai_findings_by_severity": ai_finding_counts,
        "workflow_runs": len(workflow_runs),
        "failed_workflow_runs": len(failed_runs),
        "failed_ai_review_runs": len(failed_review_runs),
        "failed_runs": [
            {
                "name": run.get("name", "Unnamed workflow"),
                "conclusion": run.get("conclusion"),
                "url": run.get("html_url", ""),
                "created_at": run.get("created_at", ""),
                "event": run.get("event", ""),
            }
            for run in failed_runs
        ],
        "data_gaps": workflow_data_gaps,
    }


def render_html(label: str, generated_at: datetime, rows: list[dict[str, Any]]) -> str:
    total_prs = sum(row["pull_requests_created"] for row in rows)
    total_commits = sum(row["default_branch_commits"] for row in rows)
    total_ai_findings = sum(row["ai_findings"] for row in rows)
    total_failures = sum(row["failed_workflow_runs"] for row in rows)
    total_ai_failures = sum(row["failed_ai_review_runs"] for row in rows)

    table_rows = []
    for row in rows:
        url = "https://github.com/" + row["repository"]
        table_rows.append(
            "<tr>"
            f"<td><a href='{html.escape(url, quote=True)}'>{html.escape(row['repository'])}</a></td>"
            f"<td>{row['pull_requests_created']}</td>"
            f"<td>{row['default_branch_commits']}</td>"
            f"<td>{row['ai_reviews_submitted']}</td>"
            f"<td>{row['ai_findings']}</td>"
            f"<td>{row['workflow_runs']}</td>"
            f"<td>{row['failed_workflow_runs']}</td>"
            f"<td>{row['failed_ai_review_runs']}</td>"
            "</tr>"
        )

    prs_html = []
    failures_html = []
    gaps_html = []
    for row in rows:
        for pr in row["pull_requests"]:
            prs_html.append(
                f"<li><a href='{html.escape(pr['url'], quote=True)}'>"
                f"{html.escape(row['repository'])} #{html.escape(str(pr['number']))}: "
                f"{html.escape(pr['title'])}</a> — {html.escape(pr['author'])}"
                f" ({html.escape(pr['state'])})</li>"
            )
        for failure in row["failed_runs"]:
            label_text = f"{row['repository']} — {failure['name']}"
            failures_html.append(
                f"<li><a href='{html.escape(failure['url'], quote=True)}'>"
                f"{html.escape(label_text)}</a> — {html.escape(str(failure['conclusion']))}"
                f" ({html.escape(str(failure['created_at']))})</li>"
            )
        for gap in row["data_gaps"]:
            gaps_html.append(f"<li>{html.escape(row['repository'])}: {html.escape(gap)}</li>")

    def list_or_empty(items: list[str], empty: str) -> str:
        return "".join(items) if items else f"<li>{html.escape(empty)}</li>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monthly AI Code Review Report — {html.escape(label)}</title>
<style>
body{{font-family:Arial,sans-serif;background:#f3f5f9;color:#202638;margin:0;padding:28px}}
main{{max-width:1120px;margin:auto;background:#fff;padding:30px;border-radius:12px}}
h1{{color:#182b49;margin-top:0}}.muted{{color:#687386}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:22px 0}}
.card{{background:#f0f4fa;padding:18px;border-radius:9px}}.value{{font-size:30px;font-weight:700;margin-top:8px}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}th,td{{padding:11px;border-bottom:1px solid #e5e9f0;text-align:left}}th{{background:#f7f9fc}}
a{{color:#1769aa}}li{{margin:8px 0;line-height:1.4}}.note{{background:#f7f9fc;padding:14px;border-radius:8px}}
</style></head><body><main>
<h1>Monthly AI Code Review Activity</h1>
<p class="muted">Period: {html.escape(label)} · Generated: {html.escape(generated_at.strftime('%Y-%m-%d %H:%M %Z'))}</p>
<section class="cards">
<div class="card">Pull requests created<div class="value">{total_prs}</div></div>
<div class="card">Default-branch commits<div class="value">{total_commits}</div></div>
<div class="card">AI review findings<div class="value">{total_ai_findings}</div></div>
<div class="card">Failed workflow runs<div class="value">{total_failures}</div></div>
<div class="card">Failed AI-review runs<div class="value">{total_ai_failures}</div></div>
</section>
<h2>Repository summary</h2>
<table><thead><tr><th>Repository</th><th>PRs created</th><th>Default-branch commits</th><th>AI reviews</th><th>AI findings</th><th>Workflow runs</th><th>Failed runs</th><th>Failed AI reviews</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table>
<h2>Pull requests created</h2><ul>{list_or_empty(prs_html, 'No pull requests were created during this reporting period.')}</ul>
<h2>Failed workflow runs / errors</h2><ul>{list_or_empty(failures_html, 'No failed workflow runs were reported.')}</ul>
<h2>Data availability</h2><ul>{list_or_empty(gaps_html, 'No GitHub Actions data-access gaps were reported.')}</ul>
<div class="note"><b>Metric definitions</b><br>
PRs are counted by PR creation date. AI findings are parsed from reviewer summary comments on those PRs where the AI review was submitted during this month. Commits are counted from the repository default branch only; commits on unmerged topic branches are not included. Failed workflow runs include failure, timed out, action required and startup failure conclusions. Failed AI reviews are failed runs whose workflow name/path contains “review”. AI findings are review candidates; a failed workflow is an automation/check failure, not necessarily a confirmed code defect.
</div></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    with Path(args.config).open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required.")

    start, end, label = month_window()
    client = GitHub(
        token,
        cfg.get("project", {}).get("github_api_url", "https://api.github.com"),
        cfg.get("report", {}).get("per_page", 100),
    )
    rows = [collect_repo(client, repo, start, end) for repo in cfg["project"]["repositories"]]

    totals = {
        "pull_requests_created": sum(row["pull_requests_created"] for row in rows),
        "default_branch_commits": sum(row["default_branch_commits"] for row in rows),
        "ai_reviews_submitted": sum(row["ai_reviews_submitted"] for row in rows),
        "ai_findings": sum(row["ai_findings"] for row in rows),
        "workflow_runs": sum(row["workflow_runs"] for row in rows),
        "failed_workflow_runs": sum(row["failed_workflow_runs"] for row in rows),
        "failed_ai_review_runs": sum(row["failed_ai_review_runs"] for row in rows),
    }
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_file = cfg.get("report", {}).get("html_file", "monthly-code-review-report.html")
    json_file = cfg.get("report", {}).get("json_file", "monthly-code-review-report.json")
    (out / html_file).write_text(render_html(label, datetime.now(REPORT_TZ), rows), encoding="utf-8")
    (out / json_file).write_text(json.dumps({"period": label, "totals": totals, "repositories": rows}, indent=2), encoding="utf-8")
    print(f"Generated {out / html_file}")
    print(f"Generated {out / json_file}")
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
