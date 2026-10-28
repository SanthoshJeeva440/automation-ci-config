# Robot Framework Selenium CI configuration

Shared YAML configuration for the parameterized Jenkins pipeline.

- `config.yml`: repository, branch, test paths, agent names, and Robot Framework variable names.
- Do not store credentials or tokens here; use Jenkins Credentials for secrets.

The selected Jenkins node must have Python 3, venv support, and required browser dependencies installed. The existing test repository README describes SeleniumLibrary and variables named `env`, `browser`, and `mode`.
