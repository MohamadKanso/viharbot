# Kepler GitHub Apps

Status: **design and tooling only; no app is registered yet.** Tracking issue [#198](https://github.com/vchokshi/viharbot/issues/198). Part of [Kepler: first-contributor steward on AWS AgentCore](kepler-steward-aws.md). No private key ever appears in the repository, in chat, or in logs.

## Plan, in two phases

**Phase 1, now: read-only.** One GitHub App, the **reader**. Kepler checks contributor pull requests every 6 hours (plus a manual check-now), works out what each first-time contributor still needs, and **emails the founder** when something needs attention, including a **draft** of the message it would send. The founder posts any contributor message by hand, as on PR #179. There is no GitHub write credential anywhere.

**Phase 2, later, only after the founder authorizes it in writing: approved comments.** A second app, the **writer**, posts founder-approved comments on contributor pull requests. It is not registered, and nothing about it is built, until the approval flow exists and the drafts have proven good. See "The writer, later".

Status and alerts to the founder never go through GitHub. See "Alerts and status".

## The reader app (register now)

| | `viharbot-kepler-reader` |
| --- | --- |
| Used by | The AgentCore runtime (the monitor) |
| Purpose | Read pull requests, issues, CI and the CLA record |
| Repository permissions | Metadata: read. Pull requests: read. Issues: read. Contents: read. Checks: read. Commit statuses: read. Actions: read |
| Account and organization permissions | none |
| Webhook | **Off.** Polling only |
| Subscribed events | none |
| Installable on | Only this account |
| Installed on | `vchokshi/viharbot` only (selected repositories) |
| AWS secret | `viharbot/kepler/github-app-reader` |

Why each permission is needed: **Pull requests** and **Issues** for the PR, its conversation and the linked issue; **Contents** to read `CLA-ACCEPTANCES.md` and the governance files from the trusted base branch, never from a contributor's branch; **Checks**, **Commit statuses** and **Actions** because a fork's workflows can sit at `action_required` awaiting maintainer approval, which only shows in workflow runs; **Metadata** is mandatory.

The reader cannot comment, label, close, merge, approve, push, change settings or run workflows; a write attempt returns 403. The repository is public, so what the reader can read is not secret. It is read-only because its input is untrusted, not because its output is sensitive.

## Alerts and status (no GitHub write)

- **Alerts: email.** Kepler publishes to the existing SNS topic `viharbot-kepler-alerts` (the founder's address is subscribed and confirmed) only when something needs attention, for example a first-time contributor has not posted the CLA acceptance after a set time, or a fork's CI is waiting for approval. Each observation has an ID recorded in the state table, so the same problem produces one email, not one per check, and a duplicate GitHub event cannot produce a duplicate email. The email carries the PR link, what is outstanding, and a draft message. Treat an email as something that could be forwarded; it should contain facts already public on the PR and nothing about the founder's accounts.
- **Status on demand.** Kepler's current view of each contributor PR lives in the state table. A small script (planned, not built) will print where each PR stands whenever the founder asks, so no standing status feed is needed.
- **Optional weekly digest** email of everything open (planned, not built).
- **Why not a GitHub issue comment.** The repository is public, so a comment on #198 would be public, and posting it needs a GitHub write credential for a status note. Email avoids both.

## The writer, later

Not registered, not built. Recorded here so the design is agreed before any write credential exists.

- **What it would be.** A separate app, named for contributors to see as the bot identity (`viharbot-kepler`), with its own secret and role, called by a small separate function. It is never called by the reader, and the reader's role cannot read its secret.
- **Permission and its real limits.** It needs **Issues: read and write**. **GitHub has no comment-only permission, and a pull request comment is an issue comment, so this permission can post comments on contributor pull requests. It can also edit and close issues and pull requests, and change labels.** None of those limits is enforced by GitHub. They are enforced by the writer's own code and by the approval gate below, which is why the writer is small, separate and last.
- **Approval gate (design).** The founder approves one specific draft; the stored approval is bound to a hash of the exact text, in the same style as the deployment controller's approvals. The writer posts only an approved draft, only on the pull request it was written for, only once, and never repeats a request already made (for example the CLA request). Its only operation is "create a comment": no close, label, merge, edit or delete. Every post is audited.
- **Not decided yet.** How the founder approves (for example a script that records the approval), what happens to a draft that goes stale because the PR changed, and the exact permission set (Pull requests: write instead of Issues: write is a possibility to weigh). These need the founder's decision before building.

## Key handling

- Each app has its own RSA private key (a `.pem` file GitHub generates once). It goes straight into AWS Secrets Manager and is then deleted locally. It is never pasted into chat, committed, logged, or placed in an environment variable.
- The secret value is JSON: `{"app_id": <number>, "installation_id": <number>, "private_key_pem": "<PEM text>"}`. The runtime reads it at run time, signs a 10-minute JWT, exchanges it for a one-hour installation token, and discards both.
- Rotation: GitHub allows several keys per app. Generate a new key, store it, verify, then delete the old key in the app's settings. Revocation is deleting the key, which takes effect immediately, or uninstalling the app.
- A leaked key is full authority of that app until deleted. The administrator role can read the secrets; the deployer can write them but, since the boundary change, cannot read them.

## Register the reader app (you do this in the browser)

Sign in as `vchokshi`, then **Settings, Developer settings, GitHub Apps, New GitHub App**.

| Field | Value |
| --- | --- |
| GitHub App name | `viharbot-kepler-reader` (names are global; if taken, add a suffix and tell me) |
| Description | Read-only monitor for first-time contributors to ViharBot. |
| Homepage URL | `https://github.com/vchokshi/viharbot` |
| Callback URL | leave blank |
| Expire user authorization tokens | leave as is |
| Request user authorization (OAuth) during installation | **unchecked** |
| Enable Device Flow | unchecked |
| Setup URL | blank |
| Webhook, Active | **unchecked** |
| Repository permissions | Actions: Read-only. Checks: Read-only. Commit statuses: Read-only. Contents: Read-only. Issues: Read-only. Metadata: Read-only. Pull requests: Read-only. |
| Organization and account permissions | none |
| Subscribe to events | none |
| Where can this GitHub App be installed? | **Only on this account** |

After **Create GitHub App**:
1. Note the **App ID** (a number at the top of the app's page). It is not secret.
2. Under **Private keys**, click **Generate a private key**. A `.pem` downloads. Move it out of Downloads to a private folder and do not open it in any shared tool.
3. In the app's left sidebar choose **Install App**, install it on `vchokshi`, choose **Only select repositories**, and select **`viharbot` only**.
4. After installing, the address bar shows `github.com/settings/installations/<number>`. That number is the **installation ID**.
5. Store the key and ids with `deploy/agentcore/kepler/github-app/store-app-secret.sh` (below), which builds the secret JSON without printing the key, writes it to Secrets Manager, and can delete the `.pem`.
6. Verify with `deploy/agentcore/kepler/github-app/verify-app.py reader`.

## Store the secret

The secret must exist first. The deployer creates it empty (about USD 0.40 a month), then you fill it:

```sh
bash deploy/agentcore/kepler/github-app/store-app-secret.sh reader <app_id> <installation_id> /path/to/key.pem --delete-pem
```

It uses your administrator login, never prints the key, and with `--delete-pem` removes the local file afterwards. The script also accepts `writer` for Phase 2, using the secret `viharbot/kepler/github-app-alert-writer`; the internal name keeps "alert-writer" until the writer phase, when it can be renamed with an IAM change.

## Verify

```sh
python3 deploy/agentcore/kepler/github-app/verify-app.py reader
```

It reads the secret with your AWS login, signs a short-lived JWT locally, and checks, without printing any key or token: the app's registered permissions equal the expected set exactly (no extra permission); no webhook events; the installation belongs to `vchokshi` and covers selected repositories; the installation token reaches `viharbot` and nothing else; the reader can read PR #179, the CLA record, workflow runs and checks; and the reader's write attempt is refused (403). The only write it attempts is an empty comment that GitHub never creates. Exit status is non-zero on any failure. The same script can check the writer in Phase 2 (`verify-app.py writer`).

## Pending changes before the runtime can send email

The runtime role cannot yet publish to the SNS topic. Adding `sns:Publish` on `viharbot-kepler-alerts` to the runtime policy and the boundary (a new boundary version) is an administrator change to be made, validated and tested together with the runtime build, not in this PR, so the live IAM does not drift from reviewed files.

## Limitations

- Registration is manual: GitHub's App registration needs the web interface. The verification script is what proves the result.
- The checks prove the app's configuration at the time they run. They do not prove the runtime uses it correctly; the runtime does not exist.
- The writer's design above is a proposal: its approval flow, permission choice and the code-level limits do not exist.
