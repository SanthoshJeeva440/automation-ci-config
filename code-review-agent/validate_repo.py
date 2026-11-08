#!/usr/bin/env python3
"""Validate YAML files and Declarative Jenkinsfiles in a downloaded PR snapshot."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
import yaml


def validate_yaml_files(root: Path, changed_files: set[str] | None = None) -> list[str]:
    failures: list[str] = []
    files = sorted([*root.rglob("*.yml"), *root.rglob("*.yaml")])
    if changed_files is not None:
        files = [path for path in files if path.relative_to(root).as_posix() in changed_files]
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as stream:
                yaml.safe_load(stream)
            print(f"[PASS] YAML: {path.relative_to(root)}")
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            failures.append(f"{path.relative_to(root)}: {exc}")
            print(f"[FAIL] YAML: {path.relative_to(root)}: {exc}")
    if not files:
        print("[INFO] No YAML files found.")
    return failures


def get_crumb(session: requests.Session, base_url: str) -> dict[str, str]:
    response = session.get(f"{base_url.rstrip('/')}/crumbIssuer/api/json", timeout=20)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    data = response.json()
    if data.get("crumbRequestField") and data.get("crumb"):
        return {data["crumbRequestField"]: data["crumb"]}
    return {}


def validate_jenkinsfiles(root: Path, changed_files: set[str] | None = None) -> list[str]:
    files = sorted(path for path in root.rglob("Jenkinsfile") if path.is_file())
    if changed_files is not None:
        files = [path for path in files if path.relative_to(root).as_posix() in changed_files]
    if not files:
        print("[INFO] No Jenkinsfiles found.")
        return []

    base_url = os.environ.get("JENKINS_URL", "").strip()
    username = os.environ.get("JENKINS_LINTER_USER", "")
    api_token = os.environ.get("JENKINS_LINTER_TOKEN", "")
    if not base_url or not username or not api_token:
        return ["Jenkinsfile lint was not run: configure JENKINS_URL and Jenkins credential jenkins-api-user-token."]

    session = requests.Session()
    session.auth = (username, api_token)
    headers = get_crumb(session, base_url)
    endpoint = f"{base_url.rstrip('/')}/pipeline-model-converter/validate"
    failures: list[str] = []

    for path in files:
        relative = str(path.relative_to(root))
        try:
            content = path.read_text(encoding="utf-8")
            response = session.post(
                endpoint,
                data={"jenkinsfile": content},
                headers=headers,
                timeout=45,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Jenkins linter HTTP {response.status_code}: {response.text[:1000]}")
            output = response.text.strip()
            if "Errors encountered validating Jenkinsfile" in output or output.lower().startswith("error"):
                raise RuntimeError(output[:2000])
            print(f"[PASS] Jenkinsfile: {relative}")
        except (OSError, requests.RequestException, RuntimeError) as exc:
            failures.append(f"{relative}: {exc}")
            print(f"[FAIL] Jenkinsfile: {relative}: {exc}")
    return failures


def get_changed_files(repository: str, pr_number: int, token: str) -> set[str]:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    paths: set[str] = set()
    page = 1
    while True:
        response = session.get(
            f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files",
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise RuntimeError("GitHub changed-files API returned an unexpected response")
        paths.update(str(item.get("filename")) for item in items if item.get("filename"))
        if len(items) < 100:
            break
        page += 1
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    args = parser.parse_args()

    root = args.source.resolve()
    if not root.is_dir():
        print(f"Source directory does not exist: {root}", file=sys.stderr)
        return 2

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required to obtain the PR changed-file list.", file=sys.stderr)
        return 2
    try:
        changed_files = get_changed_files(args.repository, args.pr_number, token)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Could not obtain the PR changed-file list: {exc}", file=sys.stderr)
        return 2

    print(f"Checking {len(changed_files)} changed file(s) from PR #{args.pr_number}.")
    failures = validate_yaml_files(root, changed_files)
    if args.repository == "SanthoshJeeva440/automation-pipelines":
        failures.extend(validate_jenkinsfiles(root, changed_files))

    print(f"\nValidation summary: {len(failures)} error(s).")
    if failures:
        print("\n".join(f" - {item}" for item in failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
