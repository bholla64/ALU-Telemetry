"""
main.py – ALU Telemetry
────────────────────────
Entry point.

Startup sequence
────────────────
1. Load persisted user config (if any).
2. Initialise DataExtractor and attempt to attach to the game process.
3. Initialise GhostManager and load the last-used ghost (if any).
4. Build and run the GUI (blocking – returns when the window is closed).
5. On exit, persist any config changes.
"""

import json
import os
import sys
import threading

from data_extractor import DataExtractor
from ghost_manager   import GhostManager
from gui             import TelemetryGUI


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
GHOSTS_DIR  = os.path.join(BASE_DIR, "ghosts")


# ─────────────────────────────────────────────────────────────────────────────
# Config persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Loads config.json if it exists, otherwise returns an empty dict."""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[main] Could not load config: {exc} – using defaults")
    return {}


def save_config(config: dict) -> None:
    """Persists the current config dict to config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
    except OSError as exc:
        print(f"[main] Could not save config: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Race loop (runs on a background thread)
# ─────────────────────────────────────────────────────────────────────────────

def race_loop(extractor: DataExtractor, ghost_manager: GhostManager,
              config: dict) -> None:
    """
    Continuously waits for a race to start, accumulates frame snapshots,
    then hands them to GhostManager when the race ends.

    This function runs forever on a daemon thread and will be stopped
    automatically when the main thread (GUI) exits.
    """
    active_ghost_path = config.get("last_ghost_path")

    while True:
        # ── Wait until the game is running ────────────────────────────────────
        print("[main] Waiting for game process…")
        while not extractor.attach(timeout=0.0):
            import time; import time as _t; _t.sleep(2.0)

        # ── Scan for offsets and inject trampolines ───────────────────────────
        print("[main] Attaching memory hooks…")
        extractor.find_offsets()

        # ── Race loop ─────────────────────────────────────────────────────────
        while extractor.is_attached():
            # Block until a race starts
            extractor.wait_for_race_start()

            # Accumulate frames for this race
            race_frames: list[dict] = []
            print("[main] Race started – recording…")

            while True:
                snapshot = extractor.get_snapshot()
                race_frames.append(snapshot)

                # Check for race end
                if active_ghost_path and extractor.detect_race_end(
                        ghost_manager, active_ghost_path, race_frames):
                    print("[main] Race ended – data saved.")
                    break

                # Minimal sleep so we don't busy-spin on the main thread;
                # actual capture rate is driven by the physics-update flag
                import time as _t; _t.sleep(0.001)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 50)
    print(" ALU Telemetry – starting up")
    print("=" * 50)

    # ── Ensure ghosts directory exists ────────────────────────────────────────
    os.makedirs(GHOSTS_DIR, exist_ok=True)

    # ── Load persisted config ─────────────────────────────────────────────────
    config = load_config()

    # ── DataExtractor ─────────────────────────────────────────────────────────
    extractor = DataExtractor()

    # Attempt a non-blocking attach; the race loop will keep retrying
    if extractor.attach(timeout=0.0):
        extractor.find_offsets()
    else:
        print("[main] Game not running yet – will retry in race loop.")

    # ── GhostManager ──────────────────────────────────────────────────────────
    ghost_manager = GhostManager()

    last_ghost = config.get("last_ghost_path")
    if last_ghost and os.path.isfile(last_ghost):
        try:
            ghost_manager.load_ghost(last_ghost)
        except Exception as exc:
            print(f"[main] Could not restore last ghost: {exc}")

    # ── Race loop on background thread ────────────────────────────────────────
    loop_thread = threading.Thread(
        target=race_loop,
        args=(extractor, ghost_manager, config),
        daemon=True,
        name="RaceLoop",
    )
    loop_thread.start()

    # ── Launch GUI (blocking) ─────────────────────────────────────────────────
    gui = TelemetryGUI(extractor, ghost_manager, config)
    gui.run()  # blocks until window is closed

    # ── Save state on exit ────────────────────────────────────────────────────
    active_path = ghost_manager.get_active_path()
    if active_path:
        config["last_ghost_path"] = active_path
    # Persist any config changes made in the settings window
    config.update(gui._config)
    save_config(config)
    print("[main] Goodbye.")


if __name__ == "__main__":
    main()
