"""
config.py -- read config/stations.yaml and answer questions about geometry.

Depths and reference frames come from here and nowhere else. Axiom's ERDDAP
reports z = 0.0 m for both the Scripps Pier station and CDIP 201, so joining
depth from the feed silently places a 5 m CTD and a surface buoy at the same
level. See AGENT_TASK.md 0.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# The canonical long frame. Every ingest module returns exactly these columns,
# in this order.
CANONICAL_COLUMNS = [
    "time_utc", "station", "variable", "value", "unit", "qc_flag",
    "depth_m", "reference_frame", "source", "fetched_utc",
]

CONFIG_DIRNAME = "config"
CONFIG_FILENAME = "stations.yaml"


def project_config_path(root: Path) -> Path:
    return Path(root) / CONFIG_DIRNAME / CONFIG_FILENAME


def empty_frame() -> pd.DataFrame:
    """An empty canonical frame with the right dtypes, so concat never widens."""
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in CANONICAL_COLUMNS})
    df["time_utc"] = pd.Series(dtype="datetime64[ns, UTC]")
    df["fetched_utc"] = pd.Series(dtype="datetime64[ns, UTC]")
    df["value"] = pd.Series(dtype="float64")
    df["qc_flag"] = pd.Series(dtype="Int64")
    df["depth_m"] = pd.Series(dtype="float64")
    return df


@dataclass
class Station:
    key: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(self.raw.get("id", self.key))

    @property
    def name(self) -> str:
        return str(self.raw.get("name", self.key))

    @property
    def lon(self) -> float | None:
        return self.raw.get("lon")

    @property
    def lat(self) -> float | None:
        return self.raw.get("lat")

    @property
    def depth_m(self) -> float | None:
        return self.raw.get("sensor_depth_m")

    @property
    def depth_reference(self) -> str | None:
        return self.raw.get("depth_reference")

    @property
    def reference_frame(self) -> str:
        return str(self.raw.get("reference_frame", "unknown"))

    @property
    def role(self) -> str:
        return str(self.raw.get("role", "context_only"))

    @property
    def cadence_min(self) -> float | None:
        return self.raw.get("cadence_min")

    @property
    def source(self) -> str | None:
        return self.raw.get("source")

    @property
    def is_clock_anchor(self) -> bool:
        return bool(self.raw.get("clock_anchor", False))

    @property
    def note(self) -> str:
        return str(self.raw.get("note", "")).strip()

    @property
    def variables(self) -> dict[str, dict]:
        out = {}
        for k, v in (self.raw.get("variables") or {}).items():
            out[k] = v if isinstance(v, dict) else {"erddap": v}
        return out

    def erddap_columns(self) -> dict[str, str]:
        """{erddap column name -> canonical variable name} for feed variables."""
        out = {}
        for canonical, spec in self.variables.items():
            col = spec.get("erddap")
            if col:
                out[str(col)] = canonical
            qc_col = spec.get("qc")
            if qc_col:
                out[str(qc_col)] = f"{canonical}__qc"
        return out

    def unit_for(self, variable: str) -> str:
        spec = self.variables.get(variable, {})
        return str(spec.get("unit_canonical", ""))

    def label(self) -> str:
        """Short display label carrying depth and frame, for chart legends."""
        if self.depth_m is None:
            depth = "bed" if self.depth_reference == "seabed" else "?"
        else:
            depth = f"{self.depth_m:.2f}".rstrip("0").rstrip(".") + " m"
        return f"{self.key} ({depth}, {self.reference_frame})"


@dataclass
class StationConfig:
    raw: dict[str, Any]
    path: Path | None = None

    # -------------------------------------------------------------- accessors

    @property
    def schema_version(self) -> int:
        return int(self.raw.get("schema_version", 1))

    @property
    def defaults(self) -> dict:
        return self.raw.get("defaults") or {}

    @property
    def window_days(self) -> int:
        return int(self.defaults.get("window_days", 45))

    @property
    def qc_policy(self) -> str:
        return str(self.defaults.get("qc_policy", "accept 1,2; flag 3; reject 4,9"))

    @property
    def endpoints(self) -> dict:
        return self.raw.get("endpoints") or {}

    @property
    def comparisons(self) -> dict:
        return self.raw.get("comparisons") or {}

    def endpoint(self, name: str) -> dict:
        ep = self.endpoints.get(name)
        if ep is None:
            raise KeyError(f"no endpoint {name!r} in {self.path}")
        return ep

    @property
    def stations(self) -> dict[str, Station]:
        return {k: Station(k, v or {})
                for k, v in (self.raw.get("stations") or {}).items()}

    def station(self, key: str) -> Station:
        st = self.stations.get(str(key))
        if st is None:
            raise KeyError(f"unknown station {key!r}; known: {list(self.stations)}")
        return st

    def by_role(self, role: str) -> list[Station]:
        return [s for s in self.stations.values() if s.role == role]

    def by_source(self, source: str) -> list[Station]:
        return [s for s in self.stations.values() if s.source == source]

    @property
    def clock_anchor(self) -> Station | None:
        for s in self.stations.values():
            if s.is_clock_anchor:
                return s
        return None

    # ------------------------------------------------------------- geometry

    def geometry(self) -> pd.DataFrame:
        """One row per station: depth_m and reference_frame, for joining."""
        return pd.DataFrame([
            {"station": s.key, "depth_m": s.depth_m,
             "reference_frame": s.reference_frame, "role": s.role}
            for s in self.stations.values()
        ])

    def attach_geometry(self, df: pd.DataFrame) -> pd.DataFrame:
        """Join depth_m / reference_frame onto a long frame, from config only.

        Any depth_m already present is overwritten on purpose: if it came from
        the feed it is 0.0 and wrong.
        """
        if df.empty:
            return df
        geo = self.geometry().set_index("station")
        st = df["station"].astype(str)
        df = df.copy()
        df["depth_m"] = st.map(geo["depth_m"]).astype("float64")
        df["reference_frame"] = st.map(geo["reference_frame"]).fillna("unknown")
        return df


def load_config(root: Path | None = None, path: Path | None = None) -> StationConfig:
    """Load config/stations.yaml. `root` is the repo root; `path` overrides it."""
    if path is None:
        if root is None:
            root = Path(__file__).resolve().parent.parent
        path = project_config_path(root)
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"station config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return StationConfig(raw=raw, path=path)
