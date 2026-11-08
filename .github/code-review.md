# AI review guidelines — automation-ci-config

Review YAML configuration as executable CI/CD behavior, not just formatting.

- Check YAML indentation, duplicate/ambiguous keys, data types, required keys, and values consumed by the associated Jenkinsfile or Python runner.
- Verify repository names, branch names, file paths, credential IDs, measurement/bucket names, and URLs are consistent with the consuming pipeline.
- Check cron expressions and document that Jenkins evaluates cron in the controller's configured timezone.
- Secrets must be referenced by Jenkins/GitHub secret identifiers; never commit secret values or personal access tokens.
- Look for missing notification settings, unsafe defaults, invalid ranges, and values that silently skip collection or validation.
- Prefer actionable defects tied to changed lines. Do not report stylistic preferences or speculative issues as bugs.
