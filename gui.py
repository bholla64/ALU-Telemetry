"""
gui.py – ALU Telemetry
───────────────────────
Handles all user-interface concerns.

Rendering approach
──────────────────
tkinter is used with a Canvas-based HUD overlay (overrideredirect, topmost,
transparent background) for the race display and standard Toplevel widgets for
the settings window.  Canvas redraws are driven by a fixed-rate after() loop
which avoids the flickering associated with widget-level updates, giving smooth
high-frequency refreshes without tearing.

Layout
──────
• Collapsed (no race)   – small bar showing [Settings] [Close]
• Expanded  (in race)   – full HUD panel showing user-selected data points
                          plus [Settings] [Close]

Every data-point display is its own method so new points can be added without
touching the rest of the layout code.
"""

import tkinter as tk
from tkinter import ttk, colorchooser, filedialog, messagebox
import threading
import time
import os
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Default configuration values
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    # ── Visibility toggles per data point ─────────────────────────────────────
    "show_timer":            True,
    "show_race_completion":  True,
    "show_velocity":         True,
    "show_gear":             True,
    "show_rpm":              True,
    "show_checkpoint":       True,
    "show_nitro_bar":        True,
    "show_nitro_state":      False,
    "show_drift_state":      True,
    "show_360_state":        True,
    "show_acceleration":     False,
    "show_car_angle":        False,
    "show_car_position":     False,
    "show_camera_angle":     False,
    "show_camera_position":  False,
    "show_ghost_delta":      True,

    # ── Velocity sub-mode ─────────────────────────────────────────────────────
    # Options: "real_total" | "fake_total" | "real_horizontal" | "fake_horizontal"
    "velocity_mode": "real_total",

    # ── Colours ───────────────────────────────────────────────────────────────
    "color_background":    "#1A1A2E",   # HUD panel background
    "color_text":          "#E0E0E0",   # Normal text
    "color_label":         "#7A7A9A",   # Dim label text
    "color_highlight":     "#00D4FF",   # Highlighted values
    "color_ahead":         "#00CC44",   # Ghost delta – player is ahead
    "color_behind":        "#FF4444",   # Ghost delta – player is behind
    "color_equal":         "#FFFFFF",   # Ghost delta – equal

    # ── Layout ────────────────────────────────────────────────────────────────
    "hud_x":       20,    # pixel position of HUD overlay (top-left)
    "hud_y":       20,
    "hud_width":   260,
    "hud_alpha":   0.88,  # overall window transparency (0.0–1.0)
    "font_family": "Consolas",
    "font_size":   11,

    # ── Hotkeys ───────────────────────────────────────────────────────────────
    "hotkey_toggle_hud":      "F9",   # placeholder
    "hotkey_toggle_ghost":    "F10",  # placeholder
    "hotkey_open_settings":   "F11",  # placeholder
}


# ─────────────────────────────────────────────────────────────────────────────
# TelemetryGUI
# ─────────────────────────────────────────────────────────────────────────────

class TelemetryGUI:
    """
    Main GUI class.

    Parameters
    ----------
    data_extractor : DataExtractor  – supplies snapshots
    ghost_manager  : GhostManager   – supplies ghost comparison data
    config         : dict           – initial config (merged with defaults)
    """

    # Refresh interval in milliseconds.  60 fps ≈ 16 ms.
    REFRESH_MS = 16

    def __init__(self, data_extractor, ghost_manager, config: dict | None = None):
        self._extractor     = data_extractor
        self._ghost_manager = ghost_manager
        self._config        = {**DEFAULT_CONFIG, **(config or {})}

        # Latest snapshot – updated by the polling thread
        self._latest_snapshot: dict = {}
        self._snapshot_lock = threading.Lock()

        # Race state
        self._race_active = False

        # Settings window reference (None when closed)
        self._settings_window: tk.Toplevel | None = None

        # ── Main (HUD) window ─────────────────────────────────────────────────
        self._root = tk.Tk()
        self._root.title("ALU Telemetry")
        self._root.overrideredirect(True)       # borderless
        self._root.attributes("-topmost", True) # always on top
        self._root.attributes("-alpha", self._config["hud_alpha"])
        self._root.configure(bg=self._config["color_background"])

        # Position
        self._root.geometry(
            f"+{self._config['hud_x']}+{self._config['hud_y']}")

        # Canvas used for all HUD drawing (avoids widget-level flicker)
        self._canvas = tk.Canvas(
            self._root,
            bg=self._config["color_background"],
            highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)

        # Button bar (always visible)
        self._build_button_bar()

        # Start the polling thread
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        # Register hotkeys
        self._register_hotkeys()

        # Schedule first draw
        self._root.after(self.REFRESH_MS, self._redraw)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Enters the tkinter main loop (blocking)."""
        self._root.mainloop()

    def close(self) -> None:
        """Destroys the main window and exits."""
        self._root.destroy()

    # ── Button bar ────────────────────────────────────────────────────────────

    def _build_button_bar(self) -> None:
        """Builds the persistent [Settings] [Close] bar at the top."""
        bar = tk.Frame(self._root, bg=self._config["color_background"])
        bar.pack(fill=tk.X, side=tk.TOP)

        btn_style = {
            "bg":     "#2E2E4E",
            "fg":     self._config["color_text"],
            "relief": tk.FLAT,
            "font":   (self._config["font_family"], 9),
            "padx":   6, "pady": 2,
            "cursor": "hand2",
        }
        tk.Button(bar, text="⚙ Settings",
                  command=self._open_settings, **btn_style).pack(
            side=tk.LEFT, padx=(4, 2), pady=2)
        tk.Button(bar, text="✕ Close",
                  command=self.close, **btn_style).pack(
            side=tk.RIGHT, padx=(2, 4), pady=2)

        # Allow dragging the window by clicking the bar background
        bar.bind("<ButtonPress-1>", self._on_drag_start)
        bar.bind("<B1-Motion>",     self._on_drag_motion)

    # ── Window dragging ───────────────────────────────────────────────────────

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self._root.winfo_x()
        self._drag_y = event.y_root - self._root.winfo_y()

    def _on_drag_motion(self, event: tk.Event) -> None:
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self._root.geometry(f"+{new_x}+{new_y}")

    # ── Polling thread ────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """
        Background thread: calls get_snapshot() on every physics update.
        Copies the result to self._latest_snapshot under the lock.
        The draw loop reads from there without blocking the UI thread.
        """
        while True:
            if self._extractor.is_attached():
                snap = self._extractor.get_snapshot()
                with self._snapshot_lock:
                    self._latest_snapshot = snap
                # Race-state detection (placeholder condition)
                completion = snap.get("race_completion_pct")
                self._race_active = (completion is not None
                                     and 0.0 <= completion < 100.0)  # placeholder condition
            time.sleep(0.005)  # ~200 Hz poll ceiling

    # ── Main draw loop ────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        """
        Redraws the entire HUD canvas.  Called every REFRESH_MS ms via after().
        All drawing goes through the canvas to prevent widget-level flickering.
        """
        with self._snapshot_lock:
            snap = dict(self._latest_snapshot)

        c     = self._canvas
        cfg   = self._config
        font  = (cfg["font_family"], cfg["font_size"])
        font_label = (cfg["font_family"], cfg["font_size"] - 1)

        c.delete("hud")  # clear previous frame's items

        if not self._race_active:
            # ── Collapsed mode ────────────────────────────────────────────────
            self._root.geometry(f"{cfg['hud_width']}x28")
            # Nothing extra to draw; button bar is already visible
        else:
            # ── Expanded mode ─────────────────────────────────────────────────
            y = 4  # vertical cursor (pixels from top of canvas area)
            line_h = cfg["font_size"] + 8

            def draw_row(label: str, value: str,
                         color: str = cfg["color_text"]) -> None:
                nonlocal y
                c.create_text(6, y, anchor="nw",
                              text=label, fill=cfg["color_label"],
                              font=font_label, tags="hud")
                c.create_text(cfg["hud_width"] - 6, y, anchor="ne",
                              text=value, fill=color,
                              font=font, tags="hud")
                y += line_h

            # ── Individual data-point sections ────────────────────────────────
            # Each section is isolated so new points can be appended below.

            self._draw_timer(snap, draw_row)
            self._draw_race_completion(snap, draw_row)
            self._draw_ghost_delta(snap, c, cfg, font, line_h)
            if cfg["show_ghost_delta"]:
                y += line_h  # space reserved for the delta row drawn above

            self._draw_velocity(snap, draw_row)
            self._draw_gear(snap, draw_row)
            self._draw_rpm(snap, draw_row)
            self._draw_checkpoint(snap, draw_row)
            self._draw_nitro_bar(snap, draw_row)
            self._draw_nitro_state(snap, draw_row)
            self._draw_drift_state(snap, draw_row)
            self._draw_360_state(snap, draw_row)
            self._draw_acceleration(snap, draw_row)
            self._draw_car_angle(snap, draw_row)
            self._draw_car_position(snap, draw_row)
            self._draw_camera_angle(snap, draw_row)
            self._draw_camera_position(snap, draw_row)

            total_height = y + 6
            self._root.geometry(f"{cfg['hud_width']}x{total_height + 28}")
            c.configure(height=total_height)

        # Schedule next frame
        self._root.after(self.REFRESH_MS, self._redraw)

    # ── Per-data-point draw sections ──────────────────────────────────────────
    # Each method receives the snapshot and a draw_row callback.
    # Adding a new data point means adding a new method and one call above.

    def _draw_timer(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_timer"]:
            return
        tv = snap.get("timer_value")
        if tv is None:
            display = "–"
        else:
            # Convert raw timer units to mm:ss.mmm  # placeholder – unit conversion factor unknown; showing raw value
            display = str(tv)  # placeholder
        draw_row("Timer", display, self._config["color_highlight"])

    def _draw_race_completion(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_race_completion"]:
            return
        pct = snap.get("race_completion_pct")
        display = f"{pct:.1f} %" if pct is not None else "–"
        draw_row("Progress", display)

    def _draw_ghost_delta(self, snap: dict, canvas: tk.Canvas,
                          cfg: dict, font: tuple, line_h: int) -> None:
        """
        Draws the ghost delta row with a coloured background strip.
        Uses GhostManager.interpolate_ghost_timer() for interpolation.
        """
        if not cfg["show_ghost_delta"]:
            return
        pct = snap.get("race_completion_pct")
        tv  = snap.get("timer_value")
        if pct is None or tv is None:
            return

        ghost_time = self._ghost_manager.interpolate_ghost_timer(pct)
        if ghost_time is None:
            return

        delta = tv - ghost_time   # positive = behind ghost, negative = ahead
        if delta < -5:            # 5-unit tolerance for "equal"  # placeholder – tune tolerance
            bg_color   = cfg["color_ahead"]
            sign       = "▲ −"
        elif delta > 5:           # placeholder
            bg_color   = cfg["color_behind"]
            sign       = "▼ +"
        else:
            bg_color   = cfg["color_equal"]
            sign       = "● "

        # Draw coloured background strip
        y_strip = self._canvas.winfo_reqheight() - line_h - 4  # approximate # placeholder
        canvas.create_rectangle(
            0, y_strip, cfg["hud_width"], y_strip + line_h,
            fill=bg_color, outline="", tags="hud")

        canvas.create_text(
            cfg["hud_width"] // 2, y_strip + line_h // 2,
            text=f"{sign}{abs(delta):.0f}",  # placeholder – format with real units
            fill="#000000", font=font, tags="hud")

    def _draw_velocity(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_velocity"]:
            return
        vel = snap.get("velocity")
        mode = self._config["velocity_mode"]
        if vel is None:
            display = "–"
        else:
            # Velocity dict expected: { "real_total", "fake_total",
            #                          "real_horizontal", "fake_horizontal" }
            display = f"{vel.get(mode, '–')}"  # placeholder
        label_map = {
            "real_total":       "Vel (Real)",
            "fake_total":       "Vel (Fake)",
            "real_horizontal":  "Vel H (Real)",
            "fake_horizontal":  "Vel H (Fake)",
        }
        draw_row(label_map.get(mode, "Velocity"), display)

    def _draw_gear(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_gear"]:
            return
        g = snap.get("gear")
        draw_row("Gear", str(g) if g is not None else "–",
                 self._config["color_highlight"])

    def _draw_rpm(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_rpm"]:
            return
        rpm = snap.get("engine_rpm")
        display = f"{int(rpm)}" if rpm is not None else "–"
        draw_row("RPM", display)

    def _draw_checkpoint(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_checkpoint"]:
            return
        cp = snap.get("checkpoint")
        draw_row("Checkpoint", str(cp) if cp is not None else "–")

    def _draw_nitro_bar(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_nitro_bar"]:
            return
        nb = snap.get("nitro_bar_pct")
        display = f"{nb:.0%}" if nb is not None else "–"  # placeholder – confirm unit
        draw_row("Nitro", display)

    def _draw_nitro_state(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_nitro_state"]:
            return
        ns = snap.get("nitro_state")
        draw_row("Nitro State", str(ns) if ns is not None else "–")  # placeholder – decode flags

    def _draw_drift_state(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_drift_state"]:
            return
        ds = snap.get("drift_state")
        draw_row("Drift", str(ds) if ds is not None else "–")  # placeholder – decode flags

    def _draw_360_state(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_360_state"]:
            return
        s = snap.get("360_state")
        draw_row("360", str(s) if s is not None else "–")  # placeholder – decode flags

    def _draw_acceleration(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_acceleration"]:
            return
        acc = snap.get("acceleration")
        display = f"{acc:.2f}" if acc is not None else "–"
        draw_row("Accel", display)

    def _draw_car_angle(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_car_angle"]:
            return
        ca = snap.get("car_angle")
        display = f"{ca:.1f}°" if ca is not None else "–"
        draw_row("Car Angle", display)

    def _draw_car_position(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_car_position"]:
            return
        pos = snap.get("car_position")
        if pos is None:
            display = "–"
        else:
            display = f"{pos.get('x','?'):.0f}, {pos.get('y','?'):.0f}"  # placeholder
        draw_row("Car Pos", display)

    def _draw_camera_angle(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_camera_angle"]:
            return
        ca = snap.get("camera_angle")
        display = f"{ca:.1f}°" if ca is not None else "–"
        draw_row("Cam Angle", display)

    def _draw_camera_position(self, snap: dict, draw_row: Callable) -> None:
        if not self._config["show_camera_position"]:
            return
        pos = snap.get("camera_position")
        if pos is None:
            display = "–"
        else:
            display = f"{pos.get('x','?'):.0f}, {pos.get('y','?'):.0f}"  # placeholder
        draw_row("Cam Pos", display)

    # ── Settings window ───────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        """Opens (or raises) the settings Toplevel window."""
        if self._settings_window is not None and \
                self._settings_window.winfo_exists():
            self._settings_window.lift()
            return

        win = tk.Toplevel(self._root)
        win.title("ALU Telemetry – Settings")
        win.geometry("480x640")
        win.resizable(False, True)
        win.configure(bg="#1A1A2E")
        self._settings_window = win
        win.protocol("WM_DELETE_WINDOW",
                     lambda: setattr(self, "_settings_window", None)
                     or win.destroy())

        # Notebook tabs
        style = ttk.Style(win)
        style.theme_use("clam")
        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_settings_display_tab(nb)
        self._build_settings_colors_tab(nb)
        self._build_settings_hotkeys_tab(nb)
        self._build_settings_ghost_tab(nb)

    def _settings_label(self, parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg="#1A1A2E", fg="#7A7A9A",
                        font=(self._config["font_family"], 9))

    def _settings_frame(self, nb: ttk.Notebook, title: str) -> tk.Frame:
        f = tk.Frame(nb, bg="#1A1A2E")
        nb.add(f, text=title)
        return f

    # ── Display tab ───────────────────────────────────────────────────────────

    def _build_settings_display_tab(self, nb: ttk.Notebook) -> None:
        frame = self._settings_frame(nb, "Display")
        canvas = tk.Canvas(frame, bg="#1A1A2E", highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical",
                                  command=canvas.yview)
        inner = tk.Frame(canvas, bg="#1A1A2E")

        inner.bind("<Configure>",
                   lambda e: canvas.configure(
                       scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Toggles per data point ────────────────────────────────────────────
        toggle_items = [
            ("show_timer",           "Race Timer"),
            ("show_race_completion", "Race Completion %"),
            ("show_ghost_delta",     "Ghost Delta"),
            ("show_velocity",        "Velocity"),
            ("show_gear",            "Gear"),
            ("show_rpm",             "Engine RPM"),
            ("show_checkpoint",      "Checkpoint #"),
            ("show_nitro_bar",       "Nitro Bar %"),
            ("show_nitro_state",     "Nitro State"),
            ("show_drift_state",     "Drift State"),
            ("show_360_state",       "360 State"),
            ("show_acceleration",    "Acceleration"),
            ("show_car_angle",       "Car Angle"),
            ("show_car_position",    "Car Position"),
            ("show_camera_angle",    "Camera Angle"),
            ("show_camera_position", "Camera Position"),
        ]

        for cfg_key, label in toggle_items:
            var = tk.BooleanVar(value=self._config.get(cfg_key, False))
            cb = tk.Checkbutton(
                inner, text=label, variable=var,
                bg="#1A1A2E", fg="#E0E0E0", selectcolor="#2E2E4E",
                activebackground="#1A1A2E", activeforeground="#00D4FF",
                font=(self._config["font_family"], 10),
                command=lambda k=cfg_key, v=var: self._config.update({k: v.get()})
            )
            cb.pack(anchor="w", padx=10, pady=1)

        # ── Velocity mode ─────────────────────────────────────────────────────
        tk.Label(inner, text="Velocity Mode", bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10, pady=(10, 0))

        vel_var = tk.StringVar(value=self._config["velocity_mode"])
        for mode_val, mode_label in [
            ("real_total",      "Real Total"),
            ("fake_total",      "Fake Total"),
            ("real_horizontal", "Real Horizontal"),
            ("fake_horizontal", "Fake Horizontal"),
        ]:
            rb = tk.Radiobutton(
                inner, text=mode_label, variable=vel_var, value=mode_val,
                bg="#1A1A2E", fg="#E0E0E0", selectcolor="#2E2E4E",
                activebackground="#1A1A2E",
                font=(self._config["font_family"], 10),
                command=lambda v=vel_var: self._config.update(
                    {"velocity_mode": v.get()})
            )
            rb.pack(anchor="w", padx=20, pady=1)

    # ── Colours tab ───────────────────────────────────────────────────────────

    def _build_settings_colors_tab(self, nb: ttk.Notebook) -> None:
        frame = self._settings_frame(nb, "Colors")

        color_items = [
            ("color_background", "HUD Background"),
            ("color_text",       "Text"),
            ("color_label",      "Label"),
            ("color_highlight",  "Highlight"),
            ("color_ahead",      "Ghost Ahead"),
            ("color_behind",     "Ghost Behind"),
            ("color_equal",      "Ghost Equal"),
        ]

        for cfg_key, label in color_items:
            row = tk.Frame(frame, bg="#1A1A2E")
            row.pack(fill=tk.X, padx=10, pady=3)

            tk.Label(row, text=label, bg="#1A1A2E", fg="#E0E0E0",
                     width=18, anchor="w",
                     font=(self._config["font_family"], 10)).pack(side=tk.LEFT)

            swatch = tk.Label(
                row, bg=self._config.get(cfg_key, "#FFFFFF"),
                width=4, relief=tk.RAISED, cursor="hand2")
            swatch.pack(side=tk.LEFT, padx=4)

            def pick_color(key=cfg_key, sw=swatch):
                initial = self._config.get(key, "#FFFFFF")
                result  = colorchooser.askcolor(color=initial,
                                                title=f"Choose colour – {key}")
                if result and result[1]:
                    self._config[key] = result[1]
                    sw.configure(bg=result[1])

            swatch.bind("<Button-1>", lambda e, fn=pick_color: fn())

        # ── Alpha slider ──────────────────────────────────────────────────────
        tk.Label(frame, text="HUD Transparency",
                 bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10, pady=(12, 0))

        alpha_var = tk.DoubleVar(value=self._config["hud_alpha"])
        alpha_slider = tk.Scale(
            frame, variable=alpha_var, from_=0.2, to=1.0,
            resolution=0.01, orient=tk.HORIZONTAL, length=200,
            bg="#1A1A2E", fg="#E0E0E0", highlightthickness=0,
            troughcolor="#2E2E4E", activebackground="#00D4FF",
            command=lambda v: (
                self._config.update({"hud_alpha": float(v)}),
                self._root.attributes("-alpha", float(v))
            )
        )
        alpha_slider.pack(anchor="w", padx=10)

    # ── Hotkeys tab ───────────────────────────────────────────────────────────

    def _build_settings_hotkeys_tab(self, nb: ttk.Notebook) -> None:
        frame = self._settings_frame(nb, "Hotkeys")

        hotkey_items = [
            ("hotkey_toggle_hud",    "Toggle HUD visibility"),
            ("hotkey_toggle_ghost",  "Toggle ghost comparison"),
            ("hotkey_open_settings", "Open settings"),
        ]

        tk.Label(frame, text="Click a field and press the desired key.",
                 bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10, pady=(6, 4))

        for cfg_key, label in hotkey_items:
            row = tk.Frame(frame, bg="#1A1A2E")
            row.pack(fill=tk.X, padx=10, pady=4)

            tk.Label(row, text=label, bg="#1A1A2E", fg="#E0E0E0",
                     width=22, anchor="w",
                     font=(self._config["font_family"], 10)).pack(side=tk.LEFT)

            entry_var = tk.StringVar(value=self._config.get(cfg_key, ""))
            entry = tk.Entry(row, textvariable=entry_var,
                             bg="#2E2E4E", fg="#E0E0E0",
                             insertbackground="#E0E0E0",
                             font=(self._config["font_family"], 10), width=8)
            entry.pack(side=tk.LEFT)

            def on_key(event, k=cfg_key, ev=entry_var):
                key_name = event.keysym
                ev.set(key_name)
                self._config[k] = key_name
                self._register_hotkeys()
                return "break"

            entry.bind("<KeyPress>", on_key)

    # ── Ghost tab ─────────────────────────────────────────────────────────────

    def _build_settings_ghost_tab(self, nb: ttk.Notebook) -> None:
        frame = self._settings_frame(nb, "Ghost")

        # Current ghost
        ghost_path = self._ghost_manager.get_active_path() or "None"
        self._ghost_path_var = tk.StringVar(value=ghost_path)
        tk.Label(frame, text="Active Ghost:", bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10, pady=(8, 0))
        tk.Label(frame, textvariable=self._ghost_path_var,
                 bg="#1A1A2E", fg="#E0E0E0", wraplength=400, justify=tk.LEFT,
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10)

        # Buttons
        btn_cfg = {
            "bg": "#2E2E4E", "fg": "#E0E0E0", "relief": tk.FLAT,
            "font": (self._config["font_family"], 10),
            "padx": 10, "pady": 4, "cursor": "hand2",
        }
        tk.Button(frame, text="Load Ghost…",
                  command=self._gui_load_ghost, **btn_cfg).pack(
            anchor="w", padx=10, pady=(8, 2))
        tk.Button(frame, text="Create New Ghost…",
                  command=self._gui_create_ghost, **btn_cfg).pack(
            anchor="w", padx=10, pady=2)

        # Split list (for active ghost)
        tk.Label(frame, text="Splits:", bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(
            anchor="w", padx=10, pady=(12, 0))

        self._splits_listbox = tk.Listbox(
            frame, bg="#2E2E4E", fg="#E0E0E0", selectbackground="#00D4FF",
            font=(self._config["font_family"], 10), height=6)
        self._splits_listbox.pack(fill=tk.X, padx=10)
        self._refresh_splits_list()

        split_btns = tk.Frame(frame, bg="#1A1A2E")
        split_btns.pack(anchor="w", padx=10, pady=2)
        tk.Button(split_btns, text="Add Split",
                  command=self._gui_add_split, **btn_cfg).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(split_btns, text="Remove Split",
                  command=self._gui_remove_split, **btn_cfg).pack(side=tk.LEFT)

    def _refresh_splits_list(self) -> None:
        if not hasattr(self, "_splits_listbox"):
            return
        self._splits_listbox.delete(0, tk.END)
        for sp in self._ghost_manager.get_splits():
            self._splits_listbox.insert(
                tk.END,
                f"{sp.get('name','?')}  @  {sp.get('race_completion', 0):.1f}%")

    def _gui_load_ghost(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Ghost File",
            filetypes=[("Ghost JSON", "*.json"), ("All Files", "*.*")])
        if not path:
            return
        try:
            self._ghost_manager.load_ghost(path)
            self._ghost_path_var.set(path)
            self._refresh_splits_list()
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))

    def _gui_create_ghost(self) -> None:
        path = filedialog.asksaveasfilename(
            title="New Ghost File",
            defaultextension=".json",
            filetypes=[("Ghost JSON", "*.json")])
        if not path:
            return

        # Ask whether to configure splits now
        configure = messagebox.askyesno(
            "Configure Splits",
            "Would you like to configure splits for this ghost now?\n"
            "(You can skip and configure them later in Ghost settings.)")
        splits = []
        if configure:
            splits = self._gui_configure_splits_dialog()

        try:
            self._ghost_manager.create_ghost(path, splits)
            self._ghost_path_var.set(path)
            self._refresh_splits_list()
        except Exception as exc:
            messagebox.showerror("Create Error", str(exc))

    def _gui_configure_splits_dialog(self) -> list[dict]:
        """
        Simple modal dialog for entering splits one by one.
        Returns a list of split dicts.
        """
        dialog = tk.Toplevel(self._root)
        dialog.title("Configure Splits")
        dialog.geometry("320x400")
        dialog.configure(bg="#1A1A2E")
        dialog.grab_set()

        splits: list[dict] = []

        tk.Label(dialog, text="Add splits (name and race completion %):",
                 bg="#1A1A2E", fg="#7A7A9A",
                 font=(self._config["font_family"], 9)).pack(padx=10, pady=(8, 2))

        lb = tk.Listbox(dialog, bg="#2E2E4E", fg="#E0E0E0", height=8,
                        font=(self._config["font_family"], 10))
        lb.pack(fill=tk.X, padx=10, pady=4)

        entry_frame = tk.Frame(dialog, bg="#1A1A2E")
        entry_frame.pack(fill=tk.X, padx=10)

        ent_name = tk.Entry(entry_frame, bg="#2E2E4E", fg="#E0E0E0",
                            font=(self._config["font_family"], 10), width=14)
        ent_name.insert(0, "Split 1")
        ent_name.pack(side=tk.LEFT, padx=(0, 4))

        ent_pct = tk.Entry(entry_frame, bg="#2E2E4E", fg="#E0E0E0",
                           font=(self._config["font_family"], 10), width=6)
        ent_pct.insert(0, "33.3")
        ent_pct.pack(side=tk.LEFT)

        def add_split():
            try:
                pct = float(ent_pct.get())
            except ValueError:
                messagebox.showerror("Invalid", "Enter a valid number for %",
                                     parent=dialog)
                return
            name = ent_name.get().strip() or f"Split {len(splits)+1}"
            splits.append({"name": name, "race_completion": pct})
            lb.insert(tk.END, f"{name}  @  {pct:.1f}%")

        tk.Button(dialog, text="Add", command=add_split,
                  bg="#2E2E4E", fg="#E0E0E0", relief=tk.FLAT,
                  font=(self._config["font_family"], 10)).pack(pady=4)

        result: list[dict] = []

        def done():
            result.extend(splits)
            dialog.destroy()

        tk.Button(dialog, text="Done", command=done,
                  bg="#2E2E4E", fg="#00D4FF", relief=tk.FLAT,
                  font=(self._config["font_family"], 10)).pack(pady=4)

        dialog.wait_window()
        return result

    def _gui_add_split(self) -> None:
        splits = self._ghost_manager.get_splits()
        # Reuse the dialog for a single new split
        new = self._gui_configure_splits_dialog()
        splits.extend(new)
        self._ghost_manager.set_splits(splits)
        self._refresh_splits_list()

    def _gui_remove_split(self) -> None:
        sel = self._splits_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        splits = self._ghost_manager.get_splits()
        if 0 <= idx < len(splits):
            splits.pop(idx)
            self._ghost_manager.set_splits(splits)
            self._refresh_splits_list()

    # ── Hotkey registration ───────────────────────────────────────────────────

    def _register_hotkeys(self) -> None:
        """
        Registers global hotkeys using the keyboard library.
        Called on startup and whenever hotkeys are reconfigured in settings.
        """
        try:
            import keyboard as kb  # imported here so missing library is non-fatal
            # Clear previously registered hotkeys
            try:
                kb.unhook_all_hotkeys()
            except Exception:
                pass

            hk_toggle_hud = self._config.get("hotkey_toggle_hud", "F9")
            hk_toggle_ghost = self._config.get("hotkey_toggle_ghost", "F10")
            hk_settings = self._config.get("hotkey_open_settings", "F11")

            if hk_toggle_hud:
                kb.add_hotkey(hk_toggle_hud, self._hotkey_toggle_hud)
            if hk_toggle_ghost:
                kb.add_hotkey(hk_toggle_ghost, self._hotkey_toggle_ghost)
            if hk_settings:
                kb.add_hotkey(hk_settings,
                              lambda: self._root.after(0, self._open_settings))
        except ImportError:
            pass  # keyboard library not installed; hotkeys unavailable  # placeholder

    def _hotkey_toggle_hud(self) -> None:
        """Toggles the HUD window visibility."""
        # Must be called on the main thread via after()
        self._root.after(0, self._do_toggle_hud)

    def _do_toggle_hud(self) -> None:
        if self._root.winfo_viewable():
            self._root.withdraw()
        else:
            self._root.deiconify()

    def _hotkey_toggle_ghost(self) -> None:
        """Toggles the ghost-delta display."""
        self._root.after(0, lambda: self._config.update(
            {"show_ghost_delta": not self._config["show_ghost_delta"]}))
