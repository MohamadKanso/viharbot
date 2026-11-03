"""Institutional memory for the education agents (Phase 3): a governed knowledge-sharing interface.

Archie never reads another agent's private memory. Agents expose approved entries through a
`MemoryProvider`; entries enter a separate institutional store as `proposed`, become `validated`
only through a named human approver, and are served only under an explicit grant (default deny).
Offline: no model, no network, no endpoint, no execution binding, not imported by the web app.
"""
