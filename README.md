# ARBITER (ST21)

This is main repo, keep it clean and always in working state!!!

A multi-agent AI governance demo. A React SPA talks to a Lambda API that
orchestrates four Bedrock AgentCore Runtimes (1 master + 3 specialists),
backed by a Bedrock Knowledge Base, OpenSearch Serverless vector store, and
a Guardrail. Provisions into a single AWS account via CloudFormation/SAM.

> ⚠️ **Demo project.** Single-AZ, AttachWaf=false, shared demo password
> for the persona users. Not for production traffic.

## Quick Start

End-to-end deployment instructions:
[`instructions/DEPLOYMENT.md`](instructions/DEPLOYMENT.md)

Architecture overview + project conventions:
[`CLAUDE.md`](CLAUDE.md)

Architecture diagram (open in draw.io / diagrams.net):
[`Documents/arbiter_st21_architecture.drawio`](Documents/arbiter_st21_architecture.drawio)

## What you get after deploy

- 9 CloudFormation/SAM stacks: VPC + KMS + Cognito + S3/DDB/OSS + Lambda +
  API Gateway + AgentCore IAM + CloudFront-hosted SPA
- 4 AgentCore Runtimes on Amazon Nova 2 Lite (override via `MODEL_ID` env var)
- 4 Cognito users (one per persona) mapped to 4 Cognito groups with RBAC
- A public CloudFront URL hosting the React UI, gated by Cognito Hosted UI

| Persona | Email | Cognito group | UI pages accessible |
|---|---|---|---|
| Employee | `emp_sarah@meridianinsurance.com` | `employee` | Analyst Chat only |
| GRC Analyst | `grc_priya@meridianinsurance.com` | `grc` | Dashboard, Findings, Heatmap, Governance, Audit, Analyst Chat |
| SOC Analyst | `soc_marcus@meridianinsurance.com` | `soc` | Dashboard, Findings, Heatmap, Actions, Audit, Analyst Chat |
| CISO | `ciso_diana@meridianinsurance.com` | `ciso` | All 10 pages |

## Repo layout

| Path | Purpose |
|---|---|
| [`Infra/templates/`](Infra/templates/) | CloudFormation + SAM templates (`00-bootstrap` … `10-ui-hosting`) |
| [`Infra/functions/`](Infra/functions/) | Lambda source (`api_handler`, `processing_pipeline`) |
| [`agents/`](agents/) | Four Bedrock AgentCore agents — see [`agents/README.md`](agents/README.md) |
| [`scripts/`](scripts/) | KB setup, agent deploy, mock-data seeding |
| [`ui/`](ui/) | Vite + React 18 SPA |
| [`BaselineFiles/`](BaselineFiles/) | Synthetic policy documents — KB seed corpus |
| [`Documents/`](Documents/) | Architecture diagrams |

## Deploy

```bash
cd Infra
DEMO_PASSWORD='<choose-a-14+-char-password>' ./deploy.sh
```

Then open the `UI URL` printed at the end of the deploy and sign in with any
of the 4 persona emails above + the password you supplied.

## License

Internal demo — no public license assigned. Treat as `All rights reserved`
until a `LICENSE` file is added.
