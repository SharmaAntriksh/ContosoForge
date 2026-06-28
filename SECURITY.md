# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately, not through public issues or
pull requests.

Use GitHub's private reporting: open the **Security** tab of this repository and
click **Report a vulnerability**. That opens a private channel with the
maintainer. (If you cannot use that, open a minimal public issue asking for a
private contact, without any vulnerability details.)

You can expect an initial acknowledgement within a few days. ContosoForge is a
noncommercial, best-effort project, so fix timelines are not guaranteed, but
security reports are triaged as a priority.

## Scope

ContosoForge generates synthetic data and ships a local web UI (FastAPI)
intended for localhost use. In scope: the data pipeline, the SQL and Power BI
script generators, and the web layer. Out of scope: issues that require a
non-default, intentionally insecure configuration, or vulnerabilities in
third-party dependencies (report those upstream; Dependabot tracks known CVEs
here).
