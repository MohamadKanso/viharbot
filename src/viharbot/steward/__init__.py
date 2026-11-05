"""Kepler, first-contributor steward: the read-only assessment core (issue #198).

This package supersedes nothing in the repository's governance and grants no authority. It reads
what GitHub says about a first-time contributor's pull request, judges it with the repository's
own CLA gate (never a second copy of its rules), and reports what is outstanding. It never writes
to GitHub, never pushes, merges, approves, closes or comments, and makes no model call.

Everything a contributor writes (a pull request body, a comment, a commit message) is untrusted
data. The assessment uses only structured facts and exact-match checks from it, and nothing a
contributor writes is ever copied into an alert or a draft.
"""
