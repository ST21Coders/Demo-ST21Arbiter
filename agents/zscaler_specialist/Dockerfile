FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .

EXPOSE 8080
# opentelemetry-instrument wraps the process so the AWS OTel distro
# auto-instruments boto3, requests, and the AgentCore SDK. Traces flow
# to the Console's AgentCore Observability tab.
CMD ["opentelemetry-instrument", "python", "agent.py"]
