#!/bin/bash
# Creates Kepler's foundation resources in the Cyera AWS lab account, as the deployer role.
#
#   ALERT_EMAIL=you@example.com bash deploy/agentcore/kepler/provision-foundation.sh
#
# Run from the repository root, signed in as the account administrator. It assumes
# viharbot-kepler-deployer (credentials stay inside this process) and creates, all tagged
# project=viharbot, agent=kepler, environment=lab, owner=vchokshi, managed-by=human-supervised:
#   1. ECR repository viharbot/kepler            (immutable tags, scan on push)
#   2. DynamoDB viharbot-kepler-state            (mutable deduplication state, TTL on expires_at)
#   3. DynamoDB viharbot-kepler-audit            (write-and-read audit rows, no TTL)
#   4. Log group /aws/lambda/viharbot-kepler-alert-writer (30-day retention)
#   5. IAM role viharbot-kepler-runtime          (permissions boundary attached)
#   6. SNS topic, a poll-failure alarm and a USD 50 monthly budget, notifying ALERT_EMAIL
# This is a record of how the foundation was created, not an idempotent tool: re-running it fails
# at the first step because the resources already exist. Note also that, since the IAM review, the
# deployer is denied altering or deleting the audit table, so a rebuild would need the
# administrator to enable that table's point-in-time recovery.
# Nothing here runs a model, holds a secret or starts an agent. The email address is never stored
# in the repository. Record the outcome in docs/aws-lab-change-record.md.
set -u
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
: "${ALERT_EMAIL:?set ALERT_EMAIL to the address for budget and alarm notifications}"
IAM_DIR="deploy/agentcore/kepler/iam"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
BOUNDARY="arn:aws:iam::$ACCT:policy/viharbot-kepler-boundary"
TAGS_KV="Key=project,Value=viharbot Key=agent,Value=kepler Key=environment,Value=lab Key=owner,Value=vchokshi Key=managed-by,Value=human-supervised"

sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/runtime-trust.json"  > "$WORK/runtime-trust.json"
sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/runtime-policy.json" > "$WORK/runtime-policy.json"

read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name provision-foundation --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

step() { local name="$1"; shift; echo "== $name"; if ! "$@"; then echo "STOPPED at: $name"; exit 1; fi; }

step "1. ECR repository" aws ecr create-repository --repository-name viharbot/kepler \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true --tags $TAGS_KV \
  --query 'repository.repositoryArn' --output text

for T in state audit; do
  step "2/3. DynamoDB viharbot-kepler-$T" aws dynamodb create-table --table-name "viharbot-kepler-$T" \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST --deletion-protection-enabled --tags $TAGS_KV \
    --query 'TableDescription.TableArn' --output text
  step "   wait for $T to be active" aws dynamodb wait table-exists --table-name "viharbot-kepler-$T"
  step "   point-in-time recovery on $T" aws dynamodb update-continuous-backups \
    --table-name "viharbot-kepler-$T" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
    --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' --output text
done
step "   TTL on state (expires_at)" aws dynamodb update-time-to-live --table-name viharbot-kepler-state \
  --time-to-live-specification Enabled=true,AttributeName=expires_at \
  --query 'TimeToLiveSpecification.AttributeName' --output text

step "4. Log group (30 days)" aws logs create-log-group \
  --log-group-name /aws/lambda/viharbot-kepler-alert-writer \
  --tags project=viharbot,agent=kepler,environment=lab,owner=vchokshi,managed-by=human-supervised
step "   retention" aws logs put-retention-policy \
  --log-group-name /aws/lambda/viharbot-kepler-alert-writer --retention-in-days 30

step "5. Runtime role (with the boundary)" aws iam create-role --role-name viharbot-kepler-runtime \
  --assume-role-policy-document "file://$WORK/runtime-trust.json" --permissions-boundary "$BOUNDARY" \
  --max-session-duration 3600 --tags $TAGS_KV --query 'Role.Arn' --output text
step "   runtime role permissions" aws iam put-role-policy --role-name viharbot-kepler-runtime \
  --policy-name viharbot-kepler-runtime --policy-document "file://$WORK/runtime-policy.json"

step "6a. SNS topic" aws sns create-topic --name viharbot-kepler-alerts --tags $TAGS_KV \
  --query 'TopicArn' --output text
TOPIC="arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-alerts"
step "   subscribe $(echo "$ALERT_EMAIL" | sed 's/.*@/***@/') (confirm the email AWS sends)" \
  aws sns subscribe --topic-arn "$TOPIC" --protocol email --notification-endpoint "$ALERT_EMAIL" \
  --query 'SubscriptionArn' --output text
step "6b. Poll-failure alarm" aws cloudwatch put-metric-alarm --alarm-name viharbot-kepler-poll-failures \
  --namespace ViharBot/Kepler --metric-name PollFailed --statistic Sum --period 900 \
  --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC" --tags $TAGS_KV

python3 - "$ALERT_EMAIL" "$WORK" <<'PY'
import json, sys
email, work = sys.argv[1], sys.argv[2]
json.dump({"BudgetName": "viharbot-kepler-monthly", "BudgetType": "COST", "TimeUnit": "MONTHLY",
           "BudgetLimit": {"Amount": "50", "Unit": "USD"}}, open(f"{work}/budget.json", "w"))
def n(kind, pct):
    return {"Notification": {"NotificationType": kind, "ComparisonOperator": "GREATER_THAN",
            "Threshold": pct, "ThresholdType": "PERCENTAGE"},
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}]}
json.dump([n("ACTUAL", 50), n("ACTUAL", 80), n("ACTUAL", 100), n("FORECASTED", 100)],
          open(f"{work}/notifications.json", "w"))
PY
step "6c. Budget USD 50 / month (whole account until cost tags are activated)" aws budgets create-budget \
  --account-id "$ACCT" --budget "file://$WORK/budget.json" \
  --notifications-with-subscribers "file://$WORK/notifications.json"
echo "DONE: all foundation resources created"
