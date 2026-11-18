# Skopax Trademark Policy

Version 1.0, 2026-11-17

Skopax is open source under the Apache License, Version 2.0. That licence covers the code. It does not give permission to use the Skopax name or logo (Apache-2.0, section 6). This policy says how you may use them.

## The short version

- You may say your software is **based on Skopax**, works with it or is a plug-in **for Skopax**.
- You may redistribute **unmodified** Skopax under its own name and logo.
- You may keep, or remove, the **"powered by Skopax"** line at the bottom of each page.
- You may write, teach and talk about Skopax.
- A **modified** copy (a fork) must use **its own name and no Skopax logo**.
- Names like **"Skopax Cloud for X"**, the logo on your product or service, and domain names with "Skopax" in them need our **written permission**.

If you are unsure, ask at hello@skopax.io.

## 1. Why we have this policy

A trademark tells people where something comes from. Because the open-source licence lets anyone change the code, we cannot vouch for a changed copy that uses our name. This policy keeps the name meaningful for users, while making it easy for the community to talk about, build on and support Skopax.

We are not trying to limit lawful use of the name. We describe what we consider lawful, so that you can tell whether your use is fine without asking. Any use that this policy does not cover and that we have not approved in writing is not approved by us; you decide for yourself whether it is lawful.

## 2. The marks this policy covers

| Mark | What it is | Used for |
|---|---|---|
| **Skopax** | the word mark | software for watching the health of production systems, and related services |
| **Beam X** | the logo: two crossing searchlight beams | the same |
| **The Skopax wordmark** | "skopa" in drawn letters, with the Beam X as the final x | the same |

The marks belong to Maksut Spahi (the "Owner"). They may later be transferred to a company or foundation that holds the Skopax project.

This policy reserves the Skopax name, the Beam X logo and the Skopax wordmark for the Owner. The following names are reserved for the Owner too and are **not** licensed by this policy: **Skopax Cloud**, **Skopax Enterprise**, and any other "Skopax" plus a word used as the name of a product or service.

## 3. The basic rule

Whenever you use a mark, nobody should be misled, directly or by leaving something out, about what they are getting and from whom. In particular:

- Do not use the marks if you are not complying with the Apache-2.0 licence for the software.
- Do not use or register the marks, in whole or in part, as part of your own trademark, company name, product or service name, domain name or social media account name.
- Do not use names that are too close to ours for similar products or services, for example "Skopex", "Scopax" or another name that starts with "Skopa" or sounds like Skopax.
- You do not gain any rights in the marks by using them. Any goodwill from your use belongs to the Owner.

Using "skopax" functionally is not a trademark use and is always fine: for example in the `skopax` Python package, the `SKOPAX_*` settings, file names, paths, or a host name you use to reach your own installation. The same goes for a plug-in package named `skopax-<name>` or `skopax_<name>`, as the example plug-in `skopax-hello` is, when its description says it is "for Skopax", does not use the logo and does not claim to be official.

## 4. Uses that do not need permission

**Unmodified redistribution.** You may redistribute an official Skopax release (source code or the official container image) that you have not changed, under the Skopax name and with the logo. Building the official source unchanged, and changing only packaging or configuration, still counts as unmodified.

**Saying what your software is based on.** You may use the word "Skopax" (not the logo) to describe truthfully how your software relates to ours. Put "Skopax" after a word that describes the relation:

- "Acme Board, based on Skopax"
- "Acme Board is derived from the Skopax source code"
- "works with Skopax", "compatible with Skopax", "runs on Skopax", "powered by Skopax"
- "a Slack plug-in for Skopax" (not "Skopax Slack plug-in")
- "managed hosting for Skopax" as a description, as long as the service itself has its own name

**The "powered by Skopax" line.** You may keep it, in unmodified and modified copies alike, as long as it stays truthful. See section 7.

**Articles, books, talks and courses.** You may use the word mark in titles and text, and the logo in illustrations, as long as you do not suggest that we wrote, published or endorse your work.

**Showing support.** You may use the word mark and the logo on your website to show that you use or support Skopax, if your own branding is more prominent, the logo links to https://skopax.io, and the page does not suggest it is ours or that we endorse you.

**Your own installation.** You may run Skopax, changed or not, inside your organization and call your instance what you like. The display name setting exists for this.

## 5. Uses that need written permission

Ask at hello@skopax.io before you:

- use a name that contains "Skopax" for a product, service, company or project, for example "Skopax Cloud for X", "Skopax Hosting", "Acme Skopax" or "SkopaxOps" (other than a plug-in, and its repository, named as in section 3);
- distribute a modified build under the Skopax name, or describe it as Skopax rather than "based on Skopax";
- use the logo or the wordmark for a product or service, as an app icon, or as part of your own branding;
- register or use a domain name, subdomain offered to others, social media handle, app store name or package name that contains "Skopax" (other than a plug-in package named as in section 3);
- sell goods (such as shirts or stickers) with the marks on them;
- name a user group or event after Skopax.

Permission, when given, is written and specific to the use. If you have it, add this line where you use the mark: "Skopax is a trademark of Maksut Spahi, used with permission."

## 6. Forks

You are welcome to fork Skopax. Apache-2.0 lets you change and share the code. If you distribute a fork, or offer it as a service:

1. **Rename it.** Choose a name that is not "Skopax" and not confusingly similar. Set your name as the display name.
2. **Remove the logo.** Replace the Beam X mark and the wordmark, including the favicon, app icons and the files in `skopax/web/brand/`, with your own.
3. **Say where it comes from, if you like.** "Acme Board, based on Skopax" is fine (section 4).
4. **Keep `LICENSE` and `NOTICE`** in copies you ship. Reproducing `NOTICE`, which names Skopax, is always allowed and is not a trademark use (Apache-2.0, section 6).

The functional names (the package, the settings and the paths) may stay as they are.

## 7. The attribution line, honestly

**What it is.** The board pages, the card page, the login and setup forms and the Chat page end with a small line that links to https://skopax.io. By default it reads "Skopax <version>", a middle dot, then "skopax.io", all on one line. When a display name is set, it reads "<display name>", a middle dot, then "powered by Skopax <version>". Slack and Teams messages carry no line unless the chat attribution setting is on; then the messages Skopax starts there end with the same line.

**What the licence requires.** Nothing about this line. Apache-2.0 cannot make anyone keep it:

- you may remove it from your copy;
- running Skopax inside your organization, or as a hosted service for others, carries no attribution duty;
- only shipping copies (source code or images) requires anything, and that is to include `LICENSE` and `NOTICE`.

**What we ask.** If you are happy to, keep the line. It is how people find the project, and it costs you nothing.

**What the trademark adds.** If the line stays, it must be true. A modified copy may keep "powered by Skopax", because it is, but it must not present itself as Skopax or use the logo as its own brand. We also ask that a modified build marks its version, for example "1.4.0+acme", so the line does not suggest an official release.

**The paid switch.** A ready-made, supported switch that hides the line is planned as an Enterprise feature; until then it stays locked. You would pay for the convenience and the support, not to unlock anything: you are always free to edit the line out yourself.

## 8. Logo files and how to use them

The logo files are in the source repository under `skopax/web/brand/`: `mark.svg` (the Beam X mark), `wordmark-on-dark.svg` and `wordmark-on-light.svg`.

- Do not change the logo except to scale it or, where only one colour is possible, to set it in one flat colour: no new colours, distortion, added elements, effects or combinations with other logos.
- Leave clear space around the logo, at least the width of one beam.
- Use the on-dark version on dark backgrounds and the on-light version on light ones.
- The colours may still change before 0.1.0.

The logo files are licensed under Apache-2.0 like the code, and `NOTICE` carries no exception for them. That licence covers the copyright in the files, not the right to use them as a trademark: the Skopax name and logo stay reserved under this policy.

## 9. Writing the name

- Write "Skopax" with a capital S, as one word, without hyphens or other words joined to it.
- Use it as an adjective or a name, not as a verb, plural or possessive: "two Skopax installations", not "two Skopaxes".
- Do not translate it.
- On the first or most prominent mention in a document about Skopax, you may add the TM symbol.

## 10. Questions and misuse

If you have a question about this policy, want permission for a use it does not cover, or see the marks used in a misleading way, write to hello@skopax.io.

We may update this policy; the version and date at the top show which one applies.

## Credit

Derived from Model Trademark Guidelines, available at www.modeltrademarkguidelines.org.

- Source: Model Trademark Guidelines, Version 1.0, http://modeltrademarkguidelines.org/index.php/Model_Trademark_Guidelines (the site's HTTPS address did not serve a valid certificate on 2026-11-17).
- Licence of the source: Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/), as stated on every page of the site. The Version 1.0 page's own credit line names Creative Commons Attribution 3.0 Unported (https://creativecommons.org/licenses/by/3.0/).
- Changes: shortened and reorganized; filled in for Skopax; sections 6 (forks) and 7 (attribution line), the plug-in and hosting examples and the functional-use note are new; the hardware, software-bundling and promotional-goods sections were left out.
- Licence of this policy: Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/). You may reuse and adapt it with credit to this credit section and to the Model Trademark Guidelines.
