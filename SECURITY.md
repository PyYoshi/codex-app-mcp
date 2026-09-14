# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for security
issues. Do not open a public issue containing credentials, exploit details, or
other sensitive information.

Include the affected version, configuration, reproduction steps, impact, and
any suggested mitigation. You should receive an acknowledgement within seven
days. A remediation timeline will depend on severity and reproducibility.

## Supported versions

Until a newer release is published, only the latest tagged release is
supported. The `main` branch may contain unreleased changes.

The bridge intentionally rejects interactive approvals and unrestricted
sandboxing. Reports about bypasses of `allowed_roots`, sandbox or approval
policy, cross-thread isolation, cancellation, process shutdown, secret logging,
or recursive self-connection are especially valuable.
