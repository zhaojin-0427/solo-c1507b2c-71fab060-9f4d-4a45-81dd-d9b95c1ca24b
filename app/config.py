"""Application settings.

Everything is local-first: a single SQLite file, zero external services.
Override the database path with the GRAYLAB_DB environment variable.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("GRAYLAB_DB", os.path.join(os.getcwd(), "graylab.db"))
    api_prefix: str = "/api"


settings = Settings()
