#!/usr/bin/env python3
"""HTTP and browser-based health checks for a personal portfolio."""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def load_config(path):
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    url = str(config.get("portfolio", {}).get("url", "")).strip()
    if not url or "YOUR-PORTFOLIO-URL" in url:
        raise ValueError("Set portfolio.url in config.yml to your published portfolio URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("portfolio.url must be a valid http:// or https:// URL.")
    return config


def add_result(results, name, passed, details, duration_ms=0):
    results.append({"name": name, "status": "PASS" if passed else "FAIL",
                    "details": str(details), "duration_ms": duration_ms})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}: {details}")


def write_reports(report_dir, results, started, final_url):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "final_url": final_url,
        "duration_seconds": round(time.monotonic() - started, 2),
        "summary": {
            "total": len(results),
            "passed": sum(r["status"] == "PASS" for r in results),
            "failed": sum(r["status"] == "FAIL" for r in results),
        },
        "checks": results,
    }
    (report_dir / "portfolio-health-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Portfolio Health Check Report", "",
        f"- Generated (UTC): {report['generated_at_utc']}",
        f"- Final URL: {final_url or 'not available'}",
        f"- Duration: {report['duration_seconds']} seconds",
        f"- Total: {report['summary']['total']}",
        f"- Passed: {report['summary']['passed']}",
        f"- Failed: {report['summary']['failed']}", "",
        "| Status | Check | Details |", "|---|---|---|",
    ]
    for result in results:
        details = re.sub(r"\s+", " ", result["details"]).replace("|", r"\|")
        lines.append(f"| {result['status']} | {result['name']} | {details} |")
    (report_dir / "portfolio-health-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args()
    started, results, final_url = time.monotonic(), [], ""

    try:
        config = load_config(args.config)
    except Exception as exc:
        add_result(results, "Configuration", False, exc)
        write_reports(args.report_dir, results, started, final_url)
        return 1

    checks = config.get("checks", {})
    url = config["portfolio"]["url"]
    timeout = int(checks.get("http_timeout_seconds", 30))
    base = urlparse(url)

    http_started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True,
                                headers={"User-Agent": "PortfolioHealthCheck/1.0"})
        final_url = response.url
        add_result(results, "HTTP availability", 200 <= response.status_code < 400,
                   f"HTTP {response.status_code}; final URL: {response.url}",
                   int((time.monotonic() - http_started) * 1000))
        html = response.text
    except requests.RequestException as exc:
        add_result(results, "HTTP availability", False, exc)
        write_reports(args.report_dir, results, started, final_url)
        return 1

    for expected in checks.get("required_text", []):
        found = expected.casefold() in html.casefold()
        add_result(results, f"HTML text: {expected}", found,
                   "Found in response HTML" if found else "Not found in response HTML")

    console_errors, failed_requests, asset_failures = [], [], []
    try:
        with sync_playwright() as p:
            browser_type = getattr(p, str(checks.get("browser", "chromium")).lower(), None)
            if browser_type is None:
                raise ValueError("Unsupported Playwright browser setting.")
            browser = browser_type.launch(headless=bool(checks.get("headless", True)))
            page = browser.new_page()
            page.set_default_timeout(int(checks.get("browser_timeout_ms", 20000)))
            page.on("console", lambda msg: console_errors.append(msg.text)
                    if msg.type == "error" else None)
            page.on("requestfailed", lambda req: failed_requests.append(
                f"{req.url}: {req.failure}"
            ) if urlparse(req.url).netloc == base.netloc else None)

            nav_started = time.monotonic()
            try:
                nav_response = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                nav_response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            final_url = page.url
            status = nav_response.status if nav_response else None
            add_result(results, "Browser page load",
                       status is not None and 200 <= status < 400,
                       f"HTTP {status}; URL: {page.url}",
                       int((time.monotonic() - nav_started) * 1000))

            body_text = page.locator("body").inner_text()
            for expected in checks.get("required_text", []):
                found = expected.casefold() in body_text.casefold()
                add_result(results, f"Rendered text: {expected}", found,
                           "Found in rendered page" if found else "Not found in rendered page")

            for section_id in checks.get("required_sections", []):
                count = page.locator(f"#{section_id}").count()
                add_result(results, f"Section ID: #{section_id}", count > 0,
                           f"Found {count} element(s)" if count else "Section ID not found")

            if checks.get("check_same_origin_assets", True):
                assets = page.evaluate("""() => {
                    const nodes = document.querySelectorAll(
                      'link[rel="stylesheet"][href], script[src], img[src], source[src]'
                    );
                    return [...new Set([...nodes].map(n => n.href || n.src).filter(Boolean))];
                }""")
                for asset_url in assets:
                    parsed = urlparse(asset_url)
                    if parsed.netloc and parsed.netloc != base.netloc:
                        continue
                    try:
                        asset_response = requests.get(asset_url, timeout=timeout, allow_redirects=True)
                        if not 200 <= asset_response.status_code < 400:
                            asset_failures.append(f"{asset_url} -> HTTP {asset_response.status_code}")
                    except requests.RequestException as exc:
                        asset_failures.append(f"{asset_url} -> {exc}")
                add_result(results, "Same-origin assets", not asset_failures,
                           "All checked assets returned 2xx/3xx" if not asset_failures
                           else "; ".join(asset_failures))

            add_result(results, "Browser console errors",
                       not console_errors or not checks.get("fail_on_console_errors", True),
                       "No console errors" if not console_errors else " | ".join(console_errors[:10]))
            add_result(results, "Failed same-origin requests", not failed_requests,
                       "No failed same-origin requests" if not failed_requests
                       else " | ".join(failed_requests[:10]))
            browser.close()
    except Exception as exc:
        add_result(results, "Playwright browser checks", False,
                   f"{type(exc).__name__}: {exc}")

    write_reports(args.report_dir, results, started, final_url)
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
