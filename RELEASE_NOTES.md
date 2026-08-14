# v0.1.1

Changes since `v0.1.0`:

- coerce report values before formula-injection checks and refuse totals that cannot be represented exactly;
- align account-code parsing across report layouts and supported Python/Windows CI legs;
- serialise token-cache refresh and persistence so concurrent processes do not spend the same rotating refresh token; and
- add workflow-built source archives, SHA-256 checksums, an SPDX SBOM and GitHub build attestations.

The release contains source and fabricated samples only. It contains no Xero credentials, tokens or client exports and remains read-only against Xero.
