"""Local settings from environment variables, with small explicit defaults."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

HOME = Path(os.environ.get("AUTOSDE_HOME", Path.home() / ".autosde"))


@dataclass(frozen=True)
class Settings:
    model: str
    # Prices in USD per million tokens; used only for the cost estimate.
    input_price: float
    output_price: float
    reasoning_effort: str | None
    db_path: Path
    gitleaks: str | None
    # Optional AWS Secrets Manager fallback for the OpenAI key (OPENAI_API_KEY wins).
    secret_id: str | None = None
    aws_profile: str | None = None
    aws_region: str = "us-east-1"
    max_file_bytes: int = 64_000
    max_total_bytes: int = 400_000


def gitleaks_path():
    configured = os.environ.get("AUTOSDE_GITLEAKS")
    if configured:
        return configured
    return shutil.which("gitleaks")


def load_settings():
    # Defaults: gpt-6-luna at $0.10 / $0.50 per 1M tokens (OpenAI pricing, 2026-10-31).
    effort = os.environ.get("AUTOSDE_REASONING_EFFORT", "low")
    return Settings(
        model=os.environ.get("AUTOSDE_MODEL", "gpt-6-luna"),
        input_price=float(os.environ.get("AUTOSDE_INPUT_PRICE", "0.10")),
        output_price=float(os.environ.get("AUTOSDE_OUTPUT_PRICE", "0.50")),
        reasoning_effort=None if effort == "none" else effort,
        db_path=Path(os.environ.get("AUTOSDE_DB", HOME / "autosde.db")),
        gitleaks=gitleaks_path(),
        secret_id=os.environ.get("AUTOSDE_SECRET_ID", "autosde/openai-api-key"),
        aws_profile=os.environ.get("AUTOSDE_SECRET_PROFILE", "ik_lab"),
        aws_region=os.environ.get("AUTOSDE_SECRET_REGION", "us-east-1"),
    )
