"""Configuration loading — the single place models, region, and index names resolve.

Precedence (lowest to highest):
    1. config/settings.toml            (base defaults, committed)
    2. config/<env>.yaml               (per-env application overrides; env = ARBITER_ENV)
    3. environment variables           (BEDROCK_GENERATION_MODEL_ID, AWS_REGION, ...)

To swap the generation LLM for the entire system, change `generation_model_id` in
config/settings.toml (or export BEDROCK_GENERATION_MODEL_ID). Notebooks and the
AgentCore agent both call get_settings(), so nothing else needs to change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for 3.10
    import tomli as tomllib  # type: ignore[no-redef]

# Path anchors. This package lives at `rag_src/arbiter_rag/config.py`:
#   parents[1] == rag_src/        (holds config/, eval/, notebooks/)
#   parents[2] == repo root       (holds data/Hawaii_Sample_Sales/, data/Hawaii_Electronics_100/)
# Config + eval assets resolve against RAG_ROOT; sample data lives one level up at DATA_ROOT.
RAG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = RAG_ROOT / "config"
DATA_ROOT = REPO_ROOT / "data"
VALID_ENVS = ("dev", "test", "prod")


def _read_flat_yaml(path: Path) -> dict[str, Any]:
    """Parse a deliberately-flat ``key: value`` YAML file (no nesting/lists).

    Kept dependency-free so the runtime library needs only boto3. The per-env
    config files (config/dev.yaml, ...) are authored to this restricted shape.
    """
    result: dict[str, Any] = {}
    if not path.exists():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.lower() in ("true", "false"):
            result[key] = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            result[key] = int(value)
        else:
            result[key] = value.strip('"').strip("'")
    return result


@dataclass(frozen=True)
class Settings:
    """Immutable, fully-resolved configuration for one environment."""

    env: str
    region: str
    account: str
    expected_account_id: str
    # models
    generation_model_id: str
    generation_max_tokens: int
    generation_temperature: float
    embedding_model_id: str
    embedding_dim: int
    # rerank
    rerank_enabled: bool
    rerank_model_id: str
    rerank_candidates_k: int
    rerank_top_k: int
    # retrieval
    retrieval_top_k: int
    # chunking
    chunk_strategy: str
    chunk_max_chars: int
    chunk_overlap_chars: int
    chunking_version: str
    # vectors
    vector_bucket: str
    hr_index: str
    sales_index: str
    distance_metric: str
    # sales sql
    glue_database: str
    glue_table: str
    athena_workgroup: str
    athena_output_prefix: str
    max_scanned_bytes: int
    # guardrails
    guardrails_enabled: bool
    guardrail_id: str
    guardrail_version: str
    # per-env application knobs
    ingest_batch_size: int
    log_level: str

    # --- Derived, env-suffixed resource names --------------------------------
    @property
    def vector_bucket_name(self) -> str:
        """Env-scoped S3 Vectors bucket name (unique per account+region)."""
        return f"{self.vector_bucket}-{self.env}"

    @property
    def hr_index_name(self) -> str:
        return f"{self.hr_index}-{self.env}"

    @property
    def sales_index_name(self) -> str:
        return f"{self.sales_index}-{self.env}"

    @property
    def glue_database_name(self) -> str:
        """Env-suffixed Glue database — matches infra/stacks/data_stack.py."""
        return f"{self.glue_database}_{self.env}"

    @property
    def athena_workgroup_name(self) -> str:
        """Env-suffixed Athena workgroup — matches infra/stacks/data_stack.py."""
        return f"{self.athena_workgroup}-{self.env}"

    def summary(self) -> dict[str, Any]:
        """Redacted, printable view for notebook/preflight output."""
        return {
            "env": self.env,
            "region": self.region,
            "generation_model_id": self.generation_model_id,
            "embedding_model_id": self.embedding_model_id,
            "embedding_dim": self.embedding_dim,
            "rerank": self.rerank_model_id if self.rerank_enabled else "(disabled)",
            "vector_bucket": self.vector_bucket_name,
            "hr_index": self.hr_index_name,
            "sales_index": self.sales_index_name,
            "chunk_strategy": self.chunk_strategy,
        }


def _resolve_env(explicit: str | None) -> str:
    env = (explicit or os.getenv("ARBITER_ENV") or "dev").lower()
    if env not in VALID_ENVS:
        raise ValueError(f"ARBITER_ENV must be one of {VALID_ENVS}, got {env!r}")
    return env


def _load_settings(env: str) -> Settings:
    toml_path = CONFIG_DIR / "settings.toml"
    if not toml_path.exists():
        raise FileNotFoundError(f"Missing {toml_path}. Run from the repo root.")
    with toml_path.open("rb") as fh:
        base = tomllib.load(fh)

    env_overrides = _read_flat_yaml(CONFIG_DIR / f"{env}.yaml")

    def env_str(name: str, default: str) -> str:
        return os.getenv(name, default)

    return Settings(
        env=env,
        region=env_str("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", base["aws"]["region"])),
        account=os.getenv("CDK_DEFAULT_ACCOUNT", base["aws"].get("account", "")),
        expected_account_id=env_str(
            "ARBITER_EXPECTED_ACCOUNT_ID", str(base["aws"].get("expected_account_id", ""))
        ),
        # models (env vars win — this is the swap point CI uses)
        generation_model_id=env_str(
            "BEDROCK_GENERATION_MODEL_ID", base["models"]["generation_model_id"]
        ),
        generation_max_tokens=int(base["models"]["generation_max_tokens"]),
        generation_temperature=float(base["models"]["generation_temperature"]),
        embedding_model_id=env_str(
            "BEDROCK_EMBEDDING_MODEL_ID", base["models"]["embedding_model_id"]
        ),
        embedding_dim=int(base["models"]["embedding_dim"]),
        # rerank
        rerank_enabled=bool(base["rerank"]["enabled"]),
        rerank_model_id=base["rerank"]["rerank_model_id"],
        rerank_candidates_k=int(base["rerank"]["candidates_k"]),
        rerank_top_k=int(base["rerank"]["top_k"]),
        # retrieval
        retrieval_top_k=int(base["retrieval"]["top_k"]),
        # chunking
        chunk_strategy=base["chunking"]["strategy"],
        chunk_max_chars=int(base["chunking"]["max_chars"]),
        chunk_overlap_chars=int(base["chunking"]["overlap_chars"]),
        chunking_version=str(base["chunking"]["chunking_version"]),
        # vectors
        vector_bucket=base["vectors"]["vector_bucket"],
        hr_index=base["vectors"]["hr_index"],
        sales_index=base["vectors"]["sales_index"],
        distance_metric=base["vectors"]["distance_metric"],
        # sales sql
        glue_database=base["sales_sql"]["glue_database"],
        glue_table=base["sales_sql"]["glue_table"],
        athena_workgroup=base["sales_sql"]["athena_workgroup"],
        athena_output_prefix=env_str(
            "ARBITER_ATHENA_OUTPUT", base["sales_sql"]["athena_output_prefix"]
        ),
        max_scanned_bytes=int(base["sales_sql"]["max_scanned_bytes"]),
        # guardrails
        guardrails_enabled=os.getenv("ARBITER_GUARDRAIL_ID") is not None
        or bool(base["guardrails"]["enabled"]),
        guardrail_id=env_str("ARBITER_GUARDRAIL_ID", base["guardrails"]["guardrail_id"]),
        guardrail_version=env_str(
            "ARBITER_GUARDRAIL_VERSION", str(base["guardrails"]["guardrail_version"])
        ),
        # per-env knobs
        ingest_batch_size=int(env_overrides.get("ingest_batch_size", 500)),
        log_level=str(env_overrides.get("log_level", "INFO")),
    )


@cache
def get_settings(env: str | None = None) -> Settings:
    """Return the resolved Settings for the active environment (cached).

    Pass ``env`` explicitly in tests; otherwise ARBITER_ENV (default 'dev') wins.
    """
    return _load_settings(_resolve_env(env))
