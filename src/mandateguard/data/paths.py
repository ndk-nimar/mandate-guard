"""Where data lives, resolved in exactly one place.

The raw KKBox files are ~4 GB and this repository sits inside a OneDrive-synced
folder, so keeping them under `data/` would push gigabytes into cloud sync. Setting
`MANDATEGUARD_DATA_DIR` (in `.env`, see `.env.example`) relocates raw/interim/processed
to a drive outside the sync root without any code knowing about it.

`data/sample/` is the deliberate exception and always resolves inside the repository:
it is committed, and it is what CI reproduces every result from (T1.5). If it could be
relocated, CI could silently read a different sample than the one under review.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
REPO_DATA = ROOT / "data"

load_dotenv(ROOT / ".env")


def data_root() -> Path:
    """Root for generated and downloaded data. Overridable; defaults into the repo."""
    configured = os.environ.get("MANDATEGUARD_DATA_DIR")
    return Path(configured).expanduser() if configured else REPO_DATA


def raw_dir() -> Path:
    """Untouched Kaggle downloads. Never written to by anything but scripts/fetch_data.py."""
    return data_root() / "raw"


def interim_dir() -> Path:
    """Typed parquet: the CSVs, losslessly re-encoded. No modelling decisions applied."""
    return data_root() / "interim"


def processed_dir() -> Path:
    """Model-ready frames, after the India mapping layer (T1.3) has had its say."""
    return data_root() / "processed"


def sample_dir() -> Path:
    """The committed ~5k-subscriber sample. Always in-repo -- CI depends on it."""
    return REPO_DATA / "sample"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
