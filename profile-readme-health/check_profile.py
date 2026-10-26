#!/usr/bin/env python3
"""Check the rendered GitHub profile page, profile README, and README images."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "ProfileReadmeHealthCheck/1.0"
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)(?:\s+['\"][^)]*['\"])?\)")


def fetch(session: requests.Session, url: str, timeout: int) -> requests.Response:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to the YAML config")
    parser.add_argument("--report-dir", default="reports", help="Directory for JSON/Markdown reports")
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "check": "github-profile-readme-health",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "FAILED",
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    try:
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        profile = config["profile"]
        options = config.get("checks", {})
        page_timeout = int(options.get("page_http_timeout_seconds", 30))
        asset_timeout = int(options.get("asset_timeout_seconds", 20))
        failure_policy = str(options.get("external_asset_failure_policy", "fail")).lower()
        if failure_policy not in {"fail", "warn"}:
            raise ValueError("checks.external_asset_failure_policy must be 'fail' or 'warn'")
        error_markers = [str(x).casefold() for x in options.get("error_markers", [])]
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})

        # 1) Validate the live profile page responds and the expected README content is rendered.
        page_url = str(profile["page_url"])
        page_response = fetch(session, page_url, page_timeout)
        page_soup = BeautifulSoup(page_response.text, "html.parser")
        page_text = page_soup.get_text(" ", strip=True)
        result["checks"].append({
            "name": "profile_page_http",
            "status": "PASS",
            "url": page_url,
            "http_status": page_response.status_code,
            "final_url": page_response.url,
        })
        for phrase in options.get("required_page_text", []):
            passed = str(phrase).casefold() in page_text.casefold()
            result["checks"].append({
                "name": f"required_page_text: {phrase}",
                "status": "PASS" if passed else "FAIL",
                "url": page_url,
            })
            if not passed:
                result["errors"].append(f"Expected rendered profile text was not found: {phrase}")

        # 2) Fetch README source. This catches source URL/config problems separately from rendering.
        raw_url = str(profile["readme_raw_url"])
        raw_response = fetch(session, raw_url, page_timeout)
        readme = raw_response.text
        result["checks"].append({
            "name": "raw_readme_http",
            "status": "PASS",
            "url": raw_url,
            "http_status": raw_response.status_code,
        })

        # HTML-style <img> tags are used by this README; also support Markdown image syntax.
        source_soup = BeautifulSoup(readme, "html.parser")
        image_urls = [
            urljoin(raw_url, tag.get("src", "").strip())
            for tag in source_soup.select("img[src]")
            if tag.get("src", "").strip()
        ]
        image_urls.extend(MARKDOWN_IMAGE_RE.findall(readme))
        image_urls.extend(str(x).strip() for x in options.get("additional_image_urls", []) if str(x).strip())
        image_urls = list(dict.fromkeys(image_urls))
        if not image_urls:
            result["errors"].append("No README image URLs were found; the checker may not be parsing the README correctly.")

        # 3) Check every image endpoint directly. Stats services sometimes return HTTP 200
        # with an SVG error card, so detect common error text as well as bad HTTP/content type.
        if options.get("inspect_all_readme_images", True):
            for url in image_urls:
                try:
                    response = session.get(url, timeout=asset_timeout, allow_redirects=True)
                    content_type = response.headers.get("Content-Type", "").lower()
                    body = response.content[:1_000_000]
                    body_lower = body.lower()
                    matched_error = next((marker for marker in error_markers if marker.encode("utf-8") in body_lower), None)
                    looks_like_image = (
                        content_type.startswith("image/")
                        or b"<svg" in body_lower[:4096]
                        or body.startswith(b"\x89PNG\r\n\x1a\n")
                        or body.startswith(b"\xff\xd8\xff")
                        or body.startswith((b"GIF87a", b"GIF89a", b"RIFF"))
                    )
                    passed = 200 <= response.status_code < 300 and looks_like_image and not matched_error
                    check = {
                        "name": "readme_image",
                        "status": "PASS" if passed else "FAIL",
                        "url": url,
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "error_marker": matched_error,
                    }
                    result["checks"].append(check)
                    if not passed:
                        message = (
                            f"README image failed: HTTP {response.status_code}, content-type={content_type}, "
                            f"error_marker={matched_error!r}, URL={url}"
                        )
                        (result["errors"] if failure_policy == "fail" else result["warnings"]).append(message)
                except requests.RequestException as exc:
                    message = f"Could not check README image URL {url}: {type(exc).__name__}: {exc}"
                    result["checks"].append({"name": "readme_image", "status": "FAIL", "url": url, "error": str(exc)})
                    (result["errors"] if failure_policy == "fail" else result["warnings"]).append(message)

        result["status"] = "FAILED" if result["errors"] else "PASSED"

    except Exception as exc:  # ensure configuration/network errors also get a report
        result["errors"].append(f"Checker could not complete: {type(exc).__name__}: {exc}")

    json_path = report_dir / "profile-readme-health.json"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    md_lines = [
        "# GitHub Profile README Health Check", "",
        f"- **Overall status:** {result['status']}",
        f"- **Checked at (UTC):** {result['timestamp_utc']}",
        f"- **Total checks:** {len(result['checks'])}",
        f"- **Passed:** {sum(c.get('status') == 'PASS' for c in result['checks'])}",
        f"- **Failed:** {sum(c.get('status') == 'FAIL' for c in result['checks'])}",
        "", "## Errors", *([f"- {x}" for x in result["errors"]] or ["- None"]),
        "", "## Warnings", *([f"- {x}" for x in result["warnings"]] or ["- None"]),
        "", "## Check details", "",
    ]
    for check in result["checks"]:
        md_lines.append(f"- **{check.get('status', 'INFO')}** — {check.get('name', 'check')}: {check.get('url', '')}")
        if check.get("error_marker"):
            md_lines.append(f"  - Error marker: `{check['error_marker']}`")
    md_path = report_dir / "profile-readme-health.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(md_path.read_text(encoding="utf-8"))
    print(f"JSON report saved to: {json_path}")
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
