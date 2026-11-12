# Security Policy

## Reporting a vulnerability

This project documents an unofficial client for a consumer device's cloud
API. If you find a security issue in the code, the protocol documentation,
or the scheduler's handling of credentials — **please do not open a public
issue**.

Contact the maintainer **via GitHub's private security advisory** (repo →
Security → Report a vulnerability) — this avoids exposing details publicly
and lands directly in the maintainer's inbox. You should expect an
acknowledgement within 48 hours and a status update within a week.

## What is NOT in scope (by design)

- **The Bestway/MXCHIP cloud APIs themselves.** This project documents an
  unofficial client for a third-party service; flaws in that service belong to
  Bestway, not this repository. Use a *disclosure* channel, not this one.
- **Weaknesses that require physical access** to the tub or its WiFi module.
- **Self-inflicted account lockouts** from repeated logins (see README).

## Security-relevant repository facts

- **No secrets are ever committed.** `secrets.env` (app credentials),
  `tokens.json` (auth tokens), `shadow_log.jsonl`, `scheduler.env`,
  `scheduler_model.json` and `scheduler.log` are all gitignored. The GitHub
  history has been scrubbed of device-specific data.
- **Runtime dependency surface is minimal:** both tools use the Python
  standard library only. There is no `requirements.txt` to audit.
- **CI runs with least privilege** (`permissions: contents: read`) and pins
  all actions to commit SHAs (see `.github/workflows/ci.yml`).

## Recommended GitHub settings

To harden the repository further, enable in the repo's **Settings → Code
security and analysis**:

- **Dependabot alerts** + **Dependabot security updates** (config in
  `.github/dependabot.yml`)
- **Secret scanning** (push protection)
- **Branch protection** on `master`: require pull-request reviews and status
  checks (CI must pass), and require signed commits.
