# Contributing

Thanks for helping improve Photo Privacy Audit.

## Before opening a pull request

1. Open or comment on an Issue describing the problem or proposed change.
2. Fork the repository and create a focused branch from `main`.
3. Keep personal photos, exact private coordinates, API keys, model weights,
   downloaded datasets, and provider tokens out of commits.
4. Add or update tests for behavior changes.
5. Run the local checks:

   ```powershell
   python -m pytest -q -rs
   python -m compileall -q src scripts
   ```

6. Open a pull request with the motivation, scope, tests, data/license
   provenance, and known limitations.

## Research and data contributions

Geolocation experiments must use an authorized reference manifest. Include the
source, license, authorization status, capture metadata, and a reproducible
manifest digest. Do not add scraped social-media data or undocumented provider
endpoints.

## Review expectations

Small, focused pull requests are easiest to review. Claims of localization
quality should include a held-out evaluation or a clearly labeled qualitative
example; a visually similar image is not proof of an exact private address.
