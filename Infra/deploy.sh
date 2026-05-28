#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# ARBITER – Deploy all CloudFormation / SAM stacks in order.
# Uses change-sets for safety (as per infra-scripts-guide).
# ─────────────────────────────────────────────────────────────
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"
PARAMS_FILE="params/${ENV}.json"
# ProjectName must match params/<env>.json — the bootstrap stack S3 bucket
# (<env>-<project>-cfn-templates) is globally unique, so the params file is
# authoritative. Read from it instead of hardcoding, to avoid the two drifting.
PROJECT=$(python3 -c "import json; print(next(p['ParameterValue'] for p in json.load(open('${PARAMS_FILE}')) if p.get('ParameterKey')=='ProjectName'))")
TEMPLATE_DIR="templates"
STACK_PREFIX="${ENV}-${PROJECT}"
BOOTSTRAP_STACK="${STACK_PREFIX}-00-bootstrap"

# SAM stacks need a deployment bucket
SAM_BUCKET="${STACK_PREFIX}-cfn-templates"

# Ordered list of stacks (plain CF first, then SAM)
CF_STACKS=(
  "01-network"
  "02-security"
  "03-identity"
  "04-storage"
)
SAM_STACKS=(
  "05-compute"
  "06-api"         
)
CF_STACKS_POST=(
  # "07-bedrock"       # deferred: KB requires OpenSearch index pre-creation; see scripts/setup_bedrock_kb.py
  # "08-observability" # deferred: dashboards/alarms — revisit after AgentCore is up
  "09-agentcore"       # IAM role + SG + ECR repos for AgentCore Runtime
  "10-ui-hosting"      # S3 + CloudFront SPA distribution (WAF gated by AttachWaf)
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ──────────────────────────── Filter params for template ──────
# Each template defines different parameters. This function reads
# the template YAML, extracts its parameter names, and writes a
# filtered copy of the master params JSON so CF doesn't reject
# unknown parameters.
filter_params() {
  local template="$1"
  local output="$2"
  python3 -c "
import json, re, sys
with open(sys.argv[1]) as f:
    content = f.read()
m = re.search(r'^Parameters:\n(.*?)(?=^\w)', content, re.MULTILINE | re.DOTALL)
valid = set(re.findall(r'^  (\w+):', m.group(1), re.MULTILINE)) if m else set()
allowed_keys = {'ParameterKey', 'ParameterValue', 'UsePreviousValue', 'ResolvedValue'}
params = json.load(open(sys.argv[2]))
filtered = [
    {k: v for k, v in p.items() if k in allowed_keys}
    for p in params if p.get('ParameterKey') in valid
]
with open(sys.argv[3], 'w') as out:
    json.dump(filtered, out)
" "${template}" "${PARAMS_FILE}" "${output}"
}

# ──────────────────────────── Deploy bootstrap (admin) ────────
deploy_bootstrap() {
  local stack_status
  stack_status=$(aws cloudformation describe-stacks \
    --stack-name "${BOOTSTRAP_STACK}" \
    --region "${REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  # A failed bootstrap has no usable outputs. With Retain removed from the
  # bootstrap resources, rollback now cleans up after itself — so we can
  # safely delete the failed stack and let the CREATE branch re-run it.
  if [[ "${stack_status}" == "ROLLBACK_COMPLETE" || "${stack_status}" == "CREATE_FAILED" ]]; then
    log "  Bootstrap stack is in ${stack_status} — deleting before re-creation..."
    aws cloudformation delete-stack --stack-name "${BOOTSTRAP_STACK}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${BOOTSTRAP_STACK}" --region "${REGION}"
    stack_status="DOES_NOT_EXIST"
  fi

  if [ "${stack_status}" = "DOES_NOT_EXIST" ]; then
    log "──── Deploying bootstrap stack (creates deployer user + CF service role) ────"
    aws cloudformation create-stack \
      --stack-name "${BOOTSTRAP_STACK}" \
      --template-body "file://${TEMPLATE_DIR}/00-bootstrap.yaml" \
      --parameters \
        ParameterKey=Environment,ParameterValue="${ENV}" \
        ParameterKey=ProjectName,ParameterValue="${PROJECT}" \
      --capabilities CAPABILITY_NAMED_IAM \
      --region "${REGION}" \
      --tags Key=Environment,Value="${ENV}" Key=Project,Value="${PROJECT}"
    log "  Waiting for bootstrap stack creation..."
    aws cloudformation wait stack-create-complete \
      --stack-name "${BOOTSTRAP_STACK}" \
      --region "${REGION}"
    log "  Bootstrap stack created."
    log ""
    log "  *** DEPLOYER CREDENTIALS ***"
    log "  Retrieve the access key from stack outputs:"
    log "    aws cloudformation describe-stacks --stack-name ${BOOTSTRAP_STACK} --region ${REGION} --query 'Stacks[0].Outputs'"
    log "  Configure them in ~/.aws/credentials or CI/CD, then re-run deploy.sh."
    log ""
  else
    log "  Bootstrap stack already exists (status: ${stack_status})"
  fi

  # Retrieve the CF service role ARN for subsequent stacks
  CFN_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${BOOTSTRAP_STACK}" \
    --region "${REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`CloudFormationServiceRoleArn`].OutputValue | [0]' \
    --output text)

  if [[ -z "${CFN_ROLE_ARN}" || "${CFN_ROLE_ARN}" == "None" || "${CFN_ROLE_ARN}" != arn:* ]]; then
    log "ERROR: Could not resolve CloudFormationServiceRoleArn from ${BOOTSTRAP_STACK}."
    log "       Got: '${CFN_ROLE_ARN}'"
    log "       Check the bootstrap stack outputs:"
    log "         aws cloudformation describe-stacks --stack-name ${BOOTSTRAP_STACK} --region ${REGION} --query 'Stacks[0].Outputs'"
    exit 1
  fi
  log "  CF Service Role: ${CFN_ROLE_ARN}"
}

# ──────────────────────────── Deploy CF stack via change-set ──
deploy_cf_stack() {
  local stack_name="${STACK_PREFIX}-${1}"
  local template="${TEMPLATE_DIR}/${1}.yaml"
  local changeset_name="cs-$(date '+%Y%m%d%H%M%S')"

  log "──── Deploying ${stack_name} ────"

  # Filter params to only those defined in this template
  local filtered_params
  filtered_params=$(mktemp)
  filter_params "${template}" "${filtered_params}"
  trap "rm -f ${filtered_params}" RETURN

  # Check if stack exists
  local stack_status
  stack_status=$(aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --region "${REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  # Handle failed/stuck stacks that need cleanup before re-creation.
  # REVIEW_IN_PROGRESS: change-set was created but never executed (e.g. validation failed).
  if [[ "${stack_status}" == "ROLLBACK_COMPLETE" \
     || "${stack_status}" == "DELETE_FAILED" \
     || "${stack_status}" == "REVIEW_IN_PROGRESS" \
     || "${stack_status}" == "CREATE_FAILED" ]]; then
    log "  Stack is in ${stack_status} — deleting before re-creation..."
    aws cloudformation delete-stack --stack-name "${stack_name}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${stack_name}" --region "${REGION}"
    stack_status="DOES_NOT_EXIST"
  fi

  if [[ "${stack_status}" == *"IN_PROGRESS"* ]]; then
    log "  Stack is in ${stack_status} — waiting for it to settle..."
    aws cloudformation wait stack-rollback-complete --stack-name "${stack_name}" --region "${REGION}" 2>/dev/null || \
    aws cloudformation wait stack-create-complete --stack-name "${stack_name}" --region "${REGION}" 2>/dev/null || \
    aws cloudformation wait stack-update-complete --stack-name "${stack_name}" --region "${REGION}" 2>/dev/null || true
    # Re-check status after waiting
    stack_status=$(aws cloudformation describe-stacks \
      --stack-name "${stack_name}" --region "${REGION}" \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
    if [ "${stack_status}" = "ROLLBACK_COMPLETE" ]; then
      log "  Rollback completed — deleting failed stack..."
      aws cloudformation delete-stack --stack-name "${stack_name}" --region "${REGION}"
      aws cloudformation wait stack-delete-complete --stack-name "${stack_name}" --region "${REGION}"
      stack_status="DOES_NOT_EXIST"
    fi
  fi

  local changeset_type="UPDATE"
  if [ "${stack_status}" = "DOES_NOT_EXIST" ]; then
    changeset_type="CREATE"
  fi

  log "  Stack status: ${stack_status} → change-set type: ${changeset_type}"

  # Create change set (using CF service role from bootstrap)
  aws cloudformation create-change-set \
    --stack-name "${stack_name}" \
    --template-body "file://${template}" \
    --parameters "file://${filtered_params}" \
    --change-set-name "${changeset_name}" \
    --change-set-type "${changeset_type}" \
    --role-arn "${CFN_ROLE_ARN}" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --region "${REGION}" \
    --tags Key=Environment,Value="${ENV}" Key=Project,Value="${PROJECT}"

  log "  Waiting for change-set to be created..."
  aws cloudformation wait change-set-create-complete \
    --stack-name "${stack_name}" \
    --change-set-name "${changeset_name}" \
    --region "${REGION}" 2>/dev/null || true

  # Check if the changeset has actual changes (UPDATE with no diff → FAILED status)
  local cs_status
  cs_status=$(aws cloudformation describe-change-set \
    --stack-name "${stack_name}" \
    --change-set-name "${changeset_name}" \
    --region "${REGION}" \
    --query 'Status' --output text 2>/dev/null)

  if [ "${cs_status}" = "FAILED" ]; then
    local cs_reason
    cs_reason=$(aws cloudformation describe-change-set \
      --stack-name "${stack_name}" \
      --change-set-name "${changeset_name}" \
      --region "${REGION}" \
      --query 'StatusReason' --output text 2>/dev/null)
    if [[ "${cs_reason}" == *"didn't contain changes"* || "${cs_reason}" == *"No updates"* ]]; then
      log "  ⊘ No changes detected — skipping ${stack_name}"
      aws cloudformation delete-change-set \
        --stack-name "${stack_name}" \
        --change-set-name "${changeset_name}" \
        --region "${REGION}" 2>/dev/null || true
      return 0
    else
      log "  ERROR: Change-set failed: ${cs_reason}"
      return 1
    fi
  fi

  # Log changes
  log "  Planned changes:"
  aws cloudformation describe-change-set \
    --stack-name "${stack_name}" \
    --change-set-name "${changeset_name}" \
    --region "${REGION}" \
    --query 'Changes[].{Action:ResourceChange.Action,LogicalId:ResourceChange.LogicalResourceId,Type:ResourceChange.ResourceType}' \
    --output table

  # Execute change set
  aws cloudformation execute-change-set \
    --stack-name "${stack_name}" \
    --change-set-name "${changeset_name}" \
    --region "${REGION}"

  log "  Waiting for stack $(echo "${changeset_type}" | tr '[:upper:]' '[:lower:]') to complete..."
  if [ "${changeset_type}" = "CREATE" ]; then
    aws cloudformation wait stack-create-complete \
      --stack-name "${stack_name}" \
      --region "${REGION}"
  else
    aws cloudformation wait stack-update-complete \
      --stack-name "${stack_name}" \
      --region "${REGION}"
  fi

  log "  ✓ ${stack_name} deployed successfully"
}

# ──────────────────────────── Deploy SAM stack ────────────────
deploy_sam_stack() {
  local stack_name="${STACK_PREFIX}-${1}"
  local template="${TEMPLATE_DIR}/${1}.yaml"

  log "──── Building & deploying SAM stack: ${stack_name} ────"

  # sam deploy can't update a stack in ROLLBACK_COMPLETE / failed states.
  # Mirror the auto-recovery logic from deploy_cf_stack.
  local stack_status
  stack_status=$(aws cloudformation describe-stacks \
    --stack-name "${stack_name}" --region "${REGION}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
  if [[ "${stack_status}" == "ROLLBACK_COMPLETE" \
     || "${stack_status}" == "DELETE_FAILED" \
     || "${stack_status}" == "REVIEW_IN_PROGRESS" \
     || "${stack_status}" == "CREATE_FAILED" ]]; then
    log "  Stack is in ${stack_status} — deleting before re-creation..."
    aws cloudformation delete-stack --stack-name "${stack_name}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${stack_name}" --region "${REGION}"
  fi

  sam build \
    --template-file "${template}" \
    --build-dir ".aws-sam/build-${1}"

  sam deploy \
    --template-file ".aws-sam/build-${1}/template.yaml" \
    --stack-name "${stack_name}" \
    --s3-bucket "${SAM_BUCKET}" \
    --s3-prefix "${stack_name}" \
    --region "${REGION}" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --parameter-overrides "Environment=${ENV}" "ProjectName=${PROJECT}" \
    --tags "Environment=${ENV}" "Project=${PROJECT}" \
    --no-fail-on-empty-changeset \
    --no-confirm-changeset

  log "  ✓ ${stack_name} deployed successfully"
}

# ──────────────────────────── Set demo passwords ─────────────
# After 03-identity creates the 4 demo users (FORCE_CHANGE_PASSWORD state),
# we set a permanent password on each so the team can sign in immediately.
# Requires DEMO_PASSWORD to be exported by the operator — never embedded
# in params/dev.json or committed.
set_demo_passwords() {
  if [[ -z "${DEMO_PASSWORD:-}" ]]; then
    log "  ⚠ DEMO_PASSWORD not set — skipping password setup."
    log "    Set passwords by hand later with:"
    log "      aws cognito-idp admin-set-user-password --user-pool-id <pool> --username <email> --password '<pw>' --permanent --region ${REGION}"
    return 0
  fi

  local user_pool_id
  user_pool_id=$(aws cloudformation list-exports --region "${REGION}" \
    --query "Exports[?Name=='${STACK_PREFIX}-UserPoolId'].Value" \
    --output text 2>/dev/null || echo "")
  if [[ -z "${user_pool_id}" ]]; then
    log "  ⚠ Could not resolve UserPoolId export — skipping password setup."
    return 0
  fi

  log "──── Setting permanent passwords on the 4 demo users ────"
  # Email-as-username; keep this list in lockstep with the four UserPoolUser
  # resources in Infra/templates/03-identity.yaml.
  for email in \
      "emp_sarah@meridianinsurance.com" \
      "grc_priya@meridianinsurance.com" \
      "soc_marcus@meridianinsurance.com" \
      "ciso_diana@meridianinsurance.com"; do
    log "  → ${email}"
    aws cognito-idp admin-set-user-password \
      --user-pool-id "${user_pool_id}" \
      --username "${email}" \
      --password "${DEMO_PASSWORD}" \
      --permanent \
      --region "${REGION}" >/dev/null
  done
  log "  ✓ Demo passwords set."
}

# ──────────────────────────── Main ────────────────────────────
main() {
  log "Starting ARBITER deployment — env=${ENV}, region=${REGION}"
  log ""

  deploy_bootstrap

  for stack in "${CF_STACKS[@]}"; do
    deploy_cf_stack "${stack}"
  done

  for stack in "${SAM_STACKS[@]}"; do
    deploy_sam_stack "${stack}"
  done

  for stack in "${CF_STACKS_POST[@]}"; do
    deploy_cf_stack "${stack}"
  done

  # Set the shared demo password on the 4 persona users created by 03-identity.
  set_demo_passwords

  # Build the UI, push to the new CloudFront-fronted bucket, and patch the
  # Cognito client with the CloudFront callback URL. Idempotent — re-runs OK.
  if [[ -f post_deploy_ui.py ]]; then
    log "──── Publishing UI to CloudFront ────"
    AWS_REGION="${REGION}" ENVIRONMENT="${ENV}" PROJECT="${PROJECT}" \
      python3 post_deploy_ui.py
  fi

  log ""
  log "════════════════════════════════════════════════════════"
  log "  ARBITER deployment complete (${ENV})"
  log "════════════════════════════════════════════════════════"

  # Print API endpoint
  API_ENDPOINT=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_PREFIX}-06-api" \
    --region "${REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
    --output text 2>/dev/null || echo "N/A")
  log "  API Endpoint  : ${API_ENDPOINT}"

  # Print the SPA URL (CloudFront — share with team members).
  UI_URL=$(aws cloudformation list-exports --region "${REGION}" \
    --query "Exports[?Name=='${STACK_PREFIX}-UIBaseURL'].Value" \
    --output text 2>/dev/null || echo "N/A")
  log "  UI URL        : ${UI_URL}"
}

main "$@"
