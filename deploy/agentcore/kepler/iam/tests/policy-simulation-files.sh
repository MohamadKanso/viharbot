#!/bin/bash
# Read-only simulation of the PROPOSED policy FILES, before anything is applied.
#
# policy-simulation.sh checks the live roles, so it cannot be used to review a change that has not
# been applied yet. This script evaluates the runtime policy together with the boundary exactly as
# IAM would for the runtime role (the identity policy capped by the permissions boundary), using
# only the files in this repository. It creates nothing and changes nothing. Run from the
# repository root with your administrator login:
#   bash deploy/agentcore/kepler/iam/tests/policy-simulation-files.sh
set +e
export AWS_PAGER=""
IAM_DIR="deploy/agentcore/kepler/iam"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/runtime-policy.json"  > "$WORK/runtime.json"
sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/boundary-policy.json" > "$WORK/boundary.json"
python3 - "$WORK" <<'PY'
import json, sys
w = sys.argv[1]
json.dump([open(f"{w}/runtime.json").read()], open(f"{w}/runtime-input.json", "w"))
json.dump([open(f"{w}/boundary.json").read()], open(f"{w}/boundary-input.json", "w"))
json.dump(['{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'],
          open(f"{w}/allow-all-input.json", "w"))
PY

pass=0; fail=0
check() {
  if [ "$3" = "$2" ]; then echo "PASS  $2 : $1"; pass=$((pass+1))
  else echo "FAIL  expected $2, got ${3:-<none>} : $1"; fail=$((fail+1)); fi
}
runtime() {  # the runtime role as IAM would evaluate it: its policy capped by the boundary
  aws iam simulate-custom-policy --policy-input-list "file://$WORK/runtime-input.json" \
    --permissions-boundary-policy-input-list "file://$WORK/boundary-input.json" \
    --action-names "$1" --resource-arns "$2" --query 'EvaluationResults[0].EvalDecision' --output text
}
ceiling() {  # the boundary alone: an all-allow identity policy capped by it
  aws iam simulate-custom-policy --policy-input-list "file://$WORK/allow-all-input.json" \
    --permissions-boundary-policy-input-list "file://$WORK/boundary-input.json" \
    --action-names "$1" --resource-arns "$2" --query 'EvaluationResults[0].EvalDecision' --output text
}

LLAMA="arn:aws:bedrock:us-west-2::foundation-model/meta.llama3-1-8b-instruct-v1:0"
FM="arn:aws:bedrock:us-west-2::foundation-model"
TOPIC="arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-alerts"
SEC="arn:aws:secretsmanager:us-west-2:$ACCT:secret"
DDB="arn:aws:dynamodb:us-west-2:$ACCT:table"

echo "--- the runtime role may call exactly one model"
check "the allowlisted Llama model"                    allowed      "$(runtime bedrock:InvokeModel "$LLAMA")"
check "another Llama (70B)"                            explicitDeny "$(runtime bedrock:InvokeModel "$FM/meta.llama3-70b-instruct-v1:0")"
check "Llama 3.3 70B"                                  explicitDeny "$(runtime bedrock:InvokeModel "$FM/meta.llama3-3-70b-instruct-v1:0")"
check "an Anthropic model"                             explicitDeny "$(runtime bedrock:InvokeModel "$FM/anthropic.claude-3-haiku-20240307-v1:0")"
check "an Anthropic inference profile"                 explicitDeny "$(runtime bedrock:InvokeModel "arn:aws:bedrock:us-west-2:$ACCT:inference-profile/us.anthropic.claude-sonnet-5")"
check "the allowlisted model through a cross-region profile" explicitDeny "$(runtime bedrock:InvokeModel "arn:aws:bedrock:us-west-2:$ACCT:inference-profile/us.meta.llama3-1-8b-instruct-v1:0")"
check "an OpenAI model on Bedrock"                     explicitDeny "$(runtime bedrock:InvokeModel "$FM/openai.gpt-5.4")"
check "an Amazon model"                                explicitDeny "$(runtime bedrock:InvokeModel "$FM/amazon.nova-micro-v1:0")"

echo "--- the alert email can be sent, to the founder's topic only"
check "publish to the alerts topic"                    allowed      "$(runtime sns:Publish "$TOPIC")"
check "publish to another Kepler topic"                implicitDeny "$(runtime sns:Publish "arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-other")"
check "publish to an unrelated topic"                  implicitDeny "$(runtime sns:Publish "arn:aws:sns:us-west-2:$ACCT:someone-elses-topic")"
check "publish to a topic in another account"          implicitDeny "$(runtime sns:Publish "arn:aws:sns:us-west-2:999999999999:viharbot-kepler-alerts")"
check "change the topic's policy"                      implicitDeny "$(runtime sns:SetTopicAttributes "$TOPIC")"
check "subscribe someone to the topic"                 implicitDeny "$(runtime sns:Subscribe "$TOPIC")"

echo "--- protections that must not have moved"
check "read the reader secret"                         allowed      "$(runtime secretsmanager:GetSecretValue "$SEC:viharbot/kepler/github-app-reader-AbCdEf")"
check "read the alert-writer secret"                   explicitDeny "$(runtime secretsmanager:GetSecretValue "$SEC:viharbot/kepler/github-app-alert-writer-AbCdEf")"
check "write an audit row"                             allowed      "$(runtime dynamodb:PutItem "$DDB/viharbot-kepler-audit")"
check "update an audit row"                            explicitDeny "$(runtime dynamodb:UpdateItem "$DDB/viharbot-kepler-audit")"
check "delete a state row"                             explicitDeny "$(runtime dynamodb:DeleteItem "$DDB/viharbot-kepler-state")"
check "create an IAM user (the boundary denies iam:*)" explicitDeny "$(runtime iam:CreateUser '*')"

echo "--- the boundary caps any role the deployer creates"
check "boundary: the allowlisted model"                allowed      "$(ceiling bedrock:InvokeModel "$LLAMA")"
check "boundary: another model"                        explicitDeny "$(ceiling bedrock:InvokeModel "$FM/meta.llama3-3-70b-instruct-v1:0")"
check "boundary: an Anthropic model"                   explicitDeny "$(ceiling bedrock:InvokeModel "$FM/anthropic.claude-3-haiku-20240307-v1:0")"
check "boundary: publish to a Kepler topic"            allowed      "$(ceiling sns:Publish "$TOPIC")"
check "boundary: publish to an unrelated topic"        implicitDeny "$(ceiling sns:Publish "arn:aws:sns:us-west-2:$ACCT:someone-elses-topic")"
check "boundary: assume another role"                  explicitDeny "$(ceiling sts:AssumeRole '*')"
check "boundary: create an IAM user"                   explicitDeny "$(ceiling iam:CreateUser '*')"

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
