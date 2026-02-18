"""
ghost_manager.py – ALU Telemetry
─────────────────────────────────
Manages ghost files: JSON documents that record every frame of a race and
support per-split best-time tracking.

Ghost File Schema
─────────────────
{
    "splits": [
        { "name": "Split 1", "race_completion": 33.3 },
        ...
    ],
    "best_splits": [
        { "timer_value": <int>, "race_completion_pct": <float> },
        ...
    ],
    "race_data": [
        {
            "timer_value":         <int   | null>,
            "race_completion_pct": <float | null>,
            "velocity":            <dict  | null>,
            "car_angle":           <float | null>,
            "car_position":        <dict  | null>,
            "camera_angle":        <float | null>,
            "camera_position":     <dict  | null>,
            "checkpoint":          <int   | null>,
            "nitro_bar_pct":       <float | null>,
            "nitro_state":         <int   | null>,
            "drift_state":         <int   | null>,
            "360_state":           <int   | null>,
            "gear":                <int   | null>,
            "engine_rpm":          <float | null>,
            "acceleration":        <float | null>
        },
        ...
    ]
}

• race_data  – one dict per captured frame; fields absent from a given frame
              are stored as null (None in Python).
• splits     – user-configured checkpoints defined as a race_completion %.
• best_splits – sparse list containing only { timer_value, race_completion_pct }
              entries.  Each split region is replaced in full when the user
              beats the stored time for that region, with timer_values
              normalised so best_splits looks identical to a race_data slice.
"""

import json
import os
import copy
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Default empty ghost structure
# ─────────────────────────────────────────────────────────────────────────────

EMPTY_GHOST: dict = {
    "splits": [],
    "best_splits": [],
    "race_data": [],
}

# Template for a single race-data frame.
# Every key that data_extractor can populate must appear here.
EMPTY_FRAME: dict = {
    "timer_value":         None,
    "race_completion_pct": None,
    "velocity":            None,
    "car_angle":           None,
    "car_position":        None,
    "camera_angle":        None,
    "camera_position":     None,
    "checkpoint":          None,
    "nitro_bar_pct":       None,
    "nitro_state":         None,
    "drift_state":         None,
    "360_state":           None,
    "gear":                None,
    "engine_rpm":          None,
    "acceleration":        None,
}


# ─────────────────────────────────────────────────────────────────────────────
# GhostManager
# ─────────────────────────────────────────────────────────────────────────────

class GhostManager:
    """
    Handles loading, creating, and saving ghost files.

    One GhostManager instance can hold one active ghost at a time.
    The GUI selects which ghost is active via load_ghost() / create_ghost().
    """

    def __init__(self) -> None:
        self._active_ghost: dict | None = None   # in-memory ghost data
        self._active_path: str | None = None     # file path of active ghost

    # ── Loading ───────────────────────────────────────────────────────────────

    def load_ghost(self, filepath: str) -> dict:
        """
        Loads a ghost JSON file from disk and returns its contents as a dict.

        Sets this ghost as the active ghost used for live comparison.

        Parameters
        ----------
        filepath : absolute or relative path to a .json ghost file

        Returns
        -------
        The loaded ghost dict (keys: splits, best_splits, race_data).

        Raises
        ------
        FileNotFoundError  if the file does not exist.
        ValueError         if the JSON is malformed or missing required keys.
        """
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"Ghost file not found: {filepath}")

        with open(filepath, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        # Basic schema validation
        for required_key in ("splits", "best_splits", "race_data"):
            if required_key not in data:
                raise ValueError(
                    f"Ghost file missing required key '{required_key}': {filepath}")

        self._active_ghost = data
        self._active_path = filepath
        print(f"[GhostManager] Loaded ghost: {filepath} "
              f"({len(data['race_data'])} frames)")
        return data

    # ── Creating ──────────────────────────────────────────────────────────────

    def create_ghost(self, filepath: str,
                     splits_config: list[dict] | None = None) -> dict:
        """
        Creates a new, empty ghost file on disk and sets it as the active ghost.

        Parameters
        ----------
        filepath      : path where the .json file will be written.
        splits_config : optional list of split dicts, each with keys:
                          "name"             (str)   – display label
                          "race_completion"  (float) – trigger point in %

                        Pass None or an empty list to create a ghost without
                        splits (can be configured later via the GUI).

        Returns
        -------
        The newly created ghost dict.
        """
        ghost = copy.deepcopy(EMPTY_GHOST)
        ghost["splits"] = splits_config if splits_config else []

        # Ensure the target directory exists
        target_dir = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(target_dir, exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as fh:
            json.dump(ghost, fh, indent=2)

        self._active_ghost = ghost
        self._active_path = filepath
        print(f"[GhostManager] Created ghost: {filepath}")
        return ghost

    # ── Saving ────────────────────────────────────────────────────────────────

    def save_race_data(self, filepath: str, race_frames: list[dict]) -> None:
        """
        Saves all frames collected during a race into the ghost file and
        updates best_splits where the current run was faster.

        Parameters
        ----------
        filepath    : path of the ghost file to update.
        race_frames : list of snapshot dicts from DataExtractor.get_snapshot()

        Algorithm
        ─────────
        1. Load the existing ghost (or create one if filepath does not exist).
        2. Replace race_data with the new frames.
        3. For each configured split, compare the new run's timer at the split
           boundary against the stored best.  If the new run is faster (lower
           timer value), replace that split's section in best_splits with the
           corresponding frames from the new run, normalising timer values so
           the best_splits list reads as a continuous timeline.
        4. Write the updated ghost back to disk.
        """
        # ── Load or initialise ────────────────────────────────────────────────
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    ghost = json.load(fh)
            except (json.JSONDecodeError, OSError):
                ghost = copy.deepcopy(EMPTY_GHOST)
        else:
            ghost = copy.deepcopy(EMPTY_GHOST)

        splits    = ghost.get("splits", [])
        old_best  = ghost.get("best_splits", [])

        # ── Update race_data ──────────────────────────────────────────────────
        ghost["race_data"] = race_frames

        # ── Update best_splits per split region ───────────────────────────────
        ghost["best_splits"] = self._compute_best_splits(
            old_best, race_frames, splits)

        # ── Write to disk ─────────────────────────────────────────────────────
        target_dir = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(target_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as fh:
            json.dump(ghost, fh, indent=2)

        self._active_ghost = ghost
        self._active_path = filepath
        print(f"[GhostManager] Saved race data to: {filepath} "
              f"({len(race_frames)} frames)")

    # ── Best-splits logic ─────────────────────────────────────────────────────

    def _compute_best_splits(self,
                             old_best: list[dict],
                             new_frames: list[dict],
                             splits: list[dict]) -> list[dict]:
        """
        Compares each split region of the new run against the stored best and
        returns the updated best_splits list.

        If no splits are configured, the whole run is treated as one region
        (from 0 % to 100 %).

        Timer normalisation
        ───────────────────
        Stored best_splits entries have their timer_value adjusted so that the
        first frame of the list always starts at timer_value == 0 within each
        region.  This makes best_splits directly comparable to race_data when
        displaying the "ahead / behind" delta in the GUI.
        """
        # Build split boundaries: [ (start_pct, end_pct, name), ... ]
        sorted_splits = sorted(splits, key=lambda s: s.get("race_completion", 0.0))
        boundaries: list[tuple[float, float, str]] = []

        if not sorted_splits:
            boundaries = [(0.0, 100.0, "Full Run")]
        else:
            prev = 0.0
            for sp in sorted_splits:
                end = sp.get("race_completion", 0.0)
                boundaries.append((prev, end, sp.get("name", "")))
                prev = end
            # Final region from last split to finish
            boundaries.append((prev, 100.0, "Finish"))

        # Extract only the two fields needed for best_splits from a frame list
        def _slim(frames: list[dict], timer_offset: int = 0) -> list[dict]:
            """Returns slim { timer_value, race_completion_pct } entries."""
            result = []
            for f in frames:
                tv = f.get("timer_value")
                rp = f.get("race_completion_pct")
                if tv is not None:
                    result.append({
                        "timer_value": tv + timer_offset,
                        "race_completion_pct": rp,
                    })
            return result

        def _frames_in_region(frames: list[dict],
                              start_pct: float, end_pct: float) -> list[dict]:
            """Filters frames that fall inside [start_pct, end_pct]."""
            return [
                f for f in frames
                if (f.get("race_completion_pct") is not None
                    and start_pct <= f["race_completion_pct"] <= end_pct)
            ]

        def _split_timer(frames: list[dict], at_pct: float) -> int | None:
            """
            Returns the timer_value of the last frame at or before at_pct.
            Uses linear interpolation between the two nearest frames.
            """
            before = [f for f in frames
                      if f.get("race_completion_pct") is not None
                      and f["race_completion_pct"] <= at_pct
                      and f.get("timer_value") is not None]
            after  = [f for f in frames
                      if f.get("race_completion_pct") is not None
                      and f["race_completion_pct"] >= at_pct
                      and f.get("timer_value") is not None]
            if not before and not after:
                return None
            if not before:
                return after[0]["timer_value"]
            if not after:
                return before[-1]["timer_value"]
            # Interpolate
            f0, f1 = before[-1], after[0]
            p0 = f0["race_completion_pct"]
            p1 = f1["race_completion_pct"]
            if p0 == p1:
                return f0["timer_value"]
            t = (at_pct - p0) / (p1 - p0)
            return int(f0["timer_value"] + t * (f1["timer_value"] - f0["timer_value"]))

        # ── Per-region comparison ─────────────────────────────────────────────
        assembled_best: list[dict] = []

        # We'll split old_best and new_frames into regions and compare
        for start_pct, end_pct, region_name in boundaries:
            new_region  = _frames_in_region(new_frames, start_pct, end_pct)
            old_region  = _frames_in_region(old_best,   start_pct, end_pct)

            new_time = _split_timer(new_region, end_pct)
            old_time = _split_timer(old_region, end_pct)

            if new_time is None:
                # No new data for this region – keep old best
                assembled_best.extend(_slim(old_region))
                continue

            if old_time is None or new_time <= old_time:
                # New run is faster (or there was no previous best) – use new
                # Normalise: subtract the timer at start_pct so each region
                # starts at 0 within best_splits.
                offset_time = _split_timer(new_region, start_pct) or 0
                assembled_best.extend(_slim(new_region, timer_offset=-offset_time))
            else:
                # Old run was faster – keep old
                assembled_best.extend(_slim(old_region))

        return assembled_best

    # ── Active ghost accessors ────────────────────────────────────────────────

    def get_active_ghost(self) -> dict | None:
        """Returns the currently loaded ghost dict, or None."""
        return self._active_ghost

    def get_active_path(self) -> str | None:
        """Returns the file path of the currently active ghost, or None."""
        return self._active_path

    def get_splits(self) -> list[dict]:
        """Returns the split list of the active ghost (may be empty)."""
        if self._active_ghost is None:
            return []
        return self._active_ghost.get("splits", [])

    def set_splits(self, splits: list[dict]) -> None:
        """
        Updates the splits of the active ghost and persists the change.
        Called by the GUI split-configuration dialog.
        """
        if self._active_ghost is None or self._active_path is None:
            return
        self._active_ghost["splits"] = splits
        with open(self._active_path, "w", encoding="utf-8") as fh:
            json.dump(self._active_ghost, fh, indent=2)

    # ── Ghost comparison helpers (used by GUI) ────────────────────────────────

    def interpolate_ghost_timer(self, current_race_pct: float) -> float | None:
        """
        Returns the ghost's timer_value at the given race_completion_pct via
        linear interpolation between the two nearest entries in best_splits.

        Returns None if the active ghost has no best_splits data.

        This is the primary function the GUI uses for the ahead/behind display.
        """
        if self._active_ghost is None:
            return None

        best = self._active_ghost.get("best_splits", [])
        if not best:
            return None

        # Filter to entries that have both required fields
        valid = [e for e in best
                 if e.get("timer_value") is not None
                 and e.get("race_completion_pct") is not None]
        if not valid:
            return None

        # Sort by completion
        valid.sort(key=lambda e: e["race_completion_pct"])

        # Edge cases
        if current_race_pct <= valid[0]["race_completion_pct"]:
            return float(valid[0]["timer_value"])
        if current_race_pct >= valid[-1]["race_completion_pct"]:
            return float(valid[-1]["timer_value"])

        # Binary search for surrounding pair
        for i in range(len(valid) - 1):
            p0 = valid[i]["race_completion_pct"]
            p1 = valid[i + 1]["race_completion_pct"]
            if p0 <= current_race_pct <= p1:
                if p0 == p1:
                    return float(valid[i]["timer_value"])
                t = (current_race_pct - p0) / (p1 - p0)
                return (valid[i]["timer_value"]
                        + t * (valid[i + 1]["timer_value"] - valid[i]["timer_value"]))

        return None
