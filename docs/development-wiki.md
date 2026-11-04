# Development Wiki

A read-only view of how development is going, inside the ViharBot web app (**Development Wiki** in the top bar, `#wiki`). GitHub stays the source of truth: the wiki reads epics, issues, pull requests, commits, milestones, releases and the repository's own Markdown, and keeps no state of its own. Tracking issue: #166.

**Status: implemented and tested with a faked GitHub; not deployed; not yet checked in a real browser.**

## What it shows

| View | Route | Contents |
|---|---|---|
| Dashboard | `#wiki` | epic counts by status, overall and per-epic progress, recently merged PRs, open blockers, upcoming milestones, last sync time |
| Epic page | `#wiki/epic/N` | objective, status with evidence links, owner or agent where documented, child issues, PRs and commits, dependencies and blockers, the epic's own checklist, maturity, related docs and ADRs, recent activity, derived next actions |
| Architecture and docs | `#wiki/docs`, `#wiki/docs/<path>` | the repository's Markdown, grouped into the nine required categories (plus "Other documentation" so nothing is hidden), with source and history links and the revision shown |
| Timeline | `#wiki/timeline?epic=&agent=&phase=&status=` | epic and issue open/close, merged PRs, closed milestones, published releases, filterable |

## Rules that keep it honest

* **Epics are discovered**, not listed: an issue labelled `epic`, or titled `[Epic]` / `Epic: ...`.
* **Children** are issues an epic names as its own: a checklist item, a `Child` line, a bullet that starts with the reference, a GitHub sub-issue, or `Parent: #N` in the child. Other mentions are *related* or *dependencies* and never count toward progress. References that match no issue are shown as excluded.
* **Progress** is `closed children ÷ linked children`, with the method shown beside it. With no linked children it is **unknown**, not 0%.
* **Status** is `completed` (epic closed), `blocked` (a `blocked` label, or "Blocked by #N" with N open), `active` (a closed child or a linked PR), else `planned`. Each status carries links to the issues or PRs that justify it.
* **Maturity** uses the vocabulary in [development status](development-status.md). From GitHub alone only *proposed* (the epic exists) and *implemented* (a merged PR references it) can be evidenced. *Tested, integrated, deployed* and *operationally validated* are shown as "not evidenced here". A closed issue or merged PR does not prove deployment. Deployment records are deliberately not shown.
* PRs and commits attach to an epic only through an explicit `#N` reference to the epic or one of its children.

## Configuration

| Variable | Meaning |
|---|---|
| `DEV_WIKI_GITHUB_TOKEN` | Read-only token. Without it the wiki shows an *unavailable* state and makes no GitHub call. Use a fine-grained token for this one repository with read access to Contents, Issues, Pull requests and Metadata. |
| `DEV_WIKI_REPO` | `owner/name`, default `vchokshi/viharbot` |
| `DEV_WIKI_API_URL` | default `https://api.github.com`; http is accepted only for a literal loopback or private address |
| `DEV_WIKI_BRANCH` | default `main` |
| `DEV_WIKI_CACHE_SECONDS` | default 300 (minimum 30) |

Local Compose passes `DEV_WIKI_GITHUB_TOKEN` through (empty by default). `deploy/digitalocean/app.yaml` is unchanged, so a Stage built from it shows the wiki as unavailable until a secret is added by a deliberate, reviewed deployment change.

## API (all behind the existing web authentication)

`GET /api/development/epics`, `/epics/{number}`, `/timeline`, `/docs`, `/docs/{path}`. They are `GET`-only, send `Cache-Control: no-store`, and never return the token. Every response carries a `sync` object: `state` (`ok`, `stale`, `unavailable`), `synced_at`, `error` (a short code such as `rate_limited`), `head_sha`, `rate_limit`, and `retry_after_seconds`. The list response also reports the raw counts it was built from (issues, pull requests, epics, requests made, whether anything was truncated) so they can be compared with GitHub.

## Behaviour under failure

One snapshot is built per cache window (about 16 GitHub requests for this repository) and shared by every view. If a refresh fails the last good snapshot is served marked *stale* with its original timestamp, and the server backs off (30 seconds, or GitHub's `Retry-After` / rate-limit reset) instead of retrying on every page view. With no snapshot at all the API answers *unavailable* and the page says so. Lists are capped at 20 pages of 100 and reported as `truncated` if the cap is hit.

## Security

* The token is read server-side, sent only in an `Authorization` header to the configured API host, never in a URL, response or log. Pagination links to any other host are ignored.
* The wiki only reads. It has no write call to GitHub, and the browser can only `GET` it.
* Documents come only from paths GitHub's own tree listing returned, so a request cannot make the server fetch an arbitrary file.
* Markdown is converted on the server to a structured block list, not HTML; the page builds DOM text nodes from it, as for chat output. Raw HTML in a document is shown literally. Only `https` links are kept; images are shown as a link to their source and never loaded.
* No model is called.

## Limits

* Authentication is the app's existing shared Basic Auth, not per-user.
* The wiki is not a project tracker: it cannot change an issue, a label or a milestone.
* The classification of "child" depends on how epics are written. An epic that mentions its children in free prose will show them as related, and progress will read unknown or low until they are linked as checklist items or sub-issues.
* Commits are limited to the latest 100 on the branch.
* Not yet checked in a real browser; the page script is exercised only against the repository's Node fake DOM.
