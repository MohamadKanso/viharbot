#!/bin/bash
# Authorization tests for the viharbot-kepler-deployer role.
#
# Assumes the deployer role from your current administrator login, keeps the temporary credentials
# inside this process (never printed), then checks that intended actions work (positive controls)
# and that boundary-escaping actions are denied. Creates and deletes ONE free test role. Run from
# the repository root:  bash deploy/agentcore/kepler/iam/tests/deployer-negative-test.sh
#
# A probe counts as "denied" only if AWS answers AccessDenied or explicit deny. A validation
# error is reported as a failure, because it means authorization was never evaluated.
set +e
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
IAM_DIR="deploy/agentcore/kepler/iam"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
BOUNDARY="arn:aws:iam::$ACCT:policy/viharbot-kepler-boundary"
SSO_ROLE=$(aws iam list-roles --path-prefix /aws-reserved/sso.amazonaws.com/ \
  --query "Roles[?contains(RoleName,'AWSAdministratorAccess')].RoleName | [0]" --output text)

sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/runtime-trust.json"   > "$WORK/runtime-trust.json"
sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/deployer-trust.json"  > "$WORK/deployer-trust.json"
sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/boundary-policy.json" > "$WORK/boundary-policy.json"
python3 -c "import zipfile;z=zipfile.ZipFile('$WORK/empty.zip','w');z.writestr('x.py','def y(e,c): return 1');z.close()"

read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name auth-test --duration-seconds 900 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

pass=0; fail=0
denied() { local name="$1"; shift; out=$("$@" 2>&1); rc=$?
  if [ $rc -ne 0 ] && echo "$out" | grep -qiE "AccessDenied|not authorized|UnauthorizedOperation|explicit deny"; then
    echo "PASS  denied   : $name"; pass=$((pass+1))
  else echo "FAIL  NOT DENIED: $name (rc=$rc) :: $(echo "$out" | head -c 200)"; fail=$((fail+1)); fi; }
allowed() { local name="$1"; shift; out=$("$@" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo "PASS  allowed  : $name"; pass=$((pass+1))
  else echo "FAIL  NOT ALLOWED: $name (rc=$rc) :: $(echo "$out" | head -c 200)"; fail=$((fail+1)); fi; }

echo "--- positive controls"
allowed "read own role" aws iam get-role --role-name viharbot-kepler-deployer
allowed "read costs" aws ce get-cost-and-usage --time-period Start=2026-11-01,End=2026-11-03 --granularity DAILY --metrics UnblendedCost
allowed "create bounded test role" aws iam create-role --role-name viharbot-kepler-test-neg --assume-role-policy-document "file://$WORK/runtime-trust.json" --permissions-boundary "$BOUNDARY"
allowed "test role carries the boundary" bash -c "aws iam get-role --role-name viharbot-kepler-test-neg --query Role.PermissionsBoundary.PermissionsBoundaryArn --output text | grep -q viharbot-kepler-boundary"

echo "--- negative tests"
denied "create a Kepler role WITHOUT the boundary" aws iam create-role --role-name viharbot-kepler-test-neg2 --assume-role-policy-document "file://$WORK/runtime-trust.json"
denied "create a bounded role outside the allowed names" aws iam create-role --role-name other-negtest --assume-role-policy-document "file://$WORK/runtime-trust.json" --permissions-boundary "$BOUNDARY"
denied "remove a role's boundary" aws iam delete-role-permissions-boundary --role-name viharbot-kepler-test-neg
denied "swap a role's boundary" aws iam put-role-permissions-boundary --role-name viharbot-kepler-test-neg --permissions-boundary arn:aws:iam::aws:policy/AdministratorAccess
denied "create an IAM user" aws iam create-user --user-name viharbot-negtest-user
denied "create a new boundary policy version" aws iam create-policy-version --policy-arn "$BOUNDARY" --policy-document "file://$WORK/boundary-policy.json" --set-as-default
denied "create an untagged AgentCore runtime" aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name viharbot_negtest --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ACCT.dkr.ecr.us-west-2.amazonaws.com/viharbot/kepler:none\"}}" --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-runtime" --network-configuration networkMode=PUBLIC
denied "create a DynamoDB table outside the namespace" aws dynamodb create-table --table-name other-negtest --attribute-definitions AttributeName=k,AttributeType=S --key-schema AttributeName=k,KeyType=HASH --billing-mode PAY_PER_REQUEST
denied "create a secret outside viharbot/kepler/" aws secretsmanager create-secret --name other/negtest --secret-string placeholder
denied "create an ECR repository outside viharbot/kepler" aws ecr create-repository --repository-name other/negtest
denied "create a Lambda outside the namespace" aws lambda create-function --function-name other-negtest --runtime python3.12 --role "arn:aws:iam::$ACCT:role/viharbot-kepler-test-neg" --handler x.y --zip-file "fileb://$WORK/empty.zip"
denied "rewrite the deployer's own trust policy" aws iam update-assume-role-policy --role-name viharbot-kepler-deployer --policy-document "file://$WORK/deployer-trust.json"
denied "tag the deployer role" aws iam tag-role --role-name viharbot-kepler-deployer --tags Key=probe,Value=1
denied "delete the deployer role" aws iam delete-role --role-name viharbot-kepler-deployer
denied "modify the deployer's own permissions" aws iam put-role-policy --role-name viharbot-kepler-deployer --policy-name viharbot-kepler-deployer --policy-document "file://$WORK/boundary-policy.json"
denied "rewrite the SSO administrator role's trust" aws iam update-assume-role-policy --role-name "$SSO_ROLE" --policy-document "file://$WORK/deployer-trust.json"
denied "pass a non-Kepler role to AgentCore" aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name viharbot_negtest2 --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ACCT.dkr.ecr.us-west-2.amazonaws.com/viharbot/kepler:none\"}}" --role-arn "arn:aws:iam::$ACCT:role/aws-reserved/sso.amazonaws.com/$SSO_ROLE" --network-configuration networkMode=PUBLIC --tags project=viharbot,agent=kepler
denied "assume the SSO administrator role" aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/aws-reserved/sso.amazonaws.com/$SSO_ROLE" --role-session-name auth-test-xx

echo "--- cleanup"
allowed "delete the test role" aws iam delete-role --role-name viharbot-kepler-test-neg

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
