# Kepler: governed coding agent

Status: **proposed**. Epic [#125](https://github.com/vchokshi/viharbot/issues/125). This page covers the part implemented by [#126](https://github.com/vchokshi/viharbot/issues/126): Kepler's identity and governance loading. Kepler does not run, has no repository, GitHub or execution access, and nothing in the application executes the `software_engineering` capability. The container, assignment workflow, Sage cycle limit, Archie handoff and final PR gate are separate issues (#127 to #131) and are not implemented.

## Identity

`src/viharbot/agent_identities/kepler.toml` and `prompts/kepler.md` declare Kepler as "The Architect of Harmony", after Johannes Kepler's discovery of mathematical order in planetary motion. The identity names no model alias and no runtime: it is independent of both. Like every identity it grants no permissions; the prompt states the boundaries, but they are enforced in code, not by the prompt.

## Governance loading

`src/viharbot/kepler_governance.py` loads the charter, constitution, architecture, skills registry and change-rationale policy from an assignment's checkout.

- **Fails closed.** A missing, unreadable, empty, oversized, non-UTF-8, unversioned (where a version is expected) or checkout-escaping document raises `GovernanceLoadError` and no bundle is returned, so the assignment cannot start. Errors name the document and the problem, never its contents.
- **Records versions.** Each document's stated version and SHA-256 are recorded (`GovernanceBundle.versions()`) for the audit log and the pull request. The hash is the authoritative identity.
- **Checks citations.** `verify_citations` requires every cited section to exist as a heading in the named document and every citation to carry evidence. `render_governance_followed` produces the mandatory "Governance followed" PR section. This proves the section exists, not that the evidence is true: Sage verifies that independently (#134).
- **Proposes, never approves.** `GovernanceProposal` is always `proposed`; constructing one with any other status raises. `require_editable(path, root)` rejects direct edits under `docs/governance/`, `docs/adr/` and to `AGENTS.md`. Its contract: `path` is checkout-relative and `root` is the trusted checkout root. Absolute, escaping and NUL-containing paths raise (fail closed), symlinks are resolved inside `root` so an innocent-looking alias of a protected file is still protected, and matching is case-insensitive. The workflow that calls it is #128.
- **Bounded reads.** Each document is read with a single read of at most the size limit plus one byte, so an oversized file produces the controlled error instead of exhausting memory.
- **Real headings only.** Headings inside fenced code blocks are not sections and cannot be cited.

## Limitations

The loader is a library only. Nothing calls it yet, the protected-path list is policy in code rather than a GitHub branch or credential restriction, and the website profile is a "proposed" section, not a live team card.
