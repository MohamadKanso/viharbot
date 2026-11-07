# One-time administrator steps: boundary policy and deployer role

These create two IAM objects in the Cyera AWS lab account, `viharbot-kepler-boundary` (a policy) and `viharbot-kepler-deployer` (a role). They are run **by the founder, as administrator**, because the deployer role cannot create or change its own boundary. Nothing else is created here: no runtime, secrets, tables or schedules. Record the result in [`docs/aws-lab-change-record.md`](../../../../docs/aws-lab-change-record.md) as a new row, with the approver.

The policy files use the placeholder `ACCOUNT_ID`. The commands below fill it in from your current login into a temporary directory, so no account ID is written into the repository. They assume the AWS CLI is signed in to the lab account (check with `aws sts get-caller-identity`) and that you run them from the repository root.

```sh
export AWS_REGION=us-west-2
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
WORK=$(mktemp -d)
for f in boundary-policy deployer-trust deployer-policy; do
  sed "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "deploy/agentcore/kepler/iam/${f}.json" > "${WORK}/${f}.json"
done

# 1. The ceiling that every role the deployer creates must carry.
aws iam create-policy --policy-name viharbot-kepler-boundary \
  --policy-document "file://${WORK}/boundary-policy.json" \
  --tags Key=project,Value=viharbot Key=agent,Value=kepler Key=environment,Value=lab \
         Key=managed-by,Value=human-supervised

# 2. The deployer role, assumable only from the founder's SSO administrator role.
aws iam create-role --role-name viharbot-kepler-deployer \
  --assume-role-policy-document "file://${WORK}/deployer-trust.json" \
  --max-session-duration 3600 \
  --tags Key=project,Value=viharbot Key=agent,Value=kepler Key=environment,Value=lab \
         Key=managed-by,Value=human-supervised

# 3. What the deployer may do.
aws iam put-role-policy --role-name viharbot-kepler-deployer \
  --policy-name viharbot-kepler-deployer \
  --policy-document "file://${WORK}/deployer-policy.json"
```

## Verify

```sh
aws iam get-role --role-name viharbot-kepler-deployer --query 'Role.[Arn,Tags]'
aws iam get-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/viharbot-kepler-boundary" --query 'Policy.[Arn,Tags]'
# Prove you can assume it (this only returns a session identity, it changes nothing):
aws sts assume-role --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/viharbot-kepler-deployer" \
  --role-session-name check --query 'AssumedRoleUser.Arn'
```

## Roll back

```sh
aws iam delete-role-policy --role-name viharbot-kepler-deployer --policy-name viharbot-kepler-deployer
aws iam delete-role --role-name viharbot-kepler-deployer
aws iam delete-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/viharbot-kepler-boundary"
```

Delete the boundary policy last, and only after no role still uses it. If you later change a policy file, validate it first with `aws accessanalyzer validate-policy` (see the pull request that introduced these files) and add a new row to the change record.

## Enable the Bedrock allowlist (ADR-011), administrator only

Boundary version 3 and the runtime policy replace the blanket deny on `bedrock:InvokeModel*` with an allow for exactly one non-Anthropic model, `meta.llama3-1-8b-instruct-v1:0`, an explicit deny for every other model, and an explicit deny for Anthropic models and inference profiles. **Not applied by the pull request that adds these files.** Run as the SSO administrator, from the repository root, after confirming the account has Bedrock access to that model (the catalog listing does not prove it) and the current price:

```sh
export AWS_PAGER="" ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
WORK=$(mktemp -d)
for f in boundary-policy runtime-policy; do
  sed "s/ACCOUNT_ID/${ACCOUNT_ID}/g" deploy/agentcore/kepler/iam/$f.json > "$WORK/$f.json"
  aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document "file://$WORK/$f.json"   # expect no findings
done
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/viharbot-kepler-boundary"
aws iam list-policy-versions --policy-arn "$BOUNDARY"          # a policy holds 5 versions; delete the oldest non-default if full
aws iam create-policy-version --policy-arn "$BOUNDARY" --policy-document "file://$WORK/boundary-policy.json" --set-as-default
aws iam put-role-policy --role-name viharbot-kepler-runtime --policy-name viharbot-kepler-runtime \
  --policy-document "file://$WORK/runtime-policy.json"
bash deploy/agentcore/kepler/iam/tests/policy-simulation.sh      # every check must pass
```

Before applying, you can review exactly what the new files will do without touching any role: `bash deploy/agentcore/kepler/iam/tests/policy-simulation-files.sh` evaluates the runtime policy capped by the boundary from the repository files alone (27 checks: one model allowed, every other model and every inference profile denied, `sns:Publish` to the alerts topic only, and the existing protections unchanged). `policy-simulation.sh` checks the live roles, so it only passes after the change is applied.

The same change also adds `sns:Publish` on `viharbot-kepler-alerts` to the runtime policy and a matching ceiling to the boundary, which the alert email needs whether or not a model is used.

Record a new row in the change record with the approver. **Roll back:** `aws iam set-default-policy-version --policy-arn "$BOUNDARY" --version-id <previous>`, and `put-role-policy` the previous runtime policy from git history. Applying this does not make Kepler call a model: that also needs `KEPLER_MODEL_ID` set to the one allowlisted ID in the runtime's configuration, and the steward refuses any other ID.
