"""Runtime configuration. Secrets are read only from the service environment."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Settings:
    data_root: Path
    app_access_token: str
    anthropic_api_key: Optional[str]
    app_env: str
    code_version: str
    xai_api_key: Optional[str] = None
    xai_model: str = "grok-4.6"

    @property
    def secure_cookie(self) -> bool:
        return self.app_env not in {"development", "test"}

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_root=Path(os.environ.get("DATA_ROOT", "./data")).resolve(),
            app_access_token=os.environ.get("APP_ACCESS_TOKEN", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            app_env=os.environ.get("APP_ENV", "development"),
            code_version=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "uncommitted"),
            xai_api_key=os.environ.get("XAI_API_KEY") or None,
            xai_model=os.environ.get("XAI_MODEL") or "grok-4.6",
        )
