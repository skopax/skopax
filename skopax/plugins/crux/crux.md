`crux` reads the Chrome UX Report (CrUX): the page speed that real Chrome users had on the configured origins, read only, with a Google API key.

- `crux|-/<host>|<mobile or desktop>:<inp, lcp or cls>` cards come from the daily crux check. It compares the p75 of the latest 28-day period with the period a week earlier and raises a warning when a metric moved into a worse band (good, needs improvement, poor) or got much worse inside a band that is not good. The share that counts as much worse is the rule `worse`.
- Thresholds: INP good up to 200 ms, poor above 500 ms; LCP good up to 2500 ms, poor above 4000 ms; CLS good up to 0.10, poor above 0.25.
- CrUX is a 28-day rolling window. A change shows days after the release that caused it, and it fades just as slowly, so a fresh deploy can't be cleared or blamed with it alone. Compare the period dates with the release list.
- `pass_table` source `crux` holds the last pass: one row per origin and device with the period end dates and the INP, LCP and CLS values.
- A rarely visited page has no data of its own; ask for its origin instead.
- An `access|-/crux|origins` card means the API refused the key: enable the Chrome UX Report API for the key's project. Page-speed cards stay as they were until it reads again.
