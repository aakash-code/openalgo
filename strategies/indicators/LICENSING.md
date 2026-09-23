# Licensing

Short version, for the two questions people actually have:

- **Using these indicators in your own OpenAlgo: no obligations at all.** None
  of the three licences here is triggered by running software. They are
  triggered by *distributing* it. Install whatever you like.
- **Redistributing them, or shipping something derived from them: read on.**
  408 of the 497 are MIT and easy. The other 89 are copyleft and stay that way.

---

## Why this repository is not single-licensed

Every indicator here is a **port**: a translation of someone else's Pine Script
onto the openalgo-charts descriptor contract. A close translation is a
derivative work, so each port carries the licence of the work it came from.

Two of the upstream projects chose copyleft licences. Neither GPL-3.0 nor
MPL-2.0 permits relicensing to MIT, so those ports are kept under their own
terms rather than relabelled. Doing otherwise would misstate someone else's
licence, which is the one outcome worth engineering against.

| Directory | Files | Licence | SPDX | Upstream |
|---|---:|---|---|---|
| `indicators/MIT/` | 408 | MIT | `MIT` | [mihakralj/pinescript](https://github.com/mihakralj/pinescript) |
| `indicators/GPL-3.0/` | 57 | GNU GPL v3.0 | `GPL-3.0-only` | [everget/tradingview-pinescript-indicators](https://github.com/everget/tradingview-pinescript-indicators) |
| `indicators/MPL-2.0/` | 32 | Mozilla Public License 2.0 | `MPL-2.0` | [ArunKBhaskar](https://github.com/ArunKBhaskar/PineScript), [sukesan7](https://github.com/sukesan7/meridian-indicators), [CedInvest](https://github.com/CedInvest/sm-radar-pine) |

Each upstream licence was verified against the source repository itself, not
only against the header text that travelled with the scripts.

`GPL-3.0-only` rather than `-or-later` is deliberate: everget's scripts say
"may be freely distributed under the terms of the GPL-3.0 license" and grant no
"or any later version" option, so the narrower identifier is the accurate one.

---

## How the repository is arranged so this is checkable

**Every file carries machine-readable SPDX tags** in its header, beside the
human-readable source and licence lines:

```js
/**
 * Acceleration Bands - ported to openalgo-charts from Pine Script v3.
 *
 * Source: https://github.com/everget/.../acceleration_bands.pine
 * Repo:   everget/tradingview-pinescript-indicators
 * License: GPL-3.0, Copyright (c) 2018-present, Alex Orekhov (everget)
 * SPDX-FileCopyrightText: (c) 2018-present Alex Orekhov (everget)
 * SPDX-FileCopyrightText: (c) 2026 marketcalls (port to openalgo-charts)
 * SPDX-License-Identifier: GPL-3.0-only
 */
```

Prose is for a reader; the SPDX tag is for the licence scanner whoever vendors
this will actually run. Both are present, and the file header is authoritative
for that file.

**Each directory is self-contained.** `indicators/GPL-3.0/` holds its own
`LICENSE` and `NOTICE`, and so do the other two. A directory copied out of this
repository takes its licence with it rather than leaving it behind.

**Verify it yourself:**

```bash
# every file tagged, and the tag matching its directory
grep -c "SPDX-License-Identifier: MIT"          indicators/MIT/*.js       | grep -c ':1$'   # 408
grep -c "SPDX-License-Identifier: GPL-3.0-only" indicators/GPL-3.0/*.js   | grep -c ':1$'   # 57
grep -c "SPDX-License-Identifier: MPL-2.0"      indicators/MPL-2.0/*.js   | grep -c ':1$'   # 32
```

---

## What you may do

### Use them (any of them)

Install, run, modify for your own use, on any number of machines, commercially
or not. **No licence here restricts use**, and none of them is triggered until
you distribute. This covers essentially everyone reading this.

### Redistribute the MIT ones (408)

Keep the copyright notice and the permission notice with them. That is the
whole obligation. You may put them in a closed-source product.

### Redistribute the GPL-3.0 ones (57)

They stay GPL-3.0. In practice:

- Keep the notices and the licence text.
- Make the source available to whoever receives them.
- A work you derive from them and then distribute is also GPL-3.0.
- You may **not** put them in a closed-source product.

Note the thing people most often get wrong: **GPL does not forbid commercial
use.** It forbids closed distribution. Selling a GPL-3.0 work is permitted.

### Redistribute the MPL-2.0 ones (32)

MPL-2.0 is file-level copyleft, which is much lighter than GPL:

- These files, and your modifications *to these files*, stay MPL-2.0.
- You may combine them with files under other licences, including proprietary
  ones, without those files becoming MPL.
- Keep the notices and make the source of the MPL files available.

---

## Commercial and embedded use

If you are a business or a website adopting this, three different licences are
in play and only one of them can catch you out. They are worth separating,
because people tend to reason about "OpenAlgo" as one thing.

### 1. The chart engine: openalgo-charts, Apache-2.0

**Embed it in a closed-source product or a paid service. That is fine.**
Apache-2.0 is permissive and is, for a commercial adopter, *stronger* than MIT
in the ways that matter:

- It grants patent rights **expressly** (section 3). MIT is silent on patents,
  which is why corporate legal teams often prefer Apache.
- It terminates the licence of anyone who sues you claiming the software
  infringes their patent (the retaliation clause).
- It disclaims warranty and limits liability in plain terms (sections 7, 8).

Your obligations are light: ship a copy of the licence, keep the contents of
the `NOTICE` file, preserve the copyright and attribution notices, and state
significant changes you made. Nothing requires you to publish your own source.

Note that Apache-2.0 section 6 grants **no trademark rights**. You may build on
the software; you may not imply the OpenAlgo project endorses your product.

### 2. The OpenAlgo platform: AGPL-3.0

This is the one most often missed, and it is the biggest for anyone running a
**website or a hosted service**.

AGPL-3.0 section 13 extends copyleft across the network: if you modify OpenAlgo
and let users interact with it **over a network**, you must offer those users
the complete corresponding source of your modified version. Ordinary GPL would
not require that, because you never "distribute" a binary. AGPL closes exactly
that gap.

Running an unmodified OpenAlgo for your own trading is unaffected. Building a
modified OpenAlgo into a customer-facing service is squarely within it.

### 3. These indicators: it depends on which ones you ship

Using them costs you nothing. **Shipping them inside your product is where the
directories start to matter:**

| If your product includes | Effect on your product |
|---|---|
| `indicators/MIT/` (408) | None beyond keeping the notices. Closed source is fine. |
| `indicators/MPL-2.0/` (32) | Those files stay MPL-2.0 and their source must be available. The rest of your product is **unaffected**: MPL is file-level. |
| `indicators/GPL-3.0/` (57) | **Your whole distributed work becomes GPL-3.0.** You must publish its source. |

So the practical rule for a closed-source product is simple:

```bash
# Safe to embed in a proprietary product:
cp indicators/MIT/*.js      your-product/indicators/     # 408

# Safe too, but these individual files stay MPL-2.0 and their source
# must remain available to your users:
cp indicators/MPL-2.0/*.js  your-product/indicators/     # 32

# Do NOT embed these unless your product is itself GPL-3.0:
#   indicators/GPL-3.0/                                  # 57
```

That is not a restriction this project invented. It is what Alex Orekhov chose
when publishing the originals, and it travels with any port of them.

### The line that decides everything

Every licence here triggers on **distribution**, not on use. A business that
installs indicators into its own OpenAlgo, for its own traders, distributes
nothing and owes nothing, whichever directory it took them from. The table
above only applies when you hand the software to someone else, and AGPL's
section 13 only applies when "handing it over" happens across a network.

**If you are unsure which side of that line you are on, that is the question to
take to a lawyer.** It is usually the only one that needs asking.

## Compatibility with OpenAlgo

OpenAlgo is **AGPL-3.0**. That is convenient rather than awkward:

- **GPL-3.0 indicators** are in the same licence family, so there is no
  conflict in running them inside OpenAlgo.
- **MPL-2.0 indicators** may be distributed under a Secondary Licence
  (GPL/LGPL/AGPL) under MPL-2.0 section 3.3, unless a file is marked
  "Incompatible With Secondary Licenses", which none here is.
- **MIT** is compatible with everything.

One thing to keep in mind for the future: OpenAlgo loads these at runtime from
`strategies/indicators/`, which is gitignored and user-installed. They are user
content, not part of OpenAlgo's distribution. If OpenAlgo ever *bundled* the
GPL-3.0 ones into its own release, that would be a combined-work question worth
thinking about first. As things stand it does not arise.

---

## What this repository does not and cannot warrant

The upstream authors published under the licences recorded above, and those
licences are what this repository relies on. Provenance further upstream is not
something anybody here can audit: if a file was contributed to one of those
projects by someone without the right to license it, that defect travels.

The mitigation is that attribution is complete enough to trace and challenge
any single file. Every port names its exact upstream URL, including the commit,
so you can read the original beside the port.

If you believe a file is misattributed or wrongly licensed, open an issue. It
will be treated as a bug and fixed or removed.

*This is a description of the licences, not legal advice.*
