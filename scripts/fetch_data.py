"""Download and extract the KKBox WSDM 2018 files this project actually needs.

Deliberately file-by-file. The whole competition is 8.9 GB because `user_logs.csv.7z`
alone is 7.1 GB, and nothing in this project reads per-session listening logs -- we
model mandates, not engagement. Fetching only these four keeps it at ~1 GB.

Prerequisites:
  1. A Kaggle API token at ~/.kaggle/access_token  (Settings -> API -> Create New Token)
  2. The competition rules accepted, otherwise every download returns HTTP 403:
     https://www.kaggle.com/c/kkbox-churn-prediction-challenge -> Rules -> Accept

Usage:
    uv run python scripts/fetch_data.py
    uv run python scripts/fetch_data.py --raw-dir F:/mandate-guard-data/raw

The destination defaults to $MANDATEGUARD_RAW_DIR, then to <repo>/data/raw. On the
development machine it is set outside the repo: the repo lives under OneDrive, which
would otherwise sync gigabytes of raw data to the cloud.

Re-running is safe: an archive already present is not re-downloaded, and an archive
already extracted is not re-extracted.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import py7zr

COMPETITION = "kkbox-churn-prediction-challenge"
ROOT = Path(__file__).resolve().parent.parent

# (archive name, the csv it yields, why we need it)
FILES: list[tuple[str, str, str]] = [
    ("train_v2.csv.7z", "train_v2.csv", "churn labels (stage 2)"),
    ("members_v3.csv.7z", "members_v3.csv", "subscriber attributes"),
    ("transactions_v2.csv.7z", "transactions_v2.csv", "final-month transactions"),
    ("transactions.csv.7z", "transactions.csv", "~21M transactions, incl. is_auto_renew"),
]


def default_raw_dir() -> Path:
    env = os.environ.get("MANDATEGUARD_RAW_DIR")
    return Path(env) if env else ROOT / "data" / "raw"


def kaggle_cli() -> str:
    """Locate the kaggle console script.

    `which` finds it under `uv run` (which puts the venv on PATH); the sibling lookup
    covers a bare `python scripts/fetch_data.py` from an unactivated interpreter.
    """
    exe = shutil.which("kaggle")
    if exe:
        return exe
    for name in ("kaggle.exe", "kaggle"):
        candidate = Path(sys.executable).parent / name
        if candidate.exists():
            return str(candidate)
    sys.exit("kaggle CLI not found. Run `uv add --dev kaggle`, then retry via `uv run`.")


def download(exe: str, archive: str, raw_dir: Path) -> None:
    target = raw_dir / archive
    if target.exists():
        print(f"  skip download, already here ({target.stat().st_size / 1e6:.0f} MB)")
        return

    result = subprocess.run(
        [exe, "competitions", "download", "-c", COMPETITION, "-f", archive, "-p", str(raw_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and target.exists():
        print(f"  downloaded {target.stat().st_size / 1e6:.0f} MB")
        return

    # Only now is the output worth reading. Matching on the full phrase, not a bare
    # "403": the progress bar prints byte counts, and one of them contained 403.
    combined = result.stdout + result.stderr
    if "403" in combined and "Forbidden" in combined:
        sys.exit(
            f"\nHTTP 403 on {archive}. The token works but the competition rules are not"
            f"\naccepted. Open https://www.kaggle.com/c/{COMPETITION} -> Rules -> Accept,"
            f"\nthen run this script again."
        )
    sys.exit(f"\nDownload of {archive} failed:\n{combined.strip()}")


def _prune_empty_parents(leaf: Path, stop: Path) -> None:
    """Remove the empty directory chain an archive left behind, never past `stop`."""
    current = leaf
    while current != stop and current.is_relative_to(stop) and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def extract(archive: str, csv_name: str, raw_dir: Path) -> None:
    """Extract one csv and flatten it to raw_dir/<csv_name>.

    The competition archives nest their payload under `data/churn_comp_refresh/`.
    Flattening here lets every downstream reader assume one predictable path instead
    of encoding Kaggle's internal folder layout.
    """
    csv_path = raw_dir / csv_name
    if csv_path.exists():
        print(f"  skip extract, {csv_name} already here ({csv_path.stat().st_size / 1e6:.0f} MB)")
        return

    with py7zr.SevenZipFile(raw_dir / archive, mode="r") as z:
        members = [n for n in z.getnames() if Path(n).name == csv_name]
        if not members:
            sys.exit(f"\n{archive} holds {z.getnames()}, not {csv_name}.")
        z.extract(path=raw_dir, targets=members)

    produced = raw_dir / members[0]
    if produced != csv_path:
        produced.replace(csv_path)
        _prune_empty_parents(produced.parent, raw_dir)
    if not csv_path.exists():
        sys.exit(f"\n{archive} extracted but {csv_name} is missing. Inspect {raw_dir} by hand.")
    print(f"  extracted {csv_name} ({csv_path.stat().st_size / 1e6:.0f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=default_raw_dir())
    parser.add_argument(
        "--no-extract", action="store_true", help="download the archives but leave them packed"
    )
    args = parser.parse_args()

    raw_dir: Path = args.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    exe = kaggle_cli()
    print(f"Destination: {raw_dir}\n")

    for archive, csv_name, why in FILES:
        print(f"{archive}  -- {why}")
        download(exe, archive, raw_dir)
        if not args.no_extract:
            extract(archive, csv_name, raw_dir)
        print()

    print("Done. Nothing here is committed; data/raw/ is gitignored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
