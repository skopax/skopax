# Hello, an example Skopax plug-in

Hello asks one address on a schedule and opens a card when the address does not answer HTTP 200. When the address answers 200 again, the card clears. It is small on purpose: each file shows one part of the plug-in contract, so you can read it in one sitting and copy it as the start of your own plug-in.

## What is in it

| File | What it shows |
| --- | --- |
| `pyproject.toml` | The package, and the `skopax.plugins` entry point named `hello` that points at `skopax_hello:PLUGIN`. Skopax finds installed plug-ins through this entry point. |
| `skopax_hello/__init__.py` | The `Plugin` itself: one `url` field, a Test connection, one check and one page. |
| `skopax_hello/panel.js` | The script of the Hello page in the sidebar's More group. |
| `skopax_hello/icon.svg` | The icon on the Integrations tile and in the sidebar. |
| `tests/test_hello.py` | Unit tests that run the check and the Test connection with a fake transport. |
| `Dockerfile` | A layer image: the Skopax image with this plug-in added. |
| `LICENSE` | The Apache License 2.0 text, which the wheel carries (`license-files`). |

### The plug-in

- **Imports.** A plug-in imports only the public surface: `skopax.contract`, `skopax.kit`, `skopax.remote` and a few more. It never opens its own sockets.
- **Field.** `url` is a required field of kind `url`. Each instance of the plug-in, added in Connect > Integrations, has its own value. The code reads it as `ctx.instance.config['url']`.
- **Capabilities.** `hosts=('@url',)` declares that the plug-in talks only to the host of its `url` field.
- **Network.** `ctx.http()` gives the transport Skopax manages for this instance, with its proxy, timeouts and size limits. `call_page` reads the address as a page and returns its HTTP status, so a 404 or a 503 comes back as a status rather than an error, and redirects are not followed. A plug-in does not open its own network connections.
- **Test connection.** `test` runs `problem` inside `kit.connection.result`. That times the call and turns an error into a short text, which the Test connection button shows.
- **Check.** `run` runs every 300 seconds for each instance. When the address does not answer 200, or does not answer at all, `run` returns one card, built with `anomaly`. The card key is `hello|<host>`. Card keys are shared by all plug-ins, so a plug-in starts its keys with its own id, and a copy of Hello that keeps a key like `down|<host>` would fight another plug-in over the same card. The texts show the address without its query, so a token in the query never reaches a card, a page or Slack. The pass names `hello` in its `ran` keys, so a later pass that finds no problem clears the card. The check also writes one row to its `hello` section, which the page reads.
- **Page.** `Panel('hello', 'panel.js', label='Hello', group='more')` adds a Hello page to the More group of the sidebar. Skopax serves the script from the package directory, and the script listens for the `skopax:state` and `skopax:page` events.

## Run the tests

From the root of the Skopax repository:

```sh
.venv/bin/python -m pytest -q -n 0 examples/hello-plugin/tests
```

## Build the layer image

The Skopax image is distroless, so it has no `pip`. The layer image installs the plug-in in a Python build stage and copies it into the Skopax image's `site-packages`:

```sh
docker build --build-arg SKOPAX_VERSION=0.5.13 -t skopax-hello examples/hello-plugin
```

Pin `SKOPAX_VERSION` to the Skopax release you run. The plug-in's `core` range (`>=0.1,<0.6`) says which Skopax versions it works with. Start the image like the plain Skopax image. Hello is then listed in Connect > Integrations, and `GET /api/admin/plugins` lists it with the id `hello`.

## Licence

Apache-2.0, like Skopax (see `LICENSE`). Copyright 2026 Maksut Spahi.
