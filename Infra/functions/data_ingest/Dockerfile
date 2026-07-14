# Container-image Lambda for the data-ingest worker.
#
# Build CONTEXT is the repo root (set via the SAM Metadata DockerContext in
# Infra/templates/13-data-ingest.yaml) so we can COPY the shared arbiter_rag library
# from rag_src/ — the same single source of truth the notebooks and agents use.
#
# Zip packaging can't hold pandas + pyarrow (Linux binary wheels blow past the 250 MB
# unzipped limit), and a host `sam build` on macOS would bundle Mac wheels that fail on
# Lambda's Linux runtime. A container image built from the AWS Lambda base sidesteps both.
FROM public.ecr.aws/lambda/python:3.13

# ${LAMBDA_TASK_ROOT} is the working dir / import root for the base image.
COPY Infra/functions/data_ingest/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Reusable RAG library (chunking/embeddings/S3 Vectors/loaders/serialization/ingest).
COPY rag_src/arbiter_rag ./arbiter_rag
COPY Infra/functions/data_ingest/handler.py ./

CMD ["handler.handler"]
