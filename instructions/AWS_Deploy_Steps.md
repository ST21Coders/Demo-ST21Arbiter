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

## How code changes ship (push to `main`, NOT `cloudformation deploy`)

The pipeline stack (`12-cicd-pipeline.yaml`) contains **only the pipeline machinery**
(CodePipeline + CodeBuild + SNS) — **no application code**. To ship an app change (UI, Lambda,
agents, templates), you do **not** run `aws cloudformation deploy`. You commit and merge to
`main`, and the pipeline does the rest:

```
edit code → commit → merge to main on GitHub
      │  (push to main auto-triggers)
      ▼
CodePipeline → CodeBuild runs Infra/deploy.sh
      │          └─ post_deploy_ui.py: npm build → S3 sync → CloudFront invalidate
      ▼
success / failure email
```

```bash
git add <changed files>
git commit -m "your message"
git push          # to a branch → open PR → merge to main   (or push to main directly)

# watch it run:
aws codepipeline list-pipeline-executions \
  --pipeline-name dev-st21arbiter-poc-pipeline --region us-east-1 \
  --max-items 3 --query "pipelineExecutionSummaries[].[status,startTime]" --output table
```

> **`aws cloudformation deploy` on this stack only changes the pipeline itself** (e.g. add a
> stage, change the notification email list). Running it after an app-code edit correctly reports
> *"No changes to deploy"* — the template didn't change. That is expected, not an error.

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
  --secret-string '{"DEMO_PASSWORD":"<YOUR_DEMO_PASSWORD>"}' \
  --region us-east-1
```

> Password policy: 14+ chars, with upper, lower, digit, and symbol.
>
> **The secret must be JSON** (`{"DEMO_PASSWORD":"..."}`) to match `buildspec.yml`'s
> `DEMO_PASSWORD: dev-st21arbiter-poc-demo-password:DEMO_PASSWORD` reference (the `:DEMO_PASSWORD`
> suffix extracts that key). If you store a *plaintext* secret instead, drop the `:DEMO_PASSWORD`
> suffix in `buildspec.yml` — otherwise CodeBuild injects the whole JSON blob and `deploy.sh`
> sets the Cognito password to that literal JSON, so logins fail with "Invalid username or
> password" even though users show `CONFIRMED`.

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
- **`sam build` "Binary validation failed for python ... runtime: python3.13"** → the build
  image's active Python must match the SAM functions' `Runtime: python3.13`. `buildspec.yml`
  pins `runtime-versions.python: 3.13` for this. If a future image drops managed 3.13, either
  pin a newer image or switch `deploy.sh`'s `sam build` to `--use-container` (needs
  `PrivilegedMode: true` on the CodeBuild project).
- **Cost** — one CodeConnections connection, CodeBuild minutes, and SNS email. Negligible for a
  demo.
- **IAM guardrail on the deploy user** — the deploy identity (`sridharn@smartek21.com`) is
  explicitly denied `iam:DeleteRolePolicy` and `iam:DeletePolicy`, so it **cannot tear down**
  stacks that contain custom IAM roles/policies — delete stalls in `DELETE_FAILED`. Creates are
  fine. **Always pass the bootstrap CFN service role on deletes** (it has AdministratorAccess and
  the user may `PassRole` it):
  ```bash
  aws cloudformation delete-stack --stack-name dev-st21arbiter-poc-12-cicd \
    --role-arn arn:aws:iam::669810405473:role/dev-st21arbiter-poc-cfn-service-role \
    --region us-east-1
  ```
  (Empty `dev-st21arbiter-poc-cicd-artifacts` first if the delete blocks on a non-empty bucket.)
- **`aws cloudformation deploy` fails with `AWS::EarlyValidation::ResourceExistenceCheck`** → the
  changeset path can throw a spurious early-validation error for this stack even though all
  referenced resources exist. Use `aws cloudformation create-stack` / `update-stack` directly
  instead — it deploys cleanly and surfaces real per-resource errors in stack events.
- **Teardown** — the CI/CD stack is separate; it is not removed by `destroy.sh`. Use the
  `delete-stack --role-arn …` command above.
