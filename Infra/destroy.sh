#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# ARBITER – Destroy all CloudFormation / SAM stacks in reverse order.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ENV="${ENVIRONMENT:-dev}"
PARAMS_FILE="params/${ENV}.json"
PROJECT=$(python3 -c "import json; print(next(p['ParameterValue'] for p in json.load(open('${PARAMS_FILE}')) if p.get('ParameterKey')=='ProjectName'))")
STACK_PREFIX="${ENV}-${PROJECT}"
SAM_BUCKET="${STACK_PREFIX}-cfn-templates"

# Reverse order of deployment (bootstrap last — it owns the deployer user + CF role)
STACKS=(
  # "08-observability"
  "07-bedrock"          # S3-Vectors KB + unstructured bucket (deletes vector bucket + index)
  "06-api"
  "05-compute"
  "04-storage"
  "03-identity"
  "02-security"
  "01-network"
  "00-bootstrap"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

delete_stack() {
  local stack_name="${STACK_PREFIX}-${1}"

  local stack_status
  stack_status=$(aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --region "${REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  if [ "${stack_status}" = "DOES_NOT_EXIST" ]; then
    log "  ⊘ ${stack_name} does not exist, skipping"
    return 0
  fi

  log "──── Deleting ${stack_name} (status: ${stack_status}) ────"

  aws cloudformation delete-stack \
    --stack-name "${stack_name}" \
    --region "${REGION}"

  log "  Waiting for deletion..."
  aws cloudformation wait stack-delete-complete \
    --stack-name "${stack_name}" \
    --region "${REGION}"

  log "  ✓ ${stack_name} deleted"
}

empty_and_delete_bucket() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" 2>/dev/null; then
    log "  Emptying bucket: ${bucket}"
    aws s3 rm "s3://${bucket}" --recursive --region "${REGION}" 2>/dev/null || true
    log "  Deleting bucket: ${bucket}"
    aws s3api delete-bucket --bucket "${bucket}" --region "${REGION}" 2>/dev/null || true
  fi
}

main() {
  log "Starting ARBITER teardown — env=${ENV}, region=${REGION}"
  log ""
  log "WARNING: This will delete ALL ARBITER stacks and data in ${ENV}."
  read -rp "Type 'yes' to confirm: " confirm
  if [ "${confirm}" != "yes" ]; then
    log "Aborted."
    exit 1
  fi
  log ""

  # Empty S3 buckets that have Object Lock / versioning (must be emptied before stack delete)
  log "──── Pre-cleanup: emptying S3 buckets ────"
  # Note: the S3 Vectors bucket (${STACK_PREFIX}-vectors-kb) is NOT a regular S3
  # bucket — it is emptied/deleted by the 07-bedrock stack delete (index + bucket),
  # not by aws s3 rm. The -unstructured docs bucket is PRE-EXISTING (created
  # out-of-band, not owned by any stack) so it is intentionally left untouched
  # here — delete it manually if teardown should remove it.
  for bucket in "${STACK_PREFIX}-raw" "${STACK_PREFIX}-processed" "${STACK_PREFIX}-cloudtrail"; do
    empty_and_delete_bucket "${bucket}" || true
  done

  for stack in "${STACKS[@]}"; do
    delete_stack "${stack}"
  done

  # Clean up SAM deployment bucket
  empty_and_delete_bucket "${SAM_BUCKET}" || true

  log ""
  log "════════════════════════════════════════════════════════"
  log "  ARBITER teardown complete (${ENV})"
  log "════════════════════════════════════════════════════════"
}

main "$@"
