# Contributing to Skopax

Thank you for helping with Skopax. This page covers what we accept, how to set the project up, the rules every change follows, how to sign off your commits, how to report a security problem, and the Code of Conduct.

## What we accept

- Issues are welcome now. Use the issue forms for bugs and feature requests, and describe what you saw and what you expected.
- Pull requests are welcome. Keep them small and focused, and link each one to an issue for anything beyond a typo.
- Contributions go to the **Skopax community edition**: the public repositories of the `skopax` GitHub organization, starting with `skopax/skopax`. The community edition is open source under the GNU Affero General Public License, version 3 (AGPL-3.0-only).
- Skopax Cloud and the Enterprise edition, if they come, will not be open for contributions.
- A plug-in you publish yourself is your own work under your own terms. It is not a contribution to Skopax. See `TRADEMARKS.md` for how to name it.
- For anything larger than a small fix, open an issue first and describe what you want to change. That saves you work if the change does not fit.

## Set up

You need Docker with Compose, Python 3.15, [uv](https://docs.astral.sh/uv/) and Node.js 22 (22.22.2 or later; the page suites' jsdom needs it).

Run Skopax locally:

    docker compose up --build

Then open http://localhost:8080 and follow "First run" in `README.md`. Plain `docker compose up` reuses the last image; add `--build` after you change code.

Run the Python tests:

    uv sync --frozen
    .venv/bin/python -m pytest -q

Run the page suites:

    (cd tests/page && npm ci)
    node tests/page/run.js

Both must pass before you open a pull request. CI runs them again. `docs/development.md` has the full guide: the layout of the code, the test kits and how to run a single test.

## Project rules

Every change follows these rules. A reviewer will ask for changes that do not.

1. **Tests first.** Write a test that fails and shows the bug or the new behaviour, then write the code that makes it pass. A change in behaviour comes with a test. A change to the page comes with a page suite test.
2. **No code comments.** Name things so the code explains itself, and put explanations in the pull request description. The only comments allowed are `# given`, `# when`, `# then` in Python tests and `// given`, `// when`, `// then` in page tests.
3. **Person-facing text goes through `skopax/messages.py`.** Any text a person reads (the page, chat, Slack, notifications, error messages) is a key in `MESSAGES`, not a string in the code. Write `{product}` where the product name appears, so the display name setting applies. You do not need to translate a new text: the built-in language packs show it in English, the pack test warns, and the maintainers add the translations before a release.
4. **Neutral examples.** Use `example.com` and its subdomains for hosts and email addresses, and alice, bob and carol for people. Do not use real companies, people, host names, IP addresses or account ids in code, tests, fixtures, docs or screenshots.
5. **No secrets in issues or pull requests.** Remove tokens, passwords, keys, cookies and internal host names from logs, screenshots and configuration before you post them. If you posted a secret by mistake, revoke it first.
6. **Small pull requests.** One change per pull request, with a description of what changed and why, and a link to the issue.

## Licence and sign-off

By signing off a contribution, you also license it to the project under Apache-2.0. Skopax itself is distributed under AGPL-3.0-only with the plug-in exception in `NOTICE`, and the example plug-ins under `examples/` under Apache-2.0. Your contribution is covered by the plug-in exception too. There is no separate agreement to sign.

Every commit in a pull request carries a `Signed-off-by` line under the Developer Certificate of Origin, version 1.1 (https://developercertificate.org). The sign-off says that you wrote the change, or have the right to submit it, under Apache-2.0 as above. Add it with:

    git commit -s -m "Explain the change"

That appends a line like this, using your real name and the email address of your git configuration:

    Signed-off-by: Alice Example <alice@example.com>

The pull request title follows Conventional Commits: `type: summary`, with type `feat`, `fix`, `docs`, `ci`, `test`, `chore`, `build`, `perf` or `refactor`, an optional `(scope)` after the type, and `!` before the colon for a breaking change, for example `fix(page): keep the sidebar open`. The title becomes the squash commit.

If you forgot it on the last commit, run `git commit --amend -s --no-edit`. For several commits, run `git rebase --signoff` on your branch and push again.

## Code you did not write

Only submit code and other material that you wrote yourself, unless you tell us otherwise. If a pull request includes anything written by someone else (copied code, a vendored library, an image, a data file):

- say so in the pull request description, with where it comes from and its licence;
- keep its copyright and licence notices;
- expect us to accept only licences that fit AGPL-3.0-only, for example Apache-2.0, MIT, BSD, ISC, PSF or MPL-2.0. We cannot accept licences that add terms the AGPL does not allow, such as the Commons Clause, SSPL or BUSL; MPL-2.0 is accepted for dependencies.

## Reporting a security issue

Do not open a public issue, discussion or pull request for a security problem. `SECURITY.md` explains how to report it privately, with GitHub private vulnerability reporting.

## Code of Conduct

Everyone who takes part in Skopax spaces (issues, pull requests, discussions, chat and events) follows the Code of Conduct in `CODE_OF_CONDUCT.md`. It is the Contributor Covenant, version 2.1 (https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Report a problem to hello@skopax.io.

## Name and logo

The Skopax name and the Beam X logo are trademarks. You can fork and change the code under AGPL-3.0-only, but when you distribute it or offer it as a service, a modified copy must not call itself Skopax or use the logo as its own branding. `TRADEMARKS.md` explains what is fine and what needs permission.

## Licence

Skopax is licensed under the GNU Affero General Public License, version 3 only (`LICENSE`). `NOTICE` adds a plug-in exception under section 7 of the AGPL: plug-ins that use only the documented plug-in interface may carry any licence. Copies you ship must include `LICENSE` and `NOTICE`. The example plug-ins under `examples/` are Apache-2.0.
