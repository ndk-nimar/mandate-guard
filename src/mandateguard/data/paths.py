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


def source_dir(sample: bool = False) -> Path:
    """Where the pipeline reads its typed parquet from: the full tables, or the sample.

    T1.5 requires that the full-data and sample runs share one code path. This function
    is that requirement made physical -- `--sample` swaps a directory here and nothing
    downstream branches on it, because `data/sample/` holds the same file names as
    `data/interim/`. A second code path would be free to drift, and the whole point of
    the sample is that CI exercises the code the full run exercises.

    Only the *input* moves. Output still goes to `processed_dir()`, which is gitignored:
    a derived frame committed next to the sample would go stale the first time the code
    changed and nobody would notice.
    """
    return sample_dir() if sample else interim_dir()


def frame_dir(sample: bool = False) -> Path:
    """Where a derived frame is written and read back from.

    The sample's frames live in their own subdirectory rather than overwriting the full
    ones. Both are gitignored, so the only thing separating a sample-derived number from
    a full-data one would otherwise be which script ran last -- and a 5,079-subscriber
    Brier score quoted as a 1.4M-mandate Brier score is exactly the kind of mistake that
    nothing downstream would catch.
    """
    return processed_dir() / "sample" if sample else processed_dir()


def spill_dir() -> Path:
    """Where DuckDB may spill an intermediate that will not fit in RAM.

    An in-memory DuckDB has nowhere to put a hash table larger than memory unless it is
    told, and what happens instead is not an error: T1.4's full run built for seven
    minutes and then died without writing a file or printing a message. Pointing
    `temp_directory` here turns that silent death into a slow success.

    It lives beside the other generated data, so on a machine with
    `MANDATEGUARD_DATA_DIR` set it lands on that drive rather than filling the system
    disk -- the 46M-row person-period frame can spill several GB while it sorts.
    """
    return data_root() / "tmp"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
