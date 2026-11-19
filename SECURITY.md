# Security policy

## Reporting a vulnerability

Do not open a public issue, discussion or pull request for a security problem.

Report it privately with GitHub private vulnerability reporting:

1. Open https://github.com/skopax/skopax and choose the **Security** tab.
2. Choose **Report a vulnerability** and fill in the form.

Please include the Skopax version, the steps to reproduce, and what an attacker could do. Do not include real secrets or personal data; use example values.

## What to expect

- We acknowledge your report within 5 working days.
- We keep you updated while we work on a fix.
- We agree a disclosure date with you. By default the problem is disclosed 90 days after the report, or earlier once a fixed release is out.
- We credit you in the advisory if you wish.

## Supported versions

Security fixes go into the latest release and the latest release candidate. Older versions are not patched; upgrade to the latest release.

## Image scanning

Every release image is scanned with Grype before it is promoted, and the release stops on any High or Critical finding that has a fix. A weekly scan of the `latest` image opens or updates one issue, "Image scan: fixable findings in latest", while there are fixable findings. The SARIF report is uploaded to code scanning. The scan covers the amd64 image only.

## Scope

This policy covers the Skopax community edition in the `skopax/skopax` repository and its published container image. For a problem in a third-party dependency, please also report it to that project.
