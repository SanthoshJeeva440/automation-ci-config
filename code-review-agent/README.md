# AI Code Review + Jenkins integration

This directory contains the shared configuration and Python utilities for two repositories:

- `SanthoshJeeva440/automation-pipelines`
- `SanthoshJeeva440/automation-ci-config`

## Architecture

1. A PR event starts the repository's `.github/workflows/ai-code-review.yml` using `pull_request_target`. This is deliberate: the review agent reads the PR diff through the GitHub API and does not check out or execute PR code. The trusted workflow can therefore post a review for fork PRs as well.
2. The reusable workflow in `SanthoshJeeva440/code-review` uses the OpenAI API (`provider: openai`, `model: gpt-4.1-mini`) and posts a GitHub pull request review with inline findings.
3. Once the AI review finishes, the GitHub workflow sends PR metadata and the review result to Jenkins through the Generic Webhook Trigger plugin.
4. Jenkins downloads the exact PR source archive, parses YAML files, validates Declarative Jenkinsfiles through the Jenkins Pipeline Model Converter endpoint, publishes a commit status named `jenkins/pr-validation`, and emails a result.
5. A separate monthly Jenkins job creates HTML and JSON reports for the previous month and emails the HTML attachment.

## GitHub repository secrets

Add these three **Actions secrets** to each target repository (or as organization secrets covering both repos):

| Secret | Value |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key. The API is billed separately from ChatGPT Plus/Pro. |
| `JENKINS_GENERIC_WEBHOOK_URL` | `https://YOUR-JENKINS-HOST/generic-webhook-trigger/invoke` (no token in the URL) |
| `JENKINS_PR_TRIGGER_TOKEN` | Random secret token used by the Jenkins Generic Webhook Trigger plugin |

The workflow uses OpenAI's Chat Completions API and the configured `gpt-4.1-mini` model. You can change the model in the reusable workflow call in both target repositories. Confirm the selected model is enabled for your OpenAI API project. The `.github/code-review.md` files in each target repository provide repository-specific review guidance.

## Jenkins prerequisites

Install/enable:

- Generic Webhook Trigger plugin
- Pipeline Utility Steps plugin (`readYaml`)
- Email Extension plugin (`emailext`)
- Git plugin
- Pipeline Model Definition plugin (Jenkinsfile lint endpoint)

Create these Jenkins credentials:

| Credential ID | Type | Purpose |
|---|---|---|
| `jenkins-pr-trigger-token` | Secret text | Same value as `JENKINS_PR_TRIGGER_TOKEN`; Generic Webhook Trigger authentication |
| `github-read-token` | Secret text | Fine-grained GitHub PAT with Contents: read, Pull requests: read, Actions: read, Metadata: read for the two repos |
| `github-pr-status-token` | Secret text | Fine-grained GitHub PAT with Commit statuses: read/write (and Metadata: read) for the two repos |
| `jenkins-api-user-token` | Username/password | Jenkins username + API token, used for Pipeline Model Converter validation |

Set Jenkins controller timezone to `Asia/Kolkata`. Configure SMTP/Gmail in the Email Extension settings. Do not store any tokens in this YAML or in GitHub files.

## Create the Jenkins PR job

1. Create a Pipeline job named `AI-Code-Review-PR`.
2. Configure Pipeline Definition as **Pipeline script from SCM**.
3. Repository: `https://github.com/SanthoshJeeva440/automation-pipelines.git`, branch: `main`.
4. Script Path: `pipelines/code-review-agent-pr/Jenkinsfile`.
5. Save and run the job manually once. This registers the Generic Webhook Trigger from the Jenkinsfile.
6. Check that the trigger token credential ID matches `jenkins-pr-trigger-token` and that `https://YOUR-JENKINS-HOST/generic-webhook-trigger/invoke` is reachable over HTTPS from GitHub Actions. The URL must forward to the Jenkins service port (commonly 8080); a tunnel forwarding to an unrelated local port will not reach this endpoint.

The job validates the YAML configuration and Jenkinsfiles in the PR snapshot. Its GitHub commit status context is `jenkins/pr-validation`. The AI review itself is also shown as a GitHub Actions PR check.

## Create the monthly report job

1. Create a second Pipeline job named `AI-Code-Review-Monthly-Report`.
2. Use the same `automation-pipelines` repository and branch `main`.
3. Script Path: `pipelines/code-review-agent-monthly-report/Jenkinsfile`.
4. Save and run once to register the cron schedule.
5. The report runs on the first day of each month at 07:00 Asia/Kolkata (IST) and covers the previous calendar month.

The report contains PRs created, commits reachable from each default branch, total GitHub Actions workflow runs, failed workflow runs and failed AI-review runs, broken down by repository. It does not treat every CI failure as a code defect.

## Important reporting definitions

- PR count uses the PR creation date in the reporting month.
- Commit count uses the commits API for the default branch; unmerged topic-branch commits are not counted.
- AI findings are extracted from the reviewer summary for AI reviews submitted during the reporting month.
- Failed runs are GitHub Actions runs concluding as `failure`, `timed_out`, `action_required`, or `startup_failure`.
- AI-review failures are failed workflow runs whose name or workflow path includes `review`.
- A failed build/check is an automation failure, not necessarily a confirmed source-code bug.

## Manual test

Open a small PR in either target repository. Expected sequence:

1. `AI Code Review` appears in PR Checks.
2. AI findings appear as a pull request review and inline comments.
3. Jenkins receives the event and runs YAML/Jenkinsfile validation.
4. `jenkins/pr-validation` appears in commit statuses.
5. Jenkins emails the PR validation result.

After creating both jobs and configuring secrets/credentials, use a test PR before relying on this for required branch protection checks.
