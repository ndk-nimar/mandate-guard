"""The book a run was made against, rebuildable from its id (T5.1/T5.2).

`snapshot_id` on a ledger entry has to mean something, or "replay this decision under the
snapshot it was made against" is a sentence with a gap in it. This module is what closes it:
given the id, rebuild the identical book.

Two ids exist, and they are the two directories `data/paths.py` already switches between:

* `sample` -- the committed 5,079-subscriber slice. Needs no download, and is what CI and
  every test use.
* `full` -- the whole KKBox book, from `MANDATEGUARD_DATA_DIR`.

Rebuilding is not cheap: it fits the hazard model and projects the forecast, which is most
of a minute on the full book. It is cached per process, keyed by the id, because a replay
session asking for three decisions from one run should pay for the book once.

### What makes this reproducible at all

`hazard.fit` takes `params.seed`, and the forecast is a deterministic function of the fitted
model and the frame. So the same id and the same seed give the same book, on any machine --
which is the property `replay` rests on and the reason `seed` is one of the six fields a
ledger entry carries. If that ever stops being true, replay does not become approximate: it
starts failing its byte-identical check, loudly, which is the right way for it to break.
"""

from __future__ import annotations

import duckdb

from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import frame_dir, spill_dir
from mandateguard.eval import forecast, world
from mandateguard.policy.loader import Params
from mandateguard.risk import hazard, scoring

SNAPSHOTS = ("sample", "full")

_CACHE: dict[tuple[str, int, int], list[world.BookMandate]] = {}

__all__ = ["SNAPSHOTS", "load_snapshot"]


def load_snapshot(snapshot_id: str, params: Params) -> list[world.BookMandate]:
    """Rebuild the book named by `snapshot_id`. Cached per (id, seed, horizon).

    The seed and the horizon are in the cache key rather than assumed constant: a sweep that
    varies either would otherwise be handed a book built for the previous one, and every
    number after that point would be about a book nobody asked for.
    """
    if snapshot_id not in SNAPSHOTS:
        raise ValueError(
            f"unknown snapshot {snapshot_id!r}. Known: {', '.join(SNAPSHOTS)}. A ledger "
            "entry naming a snapshot this build cannot rebuild is a decision that cannot "
            "be replayed, which is worth failing on rather than approximating."
        )
    key = (snapshot_id, params.seed, params.horizon.weeks)
    if key in _CACHE:
        return _CACHE[key]

    sample = snapshot_id == "sample"
    frame = frame_dir(sample) / "person_periods.parquet"
    book_path = frame_dir(sample) / "mandates.parquet"
    for path in (frame, book_path):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} does not exist -- run scripts/build_periods.py and "
                f"scripts/build_mandates.py{' --sample' if sample else ''} first."
            )

    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        forecast.build(con, model, frame, book_path, params.horizon.weeks)
        book = world.load_book(con)
    finally:
        con.close()

    _CACHE[key] = book
    return book
