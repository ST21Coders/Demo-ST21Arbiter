FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .
# Shared RAG library (config/embeddings/vectors/athena_sql/generation). Canonical
# source is rag_src/arbiter_rag/; merged into the build context by
# scripts/deploy_agents.py::build_and_push via this agent's "extra_pkgs" key.
# Must be COPY'd explicitly or the runtime crashes ModuleNotFoundError on import.
COPY arbiter_rag ./arbiter_rag
# Shared per-agent helpers (token-usage write path). Merged into the build
# context by scripts/deploy_agents.py::build_and_push.
COPY _shared ./_shared

EXPOSE 8080
# opentelemetry-instrument wraps the process so the AWS OTel distro auto-instruments
# boto3 and the AgentCore SDK; traces flow to the AgentCore Observability tab.
CMD ["opentelemetry-instrument", "python", "agent.py"]
