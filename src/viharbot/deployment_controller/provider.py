"""Provider interface and an in-memory fake (no network, no DigitalOcean client).

Response shapes here are invented for tests. They are not DigitalOcean's. A real provider is a
later change that waits for a separately approved live test and contract fixtures (ADR-007).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Protocol


class ProviderError(Exception):
    """The provider definitively refused or failed the call."""


class ProviderTimeout(ProviderError):
    """Timeout or transport error. The outcome is UNKNOWN, never a failure."""


class VersionConflict(ProviderError):
    """A conditional update was rejected because the current version differs."""


@dataclass
class ProviderApp:
    app_id: str
    team_id: str
    name: str
    version: int
    spec: dict

    @property
    def env(self) -> dict[str, str]:
        return self.spec.get("env", {})


class Provider(Protocol):
    team_id: str
    # True only if the provider enforces expected_version itself; the controller then passes the
    # just-read version on every update. If False it writes unconditionally under the store lock and
    # verifies by read-back: a foreign version is failed_uncertain plus an alert (ADR-007).
    conditional_update: bool

    def create_app(self, name: str, spec: dict) -> ProviderApp: ...
    def get_app(self, app_id: str) -> ProviderApp | None: ...
    def read_logs(self, app_id: str, lines: int) -> str: ...
    def update_app(
        self, app_id: str, spec: dict, expected_version: int | None = None
    ) -> ProviderApp: ...
    def destroy_app(self, app_id: str) -> None: ...
    def list_apps(self) -> list[ProviderApp]: ...


class FakeProvider:
    """Deterministic in-memory provider with fault injection.

    ``fail_next(op, "before" | "after")`` raises ProviderTimeout once for the next ``op``: "before"
    without applying it, "after" after applying it (a lost response). This holds for every
    operation. ``on_call(op)``, if set, runs at the start of every call (a slow-call hook).
    """

    def __init__(
        self, team_id: str = "team-1", *, conditional_update: bool = True
    ) -> None:
        self.team_id = team_id
        self.conditional_update = conditional_update
        self.apps: dict[str, ProviderApp] = {}
        self.calls: list[str] = []
        self.logs: dict[str, str] = {}
        self.before_update: Callable[[FakeProvider, str], None] | None = None
        self.on_call: Callable[[str], None] | None = None
        self._faults: dict[str, str] = {}
        self._counter = 0

    def fail_next(self, op: str, when: str) -> None:
        self._faults[op] = when

    def _enter(self, op: str) -> str | None:
        self.calls.append(op)
        if self.on_call:
            self.on_call(op)
        fault = self._faults.pop(op, None)
        if fault == "before":
            raise ProviderTimeout(op)
        return fault

    def _new(self, name: str, spec: dict, team_id: str | None = None) -> ProviderApp:
        self._counter += 1
        app = ProviderApp(
            f"app-{self._counter:04d}", team_id or self.team_id, name, 1, copy.deepcopy(spec)
        )
        self.apps[app.app_id] = app
        return app

    def create_app(self, name: str, spec: dict) -> ProviderApp:
        fault = self._enter("create")
        if any(app.name == name for app in self.apps.values()):
            raise ProviderError("name_conflict")
        app = self._new(name, spec)
        if fault == "after":
            raise ProviderTimeout("create")
        return copy.deepcopy(app)

    def get_app(self, app_id: str) -> ProviderApp | None:
        fault = self._enter("get")
        app = copy.deepcopy(self.apps.get(app_id))
        if fault == "after":
            raise ProviderTimeout("get")
        return app

    def read_logs(self, app_id: str, lines: int) -> str:
        fault = self._enter("logs")
        if fault == "after":
            raise ProviderTimeout("logs")
        return "\n".join(self.logs.get(app_id, "").splitlines()[-lines:])

    def update_app(
        self, app_id: str, spec: dict, expected_version: int | None = None
    ) -> ProviderApp:
        fault = self._enter("update")
        app = self.apps.get(app_id)
        if app is None:
            raise ProviderError("not_found")
        if self.before_update:
            self.before_update(self, app_id)
        if expected_version is not None and app.version != expected_version:
            raise VersionConflict(app_id)
        app.spec = copy.deepcopy(spec)
        app.version += 1
        if fault == "after":
            raise ProviderTimeout("update")
        return copy.deepcopy(app)

    def destroy_app(self, app_id: str) -> None:
        fault = self._enter("destroy")
        if self.apps.pop(app_id, None) is None:
            raise ProviderError("not_found")
        if fault == "after":
            raise ProviderTimeout("destroy")

    def list_apps(self) -> list[ProviderApp]:
        fault = self._enter("list")
        apps = [copy.deepcopy(app) for app in self.apps.values()]
        if fault == "after":
            raise ProviderTimeout("list")
        return apps

    # --- test helpers: situations the controller must survive ---
    def add_foreign(
        self, name: str, env: dict[str, str] | None = None, team_id: str | None = None
    ) -> ProviderApp:
        """An app the controller did not create (no markers by default)."""
        return self._new(name, {"env": dict(env or {})}, team_id)

    def copy_markers(self, source_id: str, target_id: str, keys: tuple[str, ...]) -> None:
        for key in keys:
            self.apps[target_id].spec["env"][key] = self.apps[source_id].spec["env"][key]

    def bump_version(self, app_id: str) -> None:
        self.apps[app_id].version += 1
