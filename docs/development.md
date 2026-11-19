# Development

Working on Skopax itself: the tools, the tests, continuous integration, the licence gate and releases. [CONTRIBUTING.md](../CONTRIBUTING.md) has the rules every change follows.

## Set up

You need Docker with Compose, Python 3.15, uv and Node.js 22 (22.22.2 or later; the page suites' jsdom needs it).

Skopax manages its Python packages with uv (https://docs.astral.sh/uv/). Install uv, then create `.venv` with the exact versions in `uv.lock` and run the tests:

    uv sync --frozen
    .venv/bin/python -m pytest -q

If `uv sync` reports that no Python 3.15 interpreter was found, update uv with `uv self update` and run it again.

After changing a dependency in `pyproject.toml`, update the lock:

    uv lock

`uv.lock` is the only pinned list; no requirements files are kept in the repository.

To install with pip instead, export the locked, hash-checked set with uv once, then install it with pip in a Python 3.15 virtual environment:

    uv export --frozen --group dev --no-header --no-annotate > requirements-dev.txt
    pip install --require-hashes -r requirements-dev.txt

Run Skopax from the clone with Docker Compose:

    docker compose up --build

Then open http://localhost:8080 and follow [First run](../README.md#first-run). Plain `docker compose up` reuses the last image; add `--build` after you change code.

## The code

| Path | What it holds |
|---|---|
| `skopax/` | The service: `main.py` starts it, `app.py` holds the web routes, `config.py` the settings, `store.py` the MongoDB access and `messages.py` every text a person reads |
| `skopax/contract.py`, `skopax/kit/` | The plug-in contract and the helpers plug-ins share (see [Writing a plug-in](extending.md#writing-a-plug-in)) |
| `skopax/plugins/` | The built-in plug-ins, one package each |
| `skopax/investigator/` | The AI investigator: its prompt, tools, gate and runs |
| `skopax/mcptools/` | Outside MCP servers (see [Outside MCP servers](plugins.md#outside-mcp-servers)) |
| `skopax/chatui/` | The Chat page |
| `skopax/web/` | The page: the HTML templates, the scripts in `static/`, the icons and the brand files |
| `examples/hello-plugin/` | The small example plug-in (see [The example plug-in: Hello](extending.md#the-example-plug-in-hello)) |
| `examples/couchbase-plugin/`, `examples/managed-redis-plugin/` | The two fuller example plug-ins (see [More examples](extending.md#more-examples)) |
| `tests/` | The Python tests, their kits and fakes |
| `tests/page/` | The page suites |
| `tools/` | The build and release tools |
| `demo/` | The Prometheus setup of the demo profile |

## The tests

The Python tests run with pytest. `pytest.ini` runs them in parallel (`-n auto`, at most 4 workers). For one file or one test, run it in a single process with `-n 0`:

    .venv/bin/python -m pytest -q -n 0 tests/test_readme.py
    .venv/bin/python -m pytest -q -n 0 tests/test_readme.py::test_every_docs_link_resolves

The tests need no outside services: MongoDB is `mongomock`, and the services Skopax talks to are fakes. The shared kits and fakes live next to the tests:

- `tests/plugin_kit.py`: a plug-in, its instances and a context to run its checks and tools;
- `tests/fake_http.py`: a fake transport that answers chosen routes, for plug-in reads through `ctx.http()`;
- `tests/fake_anthropic.py`, `tests/fake_slack.py`, `tests/fake_google.py` and `tests/fake_cdp.py`: the Anthropic API, Slack, Google sign-in and the browser service;
- `tests/mcp_fake.py`: an outside MCP server;
- `tests/sso_kit.py`: an OpenID Connect provider;
- `tests/agent_kit.py`, `tests/kube_kit.py`, `tests/teams_kit.py` and `tests/website_kit.py`: agents, Kubernetes, Teams routes and the Website plug-in;
- `tests/plugins/`: small plug-ins that the registry tests load.

The page suites run the page's scripts in jsdom, from `tests/page`:

    (cd tests/page && npm ci)
    node tests/page/run.js

`run.js` runs every `*.test.js` file, at most 4 at a time, and prints one line per suite. Give it part of a file name to run only the matching suites, for example `node tests/page/run.js skopax-sidebar`. The page clock follows `SKOPAX_TEST_TZ` (default `UTC`); CI runs the suites in `UTC` and in `Asia/Tokyo`:

    SKOPAX_TEST_TZ=Asia/Tokyo node tests/page/run.js

Both the Python tests and the page suites must pass before a pull request.

## Continuous integration

The workflows are in `.github/workflows/`. Every action is pinned by a full commit SHA, and every workflow starts from `permissions: {}`, so each job asks only for what it uses.

- **ci** (`ci.yml`) runs on every pull request, forks included, on every push to `main`, and as a reusable workflow called by `image`. It has read-only permissions and uses no secrets. A newer push to the same pull request cancels the older run; runs on `main` and tag runs are never cancelled. It runs four jobs:
  - `tests`: Python 3.15 through uv 0.13.8, then `uv lock --check` (it fails when `uv.lock` no longer matches `pyproject.toml`), `uv sync --frozen` and `uv run --frozen pytest -q -n auto`; then `uv run --frozen python -m tests.presets_promtool "$RUNNER_TEMP/presets"` writes a `promtool test rules` file that runs every custom check preset on the test data of the example plug-ins, and `docker run --rm -v "$RUNNER_TEMP/presets:/work:ro" -w /work --entrypoint promtool "$PROMETHEUS_IMAGE" test rules presets-test.yml` runs it with promtool from `prom/prometheus:v3.1.0`, the image `docker-compose.yml` uses.
  - `pages`: Node 22.22, `npm ci` in `tests/page`, then `node tests/page/run.js` once with `SKOPAX_TEST_TZ=UTC` and once with `SKOPAX_TEST_TZ=Asia/Tokyo`.
  - `licences`: `uv sync --frozen --no-dev`, then `python -m tools.licences check --site .venv/lib/python3.15/site-packages --out <tmp>`. It fails and names any dependency whose licence is not allowed (see [The licence gate](#the-licence-gate)).
  - `secrets`: gitleaks 8.30.1 (archive checksum-verified) over the full git history, with `.gitleaks.toml`. That file extends the default rules. It allowlists only nine named fake values, each in its own test fixture file and for one rule, plus, for those same three rules, the Python bytecode that pytest writes under `tests/__pycache__/` (git never tracks it, so only a scan of a working tree sees it); any other secret in those files is still reported.
- **image** (`image.yml`) runs only when a `v*` tag is pushed. It first runs the whole `ci` workflow as a reusable workflow, without secrets, so a tag never builds an image without passing tests. Then it runs five jobs, in order:
  - `version`: checks the tag against `pyproject.toml` with `python -m tools.release_version check` and fails before any build on a mismatch. `vX.Y.Z-rc.N` must match `X.Y.ZrcN`, and `vX.Y.Z` must match `X.Y.Z`. It then lists the image tags with `tools.release_version tags`: the first is the exact version tag, and a list without `latest` marks a release candidate. The image name follows the repository, lowercased: `ghcr.io/<owner>/<repo>`, which is `ghcr.io/skopax/skopax` for the real repository.
  - `build`: builds `linux/amd64` and `linux/arm64` with Buildx and QEMU, passing `SKOPAX_VERSION` and `COMMIT_ID`. It pushes only the exact version tag (`X.Y.Z` or `X.Y.Z-rc.N`) to GHCR, with SBOM and provenance attestations, and in a public repository it also adds a GitHub build-provenance attestation.
  - `layer-smoke`: builds `examples/hello-plugin` on the image just pushed, passed as `BASE_IMAGE`, then `examples/couchbase-plugin` and `examples/managed-redis-plugin` on top of it, one layer each, and runs the result against Mongo 8.0.4. The bootstrap token, secret key and first-admin password are generated in the job. It checks that `GET /api/admin/plugins` shows `hello`, `couchbase` and `managed-redis` as `ok` and that the icon of each (`/plugins/hello/icon.svg` and so on) answers 200.
  - `scan`: logs in to GHCR and scans the exact digest `build` pushed (`<image>@<digest>`) with Grype v0.121.1, through the `anchore/scan-action` pinned by commit SHA. It scans the amd64 image only, the platform of the runner. It fails on any High or Critical finding that has a fix (`only-fixed` with `severity-cutoff: high`), applies the ignore list in `.grype.yaml`, and uploads the SARIF report to code scanning under the category `image-release`. It is the only job in `image.yml` with `security-events: write`.
  - `promote`: runs only after the smoke passes, in the `release` environment. It also waits for `scan`.
    - For a release tag `vX.Y.Z`, it adds `X.Y` and `latest` on GHCR to the same image, with no rebuild, then copies `X.Y.Z`, `X.Y` and `latest` to `docker.io/skopax/skopax`.
    - For an rc tag (`vX.Y.Z-rc.N`) it does nothing: an rc never gets `latest` or `X.Y` and never reaches Docker Hub.
    - An image that fails the smoke or the scan never becomes `latest`.
- **scan** (`scan.yml`) runs every Monday and on demand. It scans the `latest` image on GHCR the same way and uploads the SARIF report under the category `image-latest`, but it never fails on findings. With the workflow token it opens or updates one issue titled "Image scan: fixable findings in latest" listing the fixable High and Critical findings, and closes the issue when there are none. `.grype.yaml` is the ignore list: one entry per accepted finding, with the vulnerability id, the package and a reason that ends in `(review-by YYYY-MM-DD)`. The weekly run fails its own run once such a date has passed.
- **Dependabot** (`.github/dependabot.yml`) checks GitHub Actions, uv (`uv.lock`), the npm packages of the page suites (`tests/page`) and the Docker base images (`/`, `/examples/hello-plugin`, `/examples/couchbase-plugin` and `/examples/managed-redis-plugin`) weekly. It waits 14 days after a release before proposing it. The Python base image for all four directories comes in one pull request, and the distroless base in its own; a build stops when a library the two share differs, so merge base-image bumps together. It does not update the digest pins in the workflows (the Mongo service, the binfmt image and the Buildx version), the uv image the Dockerfile copies from, or setup-uv's `version`; bump those by hand.

Every Debian library copied into the image has a dpkg status entry under `/var/lib/dpkg/status.d/`, so image scanners and the SBOM list it; Syft lists every one of them. A library whose package the distroless base already ships at the same version is used from the base and not copied, so the base's own entry stays; another version of that package stops the build. The C library, libgcc, libstdc++, OpenSSL and zlib are never copied: each of them must come from a package the distroless base ships, with the same file in that package, or the build stops. A native library that a Python wheel bundles in its own `<name>.libs/` folder needs a reviewed `[bundled."<name>.libs/<file>"]` entry in `tools/licence_overrides.toml`, or the build stops; the reviewed ones are listed in `third-party-bundled.md`, which the image carries only when a wheel bundles one.

Fork pull requests run `ci` and `pr` only. They get no secrets and publish nothing.

Every pull request also runs the `pr` workflow (`pr.yml`), with read-only permissions and no secrets, in two jobs. `title` checks that the pull request title is a Conventional Commit title (`type: summary`, see `CONTRIBUTING.md`). `dco` checks that every commit of the pull request, merge commits aside, carries a `Signed-off-by: Name <email>` line whose email matches the commit's author email; commits by `dependabot[bot]` and `github-actions[bot]` are exempt. A failing `dco` job lists the commits to fix. Run `git commit --amend -s --no-edit` for the last commit or `git rebase --signoff` for several, then push again with `git push --force-with-lease`. Run `python3 tools/dco_check.py <base> <head>` to check a range locally. Editing the title re-runs `title`.

Before the first release tag, the owner creates an environment named `release` in the repository settings, limited to `v*` tags, and adds the two Docker Hub secrets to it as environment secrets of `release`, not repository secrets:

- `DOCKERHUB_USERNAME`: the Docker Hub account that owns `skopax/skopax`.
- `DOCKERHUB_TOKEN`: a Docker Hub access token with write access to `skopax/skopax`.
- GHCR needs no secret. The workflow uses the built-in `GITHUB_TOKEN`, with `packages: write` in `build` and `promote` only.

## The licence gate

The image build refuses a dependency whose licence is not allowed, and prints one line per offender.

- **Python.** A distribution passes when its licence is one of `MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `Apache-2.0`, `ISC`, `PSF-2.0`, `Python-2.0` or `MPL-2.0`, plus `CNRI-Python` for the `regex` distribution only. Anything else fails, including GPL, LGPL, AGPL, SSPL, BUSL and an unknown licence. A bare "BSD License" classifier is named by the licence text the distribution ships: a text with the "Neither the name" clause is `BSD-3-Clause`, one without it is `BSD-2-Clause`, and a distribution with no text is unknown.
- **Debian.** Every Debian library copied into the image needs a reviewed entry for its package.
- **Bundled.** Every native library a Python wheel bundles in a `<name>.libs/` folder needs a reviewed `[bundled."<name>.libs/<file>"]` entry with a licence and a reason. The key is the exact file name, so a library with a new name after an upgrade is reviewed again.
- **Overrides.** Reviewed exceptions live in `tools/licence_overrides.toml`, each with a reason. A Python override pins the exact version, so an upgrade has to be reviewed again. An override can carry a vendored licence text (`text = "<path relative to the overrides file>"`, kept under `tools/licence_texts/`) for a distribution that ships none, so that the image carries the notice the licence asks for. An override that matches nothing is printed and does not fail the build.
- **Output.** The gate writes `third-party-python.md` and `third-party-debian.md`, plus `third-party-bundled.md` when a wheel bundles a native library, which the image carries in `/usr/share/doc/skopax/` (see [The image](operations.md#the-image)).

Run it locally on a synced environment:

    uv sync --frozen --no-dev
    .venv/bin/python -m tools.licences check --site .venv/lib/python3.15/site-packages --out <dir>

The first command leaves out the test tools, as the image does; run `uv sync --frozen` again afterwards to bring them back.

## Releases

The package version lives in `pyproject.toml` and `skopax/__init__.py`, in its PEP 440 form, such as `0.1.0rc1`. The git tag and the image tag use the release form:

| Package version | Git tag | Image tags |
|---|---|---|
| `X.Y.ZrcN` | `vX.Y.Z-rc.N` | `X.Y.Z-rc.N`, on GHCR only |
| `X.Y.Z` | `vX.Y.Z` | `X.Y.Z`, `X.Y` and `latest`, on GHCR and Docker Hub |

`python -m tools.release_version check <tag>` fails when the tag does not match the package version, and `python -m tools.release_version tags <tag>` prints the image tags. The image workflow runs both before it builds.

Bump the version in `pyproject.toml`, `skopax/__init__.py` and the `SKOPAX_VERSION` defaults in `Dockerfile` and in the example Dockerfiles (`examples/hello-plugin/Dockerfile`, `examples/couchbase-plugin/Dockerfile` and `examples/managed-redis-plugin/Dockerfile`) together; a test fails when they differ. The defaults use the tag form, so a local build such as `docker compose up --build` labels the image with the real version. `COMMIT_ID` defaults to `dev`, because the build context leaves out `.git`; pass `--build-arg COMMIT_ID=$(git rev-parse HEAD)` to name the commit.

Tag only a commit on `main` whose CI is green.
