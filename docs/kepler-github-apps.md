# Kepler GitHub Apps (reader and alert writer)

Status: **design and tooling only; the apps are not registered yet.** Tracking issue [#198](https://github.com/vchokshi/viharbot/issues/198). Part of [Kepler: first-contributor steward on AWS AgentCore](kepler-steward-aws.md). Nothing here runs an agent, and no private key ever appears in the repository, in chat, or in logs.

Kepler authenticates to GitHub as **two separate GitHub Apps**, so the monitor that reads untrusted contributor content holds no write permission at all, and the only credential that can write cannot read pull requests.

## The two apps

| | `viharbot-kepler-reader` | `viharbot-kepler-alert-writer` |
| --- | --- | --- |
| Used by | The AgentCore runtime (the monitor) | The alert-writer Lambda only |
| Purpose | Read pull requests, issues, CI and the CLA record | Post one deduplicated tracking comment on issue #198 |
| Repository permissions | Metadata: read. Pull requests: read. Issues: read. Contents: read. Checks: read. Commit statuses: read. Actions: read | Metadata: read. Issues: **read and write** |
| Account and organization permissions | none | none |
| Webhook | **Off.** Polling only in the MVP | **Off** |
| Subscribed events | none | none |
| Installable on | Only this account | Only this account |
| Installed on | `vchokshi/viharbot` only (selected repositories) | `vchokshi/viharbot` only (selected repositories) |
| AWS secret | `viharbot/kepler/github-app-reader` | `viharbot/kepler/github-app-alert-writer` |

Why each reader permission is needed: **Pull requests** and **Issues** for the PR, its conversation and the linked issue; **Contents** to read `CLA-ACCEPTANCES.md` and the governance files from the trusted base branch, never from a contributor's branch; **Checks**, **Commit statuses** and **Actions** because a fork's workflows can sit at `action_required` awaiting maintainer approval, which only shows in workflow runs; **Metadata** is mandatory.

The repository is public, so what the reader can read is not secret. The reason for read-only scope is that its input is untrusted, not that its output is sensitive.

## What these permissions do not give, and where the limits are

- The reader cannot comment, label, close, merge, approve, push, change settings or run workflows. A write attempt returns 403.
- **GitHub has no "comment only" permission.** The writer's `Issues: write` also allows creating, editing, labelling and closing issues, and commenting on pull requests as issues. The restriction to "one tracking comment on #198" is enforced by the alert-writer's own code (a fixed alert shape, a hard-coded issue number, comment update in place), not by GitHub. That is a real limit of this design and is why the writer is a separate, small Lambda with its own secret and role.
- Neither app has **Pull requests: write**, so neither can post on a contributor's pull request. Contributor-facing comments need a separate, later founder approval (and a permission change) per issue #198.
- An installation token is scoped to the repositories the app is installed on and expires after an hour. The app's private key can mint new tokens until it is deleted, so it is the sensitive object. See key handling.

## Key handling

- Each app has its own RSA private key (a `.pem` file GitHub generates once). It goes straight into AWS Secrets Manager and is then deleted locally. It is never pasted into chat, committed, logged, or placed in an environment variable.
- The secret value is JSON: `{"app_id": <number>, "installation_id": <number>, "private_key_pem": "<PEM text>"}`. The runtime reads it at run time, signs a 10-minute JWT, exchanges it for a one-hour installation token, and discards both. The runtime role may read only the reader secret; the alert-writer secret is denied to it.
- Rotation: GitHub allows several keys per app. Generate a new key, store it, verify, then delete the old key in the app's settings. Revocation is deleting the key, which takes effect immediately, or uninstalling the app.
- A leaked key is full reader or writer authority until deleted. Anyone with the administrator role or the deployer role can read these secrets through Secrets Manager; the deployer can write but, since the boundary change, cannot read them.

## Register the apps (you do this in the browser)

Do it once per app. Sign in as `vchokshi`, then **Settings, Developer settings, GitHub Apps, New GitHub App**.

| Field | Reader | Alert writer |
| --- | --- | --- |
| GitHub App name | `viharbot-kepler-reader` (names are global; if taken, add a suffix and tell me) | `viharbot-kepler-alert-writer` |
| Description | Read-only monitor for first-time contributors to ViharBot. | Posts one deduplicated tracking comment on issue #198. |
| Homepage URL | `https://github.com/vchokshi/viharbot` | same |
| Callback URL | leave blank | leave blank |
| Expire user authorization tokens | leave as is | leave as is |
| Request user authorization (OAuth) during installation | **unchecked** | **unchecked** |
| Enable Device Flow | unchecked | unchecked |
| Setup URL | blank | blank |
| Webhook, Active | **unchecked** | **unchecked** |
| Repository permissions | Actions: Read-only. Checks: Read-only. Commit statuses: Read-only. Contents: Read-only. Issues: Read-only. Metadata: Read-only. Pull requests: Read-only. | Issues: **Read and write**. Metadata: Read-only. Nothing else. |
| Organization and account permissions | none | none |
| Subscribe to events | none | none |
| Where can this GitHub App be installed? | **Only on this account** | **Only on this account** |

After **Create GitHub App**:
1. Note the **App ID** (a number at the top of the app's page). It is not secret.
2. Under **Private keys**, click **Generate a private key**. A `.pem` downloads. Move it out of Downloads to a private folder and do not open it in any shared tool.
3. In the app's left sidebar choose **Install App**, install it on `vchokshi`, choose **Only select repositories**, and select **`viharbot` only**.
4. After installing, the address bar shows `github.com/settings/installations/<number>`. That number is the **installation ID**.
5. Store the key and ids with `deploy/agentcore/kepler/github-app/store-app-secret.sh` (below), which builds the secret JSON without printing the key, writes it to Secrets Manager, and can delete the `.pem`.
6. Verify with `deploy/agentcore/kepler/github-app/verify-app.py`.

## Store the secret

The secret must exist first. The deployer creates it empty (a small recurring charge per secret, about USD 0.40 a month), then you fill it:

```sh
bash deploy/agentcore/kepler/github-app/store-app-secret.sh reader <app_id> <installation_id> /path/to/key.pem --delete-pem
```

Use `writer` for the second app. It uses your administrator login, never prints the key, and with `--delete-pem` removes the local file afterwards.

## Verify

```sh
python3 deploy/agentcore/kepler/github-app/verify-app.py reader
python3 deploy/agentcore/kepler/github-app/verify-app.py writer
```

It reads the secret with your AWS login, signs a short-lived JWT locally, and checks, without printing any key or token: the app's registered permissions equal the expected set exactly (no extra permission); no webhook events; the installation belongs to `vchokshi` and covers selected repositories; the installation token reaches `viharbot` and nothing else; the reader can read PR #179, the CLA record, workflow runs and checks; the reader's write attempt is refused (403); the writer's write attempt is authorized (a deliberately empty comment returns 422 and posts nothing); and the writer cannot read pull requests or contents. Exit status is non-zero on any failure.

## Limitations

- Registration is manual: GitHub's App registration needs the web interface (or a browser-based manifest flow, not used here). The verification script is what proves the result.
- `Issues: write` on the writer is broader than the one comment it needs, as above. A stricter design would need a different channel (for example, a separate alerts repository where the writer's blast radius is only alert issues). That is an open option, not implemented.
- The checks prove the apps' configuration at the time they run. They do not prove the runtime or Lambda use them correctly; neither exists yet.
