`safebrowsing` asks Google Safe Browsing whether given addresses are listed as unsafe, read only, with the instance's API key.

- `safebrowsing|-/<host>|listed` cards come from the safebrowsing check, every 15 minutes, for each configured host. Critical when Google names a threat (malware, social engineering, unwanted software, harmful apps); a warning when the status says only some pages are unsafe. The card names up to three listed addresses.
- With an API key the check asks the Safe Browsing API once for the root page of every host. Without one it reads Google's public site status page, one host every few seconds, and stops at the first host Google does not answer for; the hosts it did not reach keep their cards until a later pass.
- Chrome shows visitors a red warning page while a host is listed. The Security issues report in Search Console names the cause and is where a review is requested; no tool reads it.
- `pass_table` source `safeBrowsing` holds the last pass: one row per host with its status and flags, and whether it came from the API or the status page.
- The tool answers only for the exact addresses given, their parent paths and their host, so check the page a card or report names and a suspicious referrer host from the logs.
- An `access|-/safe-browsing|hosts` card means the API refused the key: enable the Safe Browsing API for the key's project, or clear the key. Listing cards stay as they were until it reads again.
