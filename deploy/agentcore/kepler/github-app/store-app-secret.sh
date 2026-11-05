#!/bin/bash
# Store a GitHub App's id, installation id and private key in AWS Secrets Manager.
#
#   bash deploy/agentcore/kepler/github-app/store-app-secret.sh <reader|writer> <app_id> <installation_id> <key.pem> [--delete-pem]
#
# Run from the repository root with your administrator login. The secret must already exist
# (created empty by the deployer). The key is read from the file, written to the secret as JSON
# through a private temporary file, and never printed. The temporary file is removed on exit.
# With --delete-pem the original .pem is deleted after a successful store.
set -u
export AWS_PAGER=""
REGION="${AWS_REGION:-us-west-2}"
ROLE="${1:-}"; APP_ID="${2:-}"; INSTALL_ID="${3:-}"; PEM="${4:-}"; FLAG="${5:-}"

case "$ROLE" in
  reader) SECRET="viharbot/kepler/github-app-reader" ;;
  writer) SECRET="viharbot/kepler/github-app-alert-writer" ;;
  *) echo "usage: $0 <reader|writer> <app_id> <installation_id> <key.pem> [--delete-pem]"; exit 2 ;;
esac
[[ "$APP_ID" =~ ^[0-9]+$ ]]     || { echo "app_id must be a number"; exit 2; }
[[ "$INSTALL_ID" =~ ^[0-9]+$ ]] || { echo "installation_id must be a number"; exit 2; }
[ -f "$PEM" ]                   || { echo "key file not found: $PEM"; exit 2; }
head -n 1 "$PEM" | grep -q "BEGIN .*PRIVATE KEY" || { echo "that file does not look like a private key"; exit 2; }
[ -z "$FLAG" ] || [ "$FLAG" = "--delete-pem" ]   || { echo "unknown option: $FLAG"; exit 2; }

aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET" >/dev/null 2>&1 \
  || { echo "secret $SECRET does not exist yet (the deployer creates it empty first)"; exit 1; }

umask 077
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
python3 - "$APP_ID" "$INSTALL_ID" "$PEM" > "$WORK/secret.json" <<'PY'
import json, sys
app_id, installation_id, path = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"app_id": int(app_id), "installation_id": int(installation_id),
                  "private_key_pem": open(path).read()}))
PY
[ -s "$WORK/secret.json" ] || { echo "could not build the secret"; exit 1; }

aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET" \
  --secret-string "file://$WORK/secret.json" --query 'VersionId' --output text >/dev/null \
  || { echo "storing the secret failed; the .pem was NOT deleted"; exit 1; }
echo "stored $ROLE app secret in $SECRET (key not printed)"

if [ "$FLAG" = "--delete-pem" ]; then
  if rm -P "$PEM" 2>/dev/null || rm -f "$PEM"; then echo "deleted $PEM"; else echo "could not delete $PEM; delete it yourself"; fi
else
  echo "reminder: delete $PEM when you no longer need it"
fi
