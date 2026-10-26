# Security policy

## Scope

This repository contains local photo-audit and authorized geolocation
research tooling. It must not be used to scrape private services, bypass
provider access controls, or identify private people or addresses without
authorization.

## Secrets and personal data

- Never commit API keys, access tokens, `.env` files, personal photos, model
  weights, or geotagged reference exports.
- Keep provider credentials in environment variables. Rotate any credential
  pasted into chat, logs, issues, or commits.
- Treat ordinary privacy-audit reports as coordinate-free. Exact coordinates
  belong only in explicitly authorized research artifacts.

## Reporting

For a security issue, open a private report with a reproducible description and
do not include personal images or credentials in the issue body.
