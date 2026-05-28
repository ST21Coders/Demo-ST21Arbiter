# ARBITER Agents (Bedrock AgentCore)

```
agents/
├── master_orchestrator/     ← entrypoint; fans out to 3 specialists
├── sharepoint_specialist/   ← retrieves SharePoint policy docs from KB
├── awsconfig_specialist/    ← queries live AWS Config rules + compliance
└── zscaler_specialist/      ← KB retrieval (+ optional live ZIA API)
```

Each agent is a Strands `Agent` wrapped in the AgentCore Runtime entrypoint
(`bedrock_agentcore.runtime.BedrockAgentCoreApp`). It listens on port 8080
inside its container, accepting `/invocations` POSTs with `{"prompt": "..."}`.

## End-to-end deploy

1. **Infrastructure** — already-deployed CFN stacks plus the new AgentCore stack:
   ```bash
   cd Infra && ./deploy.sh
   ```
   This now includes `09-agentcore` (IAM role, security group, ECR repos for
   sharepoint/awsconfig specialists — the master and zscaler repos come from
   `05-compute`).

2. **Knowledge Base** — one-time setup:
   ```bash
   pip install -r scripts/requirements.txt
   python scripts/setup_bedrock_kb.py
   # Note the knowledgeBaseId and guardrailId from the output
   ```

3. **Build + push agent images and create runtimes**:
   ```bash
   KB_ID=ABCDEFGHIJ GUARDRAIL_ID=xxxxxx python scripts/deploy_agents.py
   ```
   The script:
   - Builds each Docker image (linux/arm64) from `agents/<name>/Dockerfile`
   - Pushes to its ECR repo
   - Creates a Bedrock AgentCore Runtime per agent, VPC-attached to the
     project's private subnets, behind the AgentCore security group
   - Wires the master orchestrator's environment with the specialist runtime
     ARNs so it can call them

## Invoking the master orchestrator

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT>:runtime/dev_st21arbiter_poc_master_orchestrator-<ID> \
  --payload "$(echo -n '{"prompt":"What conflicts exist between SharePoint URL policy and Zscaler allowlist for github.com?"}' | base64)" \
  /dev/stdout
```

The orchestrator will fan out to `sharepoint_lookup`, `awsconfig_lookup`,
`zscaler_lookup` (each calling its specialist runtime), then synthesize a
final answer.

## Local iteration

Each agent can be run locally:
```bash
cd agents/sharepoint_specialist
pip install -r requirements.txt
KB_ID=... python agent.py        # listens on :8080
# In another shell:
curl -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"What does the remote work policy say about VPN?"}'
```
