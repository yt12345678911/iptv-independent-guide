# Independent TV guide (TEST preview)

A small, self-contained XMLTV guide builder for 11 UK and Danish channels. It reads two
public XMLTV feeds (UK1 and DK1), keeps only the mapped channels, validates the schedule and
writes a slim `guide.xml` plus a `status.json` build report.

**This is a TEST preview, not a production guide.** Three channel mappings are unverified,
and every generated guide says so in an XML comment:

```
<!-- TEST - unverified mapping: bbc1.uk, itv1anglia.uk, TV2Lorry.dk -->
```

The repository holds no playlist, no stream addresses, no credentials and no secrets. It needs
none: the only inputs are the two public feeds and `mapping.json`.

## Channels

Guide channel ids (the ids a player matches on), in order:

| Id | Channel | Mapping |
|---|---|---|
| `bbc1.uk` | BBC One | **provisional** (London schedule chosen as candidate region) |
| `bbc2.uk` | BBC Two | verified |
| `itv1anglia.uk` | ITV1 | **provisional** (generic ITV1 schedule; stream may be ITV Anglia) |
| `channel4.uk` | Channel 4 | verified |
| `DR1.dk` | DR1 | verified |
| `DR2.dk` | DR2 | verified |
| `TV2.dk` | TV 2 | verified |
| `TV2Syd.dk` | TV 2 / TV SYD | verified |
| `TV2Fri.dk` | TV 2 Fri | verified |
| `TV2Lorry.dk` | TV 2 Lorry / Kosmopol | **provisional** (renamed to Kosmopol; stream identity unconfirmed) |
| `TV2Fyn.dk` | TV 2 Fyn | verified |

Regional variants can show different programmes in some slots (regional news, local shows).
Until a provisional mapping has been checked against the actual channel, its schedule may be
wrong in those slots.

## Files

| Path | Purpose |
|---|---|
| `build_guide.py` | Python 3 standard-library builder: allowlisted downloads only, no redirects, size limits, no DTD/entities, 2h past to 48h ahead, strict validation, atomic writes. |
| `mapping.json` | Explicit mapping from guide channel id to feed channel id, each `verified` or `provisional`. |
| `tests/` | Unit tests with synthetic data only (no network): `python -m unittest discover -s tests -v`. |
| `pages/_headers` | Static-hosting headers for the published files. |
| `.github/workflows/publish-test.yml` | Scheduled TEST preview build. |
| `site/` | Generated output (`guide.xml`, `status.json`, `_headers`), committed by the workflow. |

## Modes

```
python build_guide.py --test-preview --output site/guide.xml --report site/status.json
```

- **`--test-preview`** allows the provisional mappings and marks the output: the XML comment
  above, a TEST `generator-info-name`, and `"mode": "test-preview"`, `"production": false` in
  `status.json`. In CI it runs only on this repository's `test` branch and refuses any other
  repository or ref. Run locally it needs the explicit flag.
- **Production** (no mode flag) refuses to build while any mapping is provisional.
- **`--local-preview`** is for local experiments only and refuses to run in CI.

Every mode applies the same checks: each channel must have a programme airing now, a next
programme and at least 24 hours ahead, with titles, no duplicates, no large gaps or overlaps
and timezone-qualified times. On any failure nothing is written, the previous output is left
byte-for-byte unchanged and the exit code is non-zero.

## Scheduled preview

The workflow runs every 6 hours (at 23 minutes past) and on manual dispatch, only on the
default branch `test`. It uses a standard `ubuntu-latest` GitHub-hosted runner, which is free
for public repositories; there are no paid runners, secrets, artifacts or caches, and the only
action used is a SHA-pinned `actions/checkout`. It runs the unit tests, builds the TEST
preview, audits the output, and only then commits `site/guide.xml`, `site/status.json` and
`site/_headers` back to `test`. It never writes any other branch. A static host may deploy
`site/` from `test` as a preview.

The generated preview is public and contains only TV schedule data.

## Before anything becomes production

Each provisional mapping must be checked by watching the actual channel during a slot where
the regional variants differ, and comparing it with the preview. Only then may its `status`
become `verified`, with the evidence recorded in `mapping.json`. A successful build or valid
XML is not regional verification. Never point an entry at another region just to make a build
pass.
