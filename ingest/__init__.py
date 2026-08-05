"""
ingest -- everything that turns a remote feed into the canonical long frame.

The canonical frame, produced by every module in here:

    time_utc | station | variable | value | unit | qc_flag | depth_m
             | reference_frame | source | fetched_utc

Long rather than wide, on purpose: the stations report different variables at
different cadences, and a wide table forces an interval choice before the user
has made one. Resampling is a query-time operation.

One rule holds throughout: a timezone is asserted from documentation or built
from parts, never inferred from a column name or from a string parser's guess.
That is what `time (UTC)` cost this project -- see AGENT_TASK.md 0.1 and
ingest/clockcheck.py.
"""

from .config import (CANONICAL_COLUMNS, StationConfig, empty_frame,
                     load_config, project_config_path)

__all__ = ["CANONICAL_COLUMNS", "StationConfig", "empty_frame", "load_config",
           "project_config_path"]
