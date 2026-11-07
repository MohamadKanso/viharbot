#!/bin/bash
# Read-only IAM policy simulation checks for the Kepler boundary, deployer and runtime roles.
#
# Uses your current administrator login to call the IAM policy simulator. It creates nothing and
# changes nothing. Run from the repository root:
#   bash deploy/agentcore/kepler/iam/tests/policy-simulation.sh
#
# Each check states the decision it expects. "implicitDeny" means no statement allowed the action;
# "explicitDeny" means a Deny statement matched. A mismatch is a failure.
set +e
export AWS_PAGER=""
IAM_DIR="deploy/agentcore/kepler/iam"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
DEPLOYER="arn:aws:iam::$ACCT:role/viharbot-kepler-deployer"
RUNTIME="arn:aws:iam::$ACCT:role/viharbot-kepler-runtime"

sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/boundary-policy.json" > "$WORK/boundary.json"
python3 - "$WORK" <<'PY'
import json, sys
w = sys.argv[1]
json.dump(['{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'],
          open(f"{w}/allow-all-input.json", "w"))
json.dump([open(f"{w}/boundary.json").read()], open(f"{w}/boundary-input.json", "w"))
PY

pass=0; fail=0
check() {  # name expected decision-from-stdin
  local name="$1" expected="$2" got="$3"
  if [ "$got" = "$expected" ]; then echo "PASS  $expected : $name"; pass=$((pass+1))
  else echo "FAIL  expected $expected, got ${got:-<none>} : $name"; fail=$((fail+1)); fi
}
capped() {  # all-allow identity policy capped by the boundary
  aws iam simulate-custom-policy --policy-input-list "file://$WORK/allow-all-input.json" \
    --permissions-boundary-policy-input-list "file://$WORK/boundary-input.json" \
    --action-names "$1" --resource-arns "$2" --query 'EvaluationResults[0].EvalDecision' --output text
}
principal() {  # a live role's effective decision
  aws iam simulate-principal-policy --policy-source-arn "$1" --action-names "$2" --resource-arns "$3" \
    --query 'EvaluationResults[0].EvalDecision' --output text
}

echo "--- A. boundary is resource-scoped (all-allow identity policy, capped by the boundary)"
SEC="arn:aws:secretsmanager:us-west-2:$ACCT:secret"
check "read a Kepler secret"            allowed      "$(capped secretsmanager:GetSecretValue "$SEC:viharbot/kepler/github-app-reader-AbCdEf")"
check "read another team's secret"      implicitDeny "$(capped secretsmanager:GetSecretValue "$SEC:other-team/db-AbCdEf")"
check "read a Kepler table"             allowed      "$(capped dynamodb:GetItem "arn:aws:dynamodb:eu-west-1:$ACCT:table/viharbot-kepler-state")"
check "read another table"              implicitDeny "$(capped dynamodb:GetItem "arn:aws:dynamodb:us-west-2:$ACCT:table/some-other-table")"
check "invoke a Kepler function"        allowed      "$(capped lambda:InvokeFunction "arn:aws:lambda:us-west-2:$ACCT:function:viharbot-kepler-alert-writer")"
check "invoke another function"         implicitDeny "$(capped lambda:InvokeFunction "arn:aws:lambda:us-west-2:$ACCT:function:payments")"
check "pull the Kepler image"           allowed      "$(capped ecr:BatchGetImage "arn:aws:ecr:us-west-2:$ACCT:repository/viharbot/kepler")"
check "pull another image"              implicitDeny "$(capped ecr:BatchGetImage "arn:aws:ecr:us-west-2:$ACCT:repository/other/app")"
check "invoke a runtime in this account" allowed     "$(capped bedrock-agentcore:InvokeAgentRuntime "arn:aws:bedrock-agentcore:us-west-2:$ACCT:runtime/kepler-AbCdEf")"
check "invoke a runtime in another account" implicitDeny "$(capped bedrock-agentcore:InvokeAgentRuntime "arn:aws:bedrock-agentcore:us-west-2:999999999999:runtime/x-AbCdEf")"
# For CloudWatch Logs the simulator evaluates the log GROUP (NAME:*), never a log-stream ARN.
check "write logs in a Kepler group"    allowed      "$(capped logs:PutLogEvents "arn:aws:logs:us-west-2:$ACCT:log-group:/aws/lambda/viharbot-kepler-alert-writer:*")"
check "write logs in another group"     implicitDeny "$(capped logs:PutLogEvents "arn:aws:logs:us-west-2:$ACCT:log-group:/aws/lambda/payments:*")"
LLAMA="arn:aws:bedrock:us-west-2::foundation-model/meta.llama3-1-8b-instruct-v1:0"
check "invoke the allowlisted Llama model" allowed "$(capped bedrock:InvokeModel "$LLAMA")"
check "publish to a Kepler alerts topic" allowed "$(capped sns:Publish "arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-alerts")"
check "publish to an unrelated topic" implicitDeny "$(capped sns:Publish "arn:aws:sns:us-west-2:$ACCT:someone-elses-topic")"
check "invoke another Llama model"      explicitDeny "$(capped bedrock:InvokeModel "arn:aws:bedrock:us-west-2::foundation-model/meta.llama3-70b-instruct-v1:0")"
check "invoke an Anthropic model"       explicitDeny "$(capped bedrock:InvokeModel "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-haiku-20240307-v1:0")"
check "invoke an Anthropic profile"     explicitDeny "$(capped bedrock:InvokeModel "arn:aws:bedrock:us-west-2:$ACCT:inference-profile/us.anthropic.claude-3-haiku-20240307-v1:0")"
check "create an IAM user"              explicitDeny "$(capped iam:CreateUser '*')"
check "assume another role"             explicitDeny "$(capped sts:AssumeRole '*')"

echo "--- B, C, D. the deployer lost the powers it did not need"
check "set a secret's resource policy"  implicitDeny "$(principal "$DEPLOYER" secretsmanager:PutResourcePolicy "$SEC:viharbot/kepler/github-app-reader-AbCdEf")"
check "set the ECR repository policy"   implicitDeny "$(principal "$DEPLOYER" ecr:SetRepositoryPolicy "arn:aws:ecr:us-west-2:$ACCT:repository/viharbot/kepler")"
check "add a Lambda resource permission" implicitDeny "$(principal "$DEPLOYER" lambda:AddPermission "arn:aws:lambda:us-west-2:$ACCT:function:viharbot-kepler-alert-writer")"
check "rewrite a workload role's trust" implicitDeny "$(principal "$DEPLOYER" iam:UpdateAssumeRolePolicy "$RUNTIME")"
AUDIT="arn:aws:dynamodb:us-west-2:$ACCT:table/viharbot-kepler-audit"
check "update the audit table"          explicitDeny "$(principal "$DEPLOYER" dynamodb:UpdateTable "$AUDIT")"
check "change audit backups"            explicitDeny "$(principal "$DEPLOYER" dynamodb:UpdateContinuousBackups "$AUDIT")"
check "delete the audit table"          explicitDeny "$(principal "$DEPLOYER" dynamodb:DeleteTable "$AUDIT")"
check "still update the state table"    allowed      "$(principal "$DEPLOYER" dynamodb:UpdateTable "arn:aws:dynamodb:us-west-2:$ACCT:table/viharbot-kepler-state")"
check "still create the audit table"    allowed      "$(principal "$DEPLOYER" dynamodb:CreateTable "$AUDIT")"
check "still attach a policy to a bounded Kepler role" allowed "$(aws iam simulate-principal-policy --policy-source-arn "$DEPLOYER" --action-names iam:AttachRolePolicy --resource-arns "$RUNTIME" --context-entries "ContextKeyName=iam:PermissionsBoundary,ContextKeyValues=arn:aws:iam::$ACCT:policy/viharbot-kepler-boundary,ContextKeyType=string" --query 'EvaluationResults[0].EvalDecision' --output text)"

echo "--- regression: the live runtime role still has what it needs"
check "read the reader secret"          allowed      "$(principal "$RUNTIME" secretsmanager:GetSecretValue "$SEC:viharbot/kepler/github-app-reader-AbCdEf")"
check "read the LiteLLM secret"         allowed      "$(principal "$RUNTIME" secretsmanager:GetSecretValue "$SEC:viharbot/kepler/litellm-api-key-AbCdEf")"
check "read the alert-writer secret"    explicitDeny "$(principal "$RUNTIME" secretsmanager:GetSecretValue "$SEC:viharbot/kepler/github-app-alert-writer-AbCdEf")"
check "write an audit row"              allowed      "$(principal "$RUNTIME" dynamodb:PutItem "$AUDIT")"
check "update an audit row"             explicitDeny "$(principal "$RUNTIME" dynamodb:UpdateItem "$AUDIT")"
check "update a state row"              allowed      "$(principal "$RUNTIME" dynamodb:UpdateItem "arn:aws:dynamodb:us-west-2:$ACCT:table/viharbot-kepler-state")"
check "delete a state row"              explicitDeny "$(principal "$RUNTIME" dynamodb:DeleteItem "arn:aws:dynamodb:us-west-2:$ACCT:table/viharbot-kepler-state")"
check "pull its image"                  allowed      "$(principal "$RUNTIME" ecr:BatchGetImage "arn:aws:ecr:us-west-2:$ACCT:repository/viharbot/kepler")"
check "write runtime logs"              allowed      "$(principal "$RUNTIME" logs:PutLogEvents "arn:aws:logs:us-west-2:$ACCT:log-group:/aws/bedrock-agentcore/runtimes/x:*")"
check "invoke the alert writer"         allowed      "$(principal "$RUNTIME" lambda:InvokeFunction "arn:aws:lambda:us-west-2:$ACCT:function:viharbot-kepler-alert-writer")"
check "runtime: invoke the allowlisted Llama" allowed "$(principal "$RUNTIME" bedrock:InvokeModel "$LLAMA")"
check "runtime: publish to the alerts topic" allowed "$(principal "$RUNTIME" sns:Publish "arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-alerts")"
check "runtime: publish to an unrelated topic" implicitDeny "$(principal "$RUNTIME" sns:Publish "arn:aws:sns:us-west-2:$ACCT:someone-elses-topic")"
check "runtime: invoke an Anthropic model" explicitDeny "$(principal "$RUNTIME" bedrock:InvokeModel "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-haiku-20240307-v1:0")"
check "runtime: invoke a non-allowlisted model" explicitDeny "$(principal "$RUNTIME" bedrock:InvokeModel "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0")"

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
