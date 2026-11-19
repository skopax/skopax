# Third-party notices

Skopax bundles the following third-party material. The Python distributions and Debian libraries in the container image are not listed here: the image build generates those lists, with each licence, and the image ships them as `/usr/share/doc/skopax/third-party-python.md` and `/usr/share/doc/skopax/third-party-debian.md`, with each Debian package's copyright file under `/usr/share/doc/<package>/`. The Python interpreter is CPython 3.15, under the PSF-2.0 licence; the image ships its licence text at `/usr/local/lib/python3.15/LICENSE.txt`. The distroless base keeps the copyright files of its own packages under `/usr/share/doc/` too.

## Icons

`skopax/web/static/icons.js` holds the navigation and feature icons as 24-unit, 1.8-stroke SVG path data. The shapes follow Feather Icons (MIT) and Lucide (ISC). The licence texts below are copied from the upstream LICENSE files (github.com/feathericons/feather and github.com/lucide-icons/lucide).

Provenance per icon, verified on 2026-11-17 against feather-icons 4.29.2 and lucide-static 1.66.0 (published 2026-10-31), the newest Lucide release at least two weeks old on that date. Each icon was parsed into path, line, polyline, polygon, rect and circle primitives, sampled along its outline, and compared with every upstream icon by the largest distance between the two outlines in a 24-unit box. "exact" means distance 0; "near" means rounding or a small rebuild (up to 0.35 units); "adapted" means the shape follows the named upstream icon but differs by more than 0.35 units, as listed; "drawn for Skopax" means no known upstream shape. For timer, key, toggle and coin the closest upstream icon is more than 1.5 units away; it is credited anyway because the shape may derive from it. Adapted icons stay under the licence texts below.

| Icon | Source | Result | Differences |
|---|---|---|---|
| `chat` | feather-icons 4.29.2 `message-square` | exact | - |
| `pulse` | feather-icons 4.29.2 `activity` | exact | - |
| `check` | feather-icons 4.29.2 `check-square` | exact | - |
| `x` | feather-icons 4.29.2 `x` | exact | Lucide `x` is the same shape |
| `out` | feather-icons 4.29.2 `log-out` | exact | - |
| `chevR` | feather-icons 4.29.2 `chevron-right` | exact | path drawn in the opposite direction |
| `chevD` | feather-icons 4.29.2 `chevron-down` | exact | - |
| `tick` | feather-icons 4.29.2 `check` | exact | - |
| `send` | feather-icons 4.29.2 `send` | exact | - |
| `eye` | feather-icons 4.29.2 `eye` | exact | - |
| `phone` | feather-icons 4.29.2 `smartphone` | exact | - |
| `lock` | feather-icons 4.29.2 `lock` | exact | - |
| `plus` | feather-icons 4.29.2 `plus` | exact | - |
| `alert` | feather-icons 4.29.2 `alert-triangle` | exact | - |
| `dots` | feather-icons 4.29.2 `more-horizontal` | exact | - |
| `monitor` | feather-icons 4.29.2 `monitor` | exact | Lucide `monitor` is the same shape |
| `collapse` | lucide-static 1.66.0 `panel-left-close` | exact | chevron drawn in the opposite direction |
| `updown` | lucide-static 1.66.0 `chevrons-up-down` | exact | - |
| `ear` | lucide-static 1.66.0 `ear` | exact | - |
| `inbox` | feather-icons 4.29.2 `inbox` | near | outline coordinates rounded to one decimal (largest gap 0.05) |
| `tag` | feather-icons 4.29.2 `tag` | near | outline coordinates rounded to one decimal (largest gap 0.03) |
| `report` | feather-icons 4.29.2 `file-text` | near | the short line is one segment instead of two (largest gap 0.05) |
| `bell` | feather-icons 4.29.2 `bell` | near | coordinates rounded to one decimal (largest gap 0.05) |
| `wrench` | feather-icons 4.29.2 `tool` | near | coordinates rounded to one decimal (largest gap 0.12) |
| `moon` | feather-icons 4.29.2 `moon` | near | coordinates rounded to one decimal (largest gap 0.06) |
| `gear` | feather-icons 4.29.2 `settings` | near | outer lobes rebuilt from rounded arcs (largest gap 0.33) |
| `sun` | lucide-static 1.66.0 `sun` | near | coordinates rounded to one decimal (largest gap 0.06); Feather `sun` is a larger variant |
| `board` | lucide-static 1.66.0 `layout-grid` | near | corner radius 1.5 instead of 1 (largest gap 0.24) |
| `search` | feather-icons 4.29.2 `search` | adapted | lens radius 7 instead of 8, handle ends at 16.7 (largest gap 1.0) |
| `menu` | feather-icons 4.29.2 `menu` | adapted | the three bars run from 4 to 20 instead of 3 to 21 (largest gap 1.0) |
| `shield` | lucide-static 1.66.0 `shield-check` | adapted | simplified outline with a pointed base and square top corners; the check mark is the same (largest gap 0.96) |
| `server` | feather-icons 4.29.2 `server` | adapted | rectangles are 18 by 7 inset one unit instead of 20 by 8, dots at x=7 instead of 6 (largest gap 1.41) |
| `plug` | lucide-static 1.66.0 `plug` | adapted | body is a half disc reaching y=18 instead of a rounded block reaching y=17 (largest gap 1.02) |
| `history` | lucide-static 1.66.0 `history` | adapted | one 9-unit arc instead of two, and the hand ends at 15,14 instead of 16,14 (largest gap 1.02) |
| `expand` | lucide-static 1.66.0 `panel-left-open` | adapted | chevron sits one unit to the left (largest gap 1.0) |
| `badge` | lucide-static 1.66.0 `badge-check` | adapted | outline is identical; the check mark runs 9,12 to 11,14 to 15,10 instead of 8,12 to 10.5,14.5 to 16,9 (largest gap 1.41) |
| `book` | lucide-static 1.66.0 `book` | adapted | cover and spine drawn as two paths without the rounded right corners (largest gap 0.45) |
| `spark` | none | drawn for Skopax | no known upstream shape |
| `hex` | none | drawn for Skopax | no known upstream shape |
| `timer` | lucide-static 1.66.0 `timer` | adapted | closest upstream shapes are Lucide `timer` (3.3 units) and Feather `watch` (2.4 units); circle at y=13 radius 8, hand 12,9 to 12,13 to 14.5,15, top bar 9.5 to 14.5 |
| `key` | feather-icons 4.29.2 `key` | adapted | closest upstream shape is Feather `key` (2.0 units); simplified to a circle with one diagonal and two teeth |
| `toggle` | lucide-static 1.66.0 `toggle-right` | adapted | closest upstream shape is `toggle-right` in both packs (2.1 units); pill is 20 by 10 instead of 20 by 14 and the knob sits at x=16 |
| `coin` | lucide-static 1.66.0 `circle-dollar-sign` | adapted | closest upstream shape is Lucide `circle-dollar-sign` (1.7 units); circle radius 9 instead of 10, S-curve with two ticks |

### Feather Icons

The MIT License (MIT)

Copyright (c) 2013-2023 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### Lucide

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

---

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


## Plug-in marks

Each plug-in's `icon.svg` is a two-letter monogram drawn for Skopax; no third-party source.

## Sign-in button marks

The single sign-on button on the login page shows one of the files in `skopax/web/icons/sso/`.

| File | Source | Licence and terms |
|---|---|---|
| `microsoft.svg` | The Microsoft logo (`ms-symbollockup_mssymbol_19.svg`), unchanged, from "Sign in with Microsoft branding guidelines" (learn.microsoft.com/en-us/entra/identity-platform/howto-add-branding-in-apps), downloaded 2026-11-19 | Microsoft trademark. Used under those guidelines: unaltered, only with the text "Sign in with Microsoft" and the light (#FFFFFF, #8C8C8C border, #5E5E5E text) or dark (#2F2F2F, white text) button the guideline assets use. With any other button text Skopax shows `key.svg` instead. |
| `google.svg`, `okta.svg`, `auth0.svg`, `onelogin.svg`, `jumpcloud.svg`, `zitadel.svg`, `authentik.svg`, `gitlab.svg`, `keycloak.svg` | One-letter tiles drawn for Skopax; no third-party artwork | Same licence as Skopax. They are not the providers' logos. Google's guidelines (developers.google.com/identity/branding-guidelines) allow the Google "G" only inside a button with Google's wording and colours, and other uses need Google's written consent. Its sign-in bundle (signin-assets.zip, checked 2026-11-19: 133 files) holds only whole buttons, PNG at 1x to 4x and SVG, Light, Dark and Neutral, with or without text, pill or square, for Android + Web and iOS, and no standalone G; the SVGs are Figma exports with foreignObject. So Skopax ships a letter tile, not the Google mark. The other providers' brand rules for sign-in buttons were not checked, so they get letter tiles too. |
| `key.svg` | A key tile drawn for Skopax | Same licence as Skopax. |

Names of identity providers are trademarks of their owners and are used only to describe which provider an issuer belongs to.

## Fonts

None bundled. The page, card page, login, setup and chat use the system font stacks.

## Python distributions and Debian libraries

Listed in the container image under `/usr/share/doc/skopax/`, as the first paragraph says.
