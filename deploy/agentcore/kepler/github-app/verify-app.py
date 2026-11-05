#!/usr/bin/env python3
"""Verify a Kepler GitHub App's real configuration. Prints no key, token or secret.

    python3 deploy/agentcore/kepler/github-app/verify-app.py reader
    python3 deploy/agentcore/kepler/github-app/verify-app.py writer

Reads the app's secret from AWS Secrets Manager with your current login, signs a short-lived JWT
locally, and checks the app against what docs/kepler-github-apps.md says it must be. The only
write it attempts is a deliberately EMPTY issue comment on #198, which GitHub rejects with 422
(authorized but invalid) or 403 (not authorized) and never creates. Exit status is 0 only if every
check passes.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

OWNER = "vchokshi"
REPO = "viharbot"
API = "https://api.github.com"
EXPECTED = {
    "reader": {
        "actions": "read", "checks": "read", "contents": "read", "issues": "read",
        "metadata": "read", "pull_requests": "read", "statuses": "read",
    },
    "writer": {"issues": "write", "metadata": "read"},
}
SECRET_NAME = {
    "reader": "viharbot/kepler/github-app-reader",
    "writer": "viharbot/kepler/github-app-alert-writer",
}
REQUIRED_SECRET_KEYS = ("app_id", "installation_id", "private_key_pem")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: str, pem: str, now: float | None = None) -> str:
    """A GitHub App JWT: RS256, issued 60 s in the past, valid at most 10 minutes."""
    issued = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)}
    signing_input = ".".join(
        b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url(signature)}"


def load_secret(role: str, region: str) -> dict:
    out = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--region", region,
         "--secret-id", SECRET_NAME[role], "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"cannot read secret {SECRET_NAME[role]} (does it exist, are you logged in?)")
    try:
        secret = json.loads(out.stdout)
    except ValueError:
        raise SystemExit("the secret is not valid JSON") from None
    missing = [k for k in REQUIRED_SECRET_KEYS if not secret.get(k)]
    if missing:
        raise SystemExit(f"the secret is missing: {', '.join(missing)}")
    return secret


def call(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | list]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "viharbot-kepler-verify",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SystemExit("network error talking to GitHub") from None


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failed += 1
            print(f"FAIL  {name}" + (f" :: {detail}" if detail else ""))


def run(role: str, region: str) -> int:
    secret = load_secret(role, region)
    expected = EXPECTED[role]
    report = Report()
    try:
        app_jwt = make_jwt(str(secret["app_id"]), secret["private_key_pem"])
    except (ValueError, TypeError):
        raise SystemExit("the private key in the secret could not be loaded") from None

    status, app = call("GET", "/app", app_jwt)
    report.check("app authenticates with its key", status == 200, f"HTTP {status}")
    if status != 200:
        print("RESULT: cannot continue without a valid app identity")
        return 1
    report.check("app is owned by the founder's account", app.get("owner", {}).get("login") == OWNER,
                 str(app.get("owner", {}).get("login")))
    report.check("app permissions are exactly the expected set", app.get("permissions") == expected,
                 f"got {app.get('permissions')}")
    report.check("app subscribes to no webhook events", not app.get("events"), str(app.get("events")))

    install_id = secret["installation_id"]
    status, install = call("GET", f"/app/installations/{install_id}", app_jwt)
    report.check("installation exists", status == 200, f"HTTP {status}")
    if status == 200:
        report.check("installation belongs to the founder's account",
                     install.get("account", {}).get("login") == OWNER)
        report.check("installation is limited to selected repositories",
                     install.get("repository_selection") == "selected",
                     str(install.get("repository_selection")))
        report.check("installation permissions are exactly the expected set",
                     install.get("permissions") == expected, f"got {install.get('permissions')}")
        report.check("installation is not suspended", not install.get("suspended_at"))

    status, issued = call("POST", f"/app/installations/{install_id}/access_tokens", app_jwt,
                          {"repositories": [REPO]})
    report.check("an installation token can be issued for the repository", status == 201,
                 f"HTTP {status}")
    if status != 201:
        print(f"RESULT: {report.passed} passed, {report.failed} failed")
        return 1
    token = issued["token"]
    report.check("the token's permissions are exactly the expected set",
                 issued.get("permissions") == expected, f"got {issued.get('permissions')}")

    status, repos = call("GET", "/installation/repositories", token)
    names = sorted(r.get("name") for r in repos.get("repositories", [])) if status == 200 else []
    report.check("the installation reaches only the viharbot repository", names == [REPO], str(names))

    slug = f"/repos/{OWNER}/{REPO}"
    if role == "reader":
        for label, path in (
            ("read pull request #179", f"{slug}/pulls/179"),
            ("read the CLA record from main", f"{slug}/contents/CLA-ACCEPTANCES.md?ref=main"),
            ("read workflow runs", f"{slug}/actions/runs?per_page=1"),
            ("read check runs", f"{slug}/commits/main/check-runs?per_page=1"),
            ("read commit status", f"{slug}/commits/main/status"),
            ("read issue #198", f"{slug}/issues/198"),
        ):
            status, _ = call("GET", path, token)
            report.check(label, status == 200, f"HTTP {status}")
        status, _ = call("POST", f"{slug}/issues/198/comments", token, {})
        report.check("a write attempt is refused (403)", status == 403, f"HTTP {status}")
    else:
        status, _ = call("POST", f"{slug}/issues/198/comments", token, {})
        report.check("a comment attempt is authorized (empty body gives 422, nothing is posted)",
                     status == 422, f"HTTP {status}")
        status, _ = call("GET", f"{slug}/pulls/179", token)
        report.check("cannot read pull requests", status in (403, 404), f"HTTP {status}")
        status, _ = call("GET", f"{slug}/contents/CLA-ACCEPTANCES.md?ref=main", token)
        report.check("cannot read repository contents", status in (403, 404), f"HTTP {status}")

    print(f"RESULT: {report.passed} passed, {report.failed} failed")
    return 0 if report.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=sorted(EXPECTED))
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    return run(args.role, args.region)


if __name__ == "__main__":
    sys.exit(main())
