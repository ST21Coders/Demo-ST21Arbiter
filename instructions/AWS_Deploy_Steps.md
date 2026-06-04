# ARBITER — CI/CD Setup (CodePipeline + CodeBuild on merge to `main`)

This runbook sets up **automatic deployment**: when code is merged to `main` on GitHub
(`ST21Coders/Demo-ST21Arbiter`) via a PR, **AWS CodePipeline** triggers, **AWS CodeBuild**
runs the existing [`Infra/deploy.sh`](../Infra/deploy.sh), and a **set of team members** are
emailed the result.

> **Why no CodeDeploy?** CodeDeploy deploys to EC2/ASG, ECS, or shifts Lambda traffic via an
> `appspec.yml`. ARBITER has none of those as deploy targets — it ships entirely via
> CloudFormation/SAM stacks, AgentCore runtimes, and an S3+CloudFront UI sync, all orchestrated
> by `deploy.sh`. The correct AWS-native pattern is **CodePipeline → CodeBuild → SNS email**.
> CodeDeploy is intentionally omitted.

---

## Architecture

```
GitHub (PR merged → push to main)
      │   AWS Connector for GitHub  (existing CodeConnections connection, by ARN)
      ▼
CodePipeline V2
  ├─ Source stage : CodeStarSourceConnection → repo zip → S3 artifact bucket
  └─ Deploy stage : CodeBuild "deploy" project → runs  `cd Infra && ./deploy.sh`
                          │                              (9 CFN/SAM stacks + UI publish)
                          ▼
        CodeStar Notification rule  →  SNS topic  →  3 email subscribers
            (pipeline Succeeded / Failed / Canceled)
```

**Scope:** the pipeline runs `deploy.sh` only — the application infra stacks + UI publish.
Since [`params/dev.json`](../Infra/params/dev.json) already has `KbId` and
`MasterAgentRuntimeArn` populated, this is the normal "second-pass" deploy. The one-time
Bedrock KB ([`setup_bedrock_kb.py`](../scripts/setup_bedrock_kb.py)) and agent image builds
([`deploy_agents.py`](../scripts/deploy_agents.py)) remain **manual / out of band**.

**Account / region:** `669810405473` / `us-east-1`.

---

## What gets created

| Resource | Logical (in template) | Notes |
|---|---|---|
| Artifact S3 bucket | `ArtifactBucket` | `dev-st21arbiter-poc-cicd-artifacts`, encrypted, private. |
| GitHub connection | *(reused, not created)* | An **existing, already-authorized** CodeConnections connection is passed by ARN via the `GitHubConnectionArn` parameter. |
| CodeBuild project | `DeployProject` | `aws/codebuild/standard:7.0` (AWS CLI v2 + SAM CLI), 60-min timeout, runs `buildspec.yml`. |
| CodeBuild role | `CodeBuildServiceRole` | **AdministratorAccess (dev tradeoff — see note).** |
| Pipeline (V2) | `Pipeline` | Push trigger on `main`. |
| Pipeline role | `PipelineServiceRole` | Use connection, artifact S3 RW, start CodeBuild. |
| SNS topic + 3 subs | `NotificationTopic`, `EmailSub0..2` | Email to the three recipients. |
| Notification rule | `PipelineNotificationRule` | Pipeline success/failure/cancel → SNS. |

Files in the repo backing this:
- [`buildspec.yml`](../buildspec.yml) — runs `cd Infra && ./deploy.sh`.
- [`Infra/templates/12-cicd-pipeline.yaml`](../Infra/templates/12-cicd-pipeline.yaml) — the stack.

> **IAM note (read this):** `deploy.sh` provisions VPC/KMS/IAM/Cognito/S3/DDB/Lambda/API GW/ECR.
> The CFN stacks run under the bootstrap **AdministratorAccess** CFN service role, but the SAM
> stacks (`05/06/11`) run `sam deploy` **without** `--role-arn`, so they create resources under
> the **CodeBuild role**. For this dev demo the CodeBuild role is therefore granted
> `AdministratorAccess`, mirroring the project's existing "admin runs deploy.sh" model. Scope it
> down before any prod use — the inline comment in the template marks the spot.

---

## One-time setup

All commands assume repo root and `us-east-1`.

### 1. Store the demo password secret

`deploy.sh` resets the 4 Cognito demo-user passwords from `DEMO_PASSWORD`; `buildspec.yml`
injects it from Secrets Manager.

```bash
aws secretsmanager create-secret \
  --name dev-st21arbiter-poc-demo-password \
  --secret-string '<YOUR_DEMO_PASSWORD>' \
  --region us-east-1
```

> Password policy: 14+ chars, with upper, lower, digit, and symbol.

### 2. Deploy the pipeline stack

Validate first, then deploy. (This stack is deliberately **not** part of `deploy.sh`/
`destroy.sh` — deploy it independently, once.)

```bash
aws cloudformation validate-template \
  --template-body file://Infra/templates/12-cicd-pipeline.yaml \
  --region us-east-1

aws cloudformation deploy \
  --template-file Infra/templates/12-cicd-pipeline.yaml \
  --stack-name dev-st21arbiter-poc-12-cicd \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
    'GitHubConnectionArn=arn:aws:codeconnections:us-east-1:669810405473:connection/9521a437-07ea-4202-99cd-9c002dc51f0f'
```

> **`GitHubConnectionArn` is required** — this stack reuses an existing, already-authorized
> CodeConnections connection (the default in the template is the account's current connection).
> It does **not** create or authorize a connection. To override the notification recipients at
> the same time, append:
> ```bash
>     'NotificationEmails=a@x.com,b@x.com,c@x.com'
> ```
> The template wires exactly **three** email subscriptions. To add/remove recipients, edit the
> `EmailSub*` resources in the template and redeploy.

### 3. Confirm the GitHub connection is usable

No authorization step is needed — the connection already exists and is authorized. Just verify
it is `AVAILABLE` before the first run:

```bash
aws codeconnections get-connection \
  --connection-arn 'arn:aws:codeconnections:us-east-1:669810405473:connection/9521a437-07ea-4202-99cd-9c002dc51f0f' \
  --region us-east-1 --query 'Connection.ConnectionStatus' --output text   # → AVAILABLE
```

> If you ever need a brand-new connection instead: Console → Developer Tools → Settings →
> Connections → **Create connection** → authorize the *AWS Connector for GitHub* app on the
> `ST21Coders` org, then pass the new ARN via `GitHubConnectionArn`.

### 4. Commit `buildspec.yml`

`buildspec.yml` must be on `main` (it already lives at the repo root). Merge it if not yet
present — the CodeBuild action reads it from the source artifact.

### 5. Confirm the email subscriptions

Each of the three recipients receives an **"AWS Notification — Subscription Confirmation"**
email and must click **Confirm subscription**. Until confirmed, that address gets nothing.

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn "$(aws cloudformation describe-stacks \
      --stack-name dev-st21arbiter-poc-12-cicd --region us-east-1 \
      --query 'Stacks[0].Outputs[?OutputKey==`NotificationTopicArn`].OutputValue' --output text)" \
  --region us-east-1 --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

---

## Verify end-to-end

1. **Open a trivial PR** (e.g. a one-line change to `README`) and **merge it to `main`**.
2. **Confirm the pipeline starts** within ~1 minute:
   ```bash
   aws codepipeline list-pipeline-executions \
     --pipeline-name dev-st21arbiter-poc-pipeline --region us-east-1 \
     --max-items 3 --query 'pipelineExecutionSummaries[].[status,startTime]' --output table
   ```
3. **Watch the build logs** (CodeBuild console or `aws codebuild batch-get-builds`). A healthy
   run ends with `ARBITER deployment complete (dev)` plus the API and UI URLs.
4. **No-op safety:** a merge with no infra changes still succeeds — change-sets report
   "No changes detected" and skip each unchanged stack.
5. **Notifications:** the three recipients receive a **success** email. To test failure, push a
   deliberately broken template on a throwaway branch, merge it, and confirm a **failure** email
   arrives (then revert).

---

## Operational notes & troubleshooting

- **Pipeline doesn't trigger on merge** → connection not `AVAILABLE` (Step 3), or the push
  landed on a branch other than `main`. A PR *merge commit* on `main` is a push to `main`.
- **`CLIENT_ERROR: secret not found`** in the build → Step 1 secret missing or named
  differently than `DemoPasswordSecretName` / the `secrets-manager` key in `buildspec.yml`.
- **`PipelineNotificationRule` CREATE_FAILED "Invalid request provided"** → the SNS topic policy
  must precede the notification rule; ensured via `DependsOn: NotificationTopicPolicy` on the
  rule. A `ROLLBACK_FAILED` stack from this must be **deleted** (not updated) before redeploy.
- **Build fails on `iam:PassRole` / access denied** → the CodeBuild role lost AdministratorAccess
  (it deploys the SAM stacks directly). See the IAM note above.
- **Concurrent merges** → two executions could run `deploy.sh` on the same stacks at once and
  collide on change-sets. Mitigate by enabling *Superseded* execution mode on the pipeline (or
  pause-and-queue) in the console if merges are frequent.
- **Python 3.12 in the build image** vs the repo's 3.13 target is fine — only deploy-time
  stdlib + boto3 code runs in CI.
- **Cost** — one CodeConnections connection, CodeBuild minutes, and SNS email. Negligible for a
  demo.
- **Teardown** — delete the CI/CD stack separately; it is not removed by `destroy.sh`:
  ```bash
  aws cloudformation delete-stack --stack-name dev-st21arbiter-poc-12-cicd --region us-east-1
  ```
  (Empty `dev-st21arbiter-poc-cicd-artifacts` first if the delete blocks on a non-empty bucket.)
