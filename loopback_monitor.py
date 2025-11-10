import json
import logging
import math
import queue
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None

try:
    import serial  # type: ignore[import-not-found]

    SERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]
    SERIAL_AVAILABLE = False

if SERIAL_AVAILABLE:
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        list_ports = None  # type: ignore[assignment]
else:
    list_ports = None  # type: ignore[assignment]

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover
    import pyaudio


INT16_MAX = 32768.0
AUDIO_FORMAT = getattr(pyaudio, "paInt16", 8)


class LoopbackMonitorApp:
    """Simple 120 FPS RMS monitor using PyAudio loopback on Windows."""

    def __init__(self, root: tk.Tk, target_fps: int = 120) -> None:
        self.root = root
        self.root.title("Loopback RMS Monitor")
        self.root.geometry("360x400")
        self.root.resizable(True, True)
        self.target_fps = target_fps

        self.status_var = tk.StringVar(value="Preparing...")
        self.left_db_var = tk.StringVar(value="-inf dBFS")
        self.right_db_var = tk.StringVar(value="-inf dBFS")
        self.low_band_byte_var = tk.StringVar(value="0")
        self.serial_port_var = tk.StringVar(value="COM3")
        self.serial_baud_var = tk.StringVar(value="115200")
        default_serial_status = "Serial: disconnected" if SERIAL_AVAILABLE else "Serial: pyserial not installed"
        self.serial_status_var = tk.StringVar(value=default_serial_status)

        self.band_config_path = Path(__file__).with_name("band_limits.json")
        self._persisted_limits = self._load_band_limits()

        self.rms_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=24)
        self.status_queue: "queue.Queue[str]" = queue.Queue(maxsize=24)
        self.last_rms = np.zeros(2, dtype=np.float32)
        self._smoothed_rms = np.zeros(2, dtype=np.float32)
        self._last_update_ts: float | None = None

        self.pa: pyaudio.PyAudio | None = None
        self.stream: pyaudio.Stream | None = None
        self.input_device_index: int | None = None
        self.input_device_name = "Unknown"
        self.output_device_name = "Unknown"
        self.using_loopback = False
        self.samplerate = 44100
        self.channels = 2
        self.blocksize = max(128, int(self.samplerate / max(1, self.target_fps)))

        self.band_definitions = [
            {"label": "20-60 Hz", "low": 20.0, "high": 60.0, "color": "#d4af37"},
            {"label": "60-200 Hz", "low": 60.0, "high": 200.0, "color": "#f0c23c"},
            {"label": "200-600 Hz", "low": 200.0, "high": 600.0, "color": "#f7d24c"},
            {"label": "600-2 kHz", "low": 600.0, "high": 2000.0, "color": "#fbe580"},
            {"label": "2-6 kHz", "low": 2000.0, "high": 6000.0, "color": "#fff1a4"},
            {"label": "6-20 kHz", "low": 6000.0, "high": 20000.0, "color": "#fff7d6"},
        ]
        self.band_states: list[dict] = []
        self._create_band_states()
        self.circular_window: "CircularRMSWindow | None" = None
        self.neon_window: "NeonWaveWindow | None" = None
        self.moving_average_var = tk.BooleanVar(value=False)
        self.moving_average_ms_var = tk.DoubleVar(value=100.0)
        self._ma_ms_trace_guard = 0

        self.beat_queue: "queue.Queue[float]" = queue.Queue(maxsize=24)
        self.beat_last = 0.0
        self.beat_detector: "BeatDetectorRNN | None" = None

        self.logger = logging.getLogger(__name__)

        self.serial_conn: Any | None = None
        self.serial_button: ttk.Button | None = None
        self.serial_port_combo: ttk.Combobox | None = None
        self.available_serial_ports: list[str] = []
        self.serial_include_high_var = tk.BooleanVar(value=False)

        self._build_gui()

        try:
            self._initialize_audio()
        except Exception:
            self.logger.exception("Audio setup failed.")
            self.root.after(100, self.root.destroy)
            return

        self._schedule_gui_updates()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_band_states(self) -> None:
        self.band_states = []
        for band in self.band_definitions:
            saved = self._persisted_limits.get(band["label"], {})
            saved_min = float(saved.get("min", 0.0))
            saved_max = float(saved.get("max", 0.3))
            if saved_max < saved_min:
                saved_max = saved_min
            saved_color = saved.get("color")
            if isinstance(saved_color, str) and saved_color.startswith("#") and len(saved_color) == 7:
                color = saved_color.lower()
            else:
                color = band["color"]
            state = {
                "label": band["label"],
                "low_cut": float(band["low"]),
                "high_cut": float(band["high"]),
                "color": color,
                "queue": queue.Queue(maxsize=24),
                "last": 0.0,
                "raw_last": 0.0,
                "smoothed_last": None,
                "min": saved_min,
                "max": saved_max,
                "window": None,
                "min_var": None,
                "max_var": None,
                "hp_alpha": 0.0,
                "lp_alpha": 0.0,
                "hp_prev_input": None,
                "hp_prev_output": None,
                "lp_prev_output": None,
                "gradient": bool(saved.get("gradient", False)),
            }
            self.band_states.append(state)
        self._save_band_limits()

    def _build_gui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.grid(column=0, row=0, sticky="nsew")

        ttk.Label(main_frame, text="120 FPS RMS meters (L / R)").grid(
            column=0, row=0, columnspan=2, sticky="w"
        )

        self.left_meter = ttk.Progressbar(main_frame, orient="horizontal", length=260, maximum=100)
        self.left_meter.grid(column=0, row=1, sticky="ew", padx=(0, 8))
        ttk.Label(main_frame, textvariable=self.left_db_var, width=10, anchor="e").grid(
            column=1, row=1, sticky="e"
        )

        self.right_meter = ttk.Progressbar(main_frame, orient="horizontal", length=260, maximum=100)
        self.right_meter.grid(column=0, row=2, sticky="ew", padx=(0, 8), pady=(6, 0))
        ttk.Label(main_frame, textvariable=self.right_db_var, width=10, anchor="e").grid(
            column=1, row=2, sticky="e", pady=(6, 0)
        )

        self.status_label = ttk.Label(main_frame, textvariable=self.status_var, anchor="w")
        self.status_label.grid(column=0, row=3, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Label(main_frame, text="20-60 Hz (0-255)").grid(column=0, row=4, sticky="w", pady=(6, 0))
        ttk.Label(main_frame, textvariable=self.low_band_byte_var, width=6, anchor="e").grid(
            column=1, row=4, sticky="e", pady=(6, 0)
        )

        serial_frame = ttk.LabelFrame(main_frame, text="Micro:bit Serial")
        serial_frame.grid(column=0, row=5, columnspan=2, sticky="ew", pady=(10, 0))
        serial_frame.columnconfigure(1, weight=1)

        ttk.Label(serial_frame, text="Port").grid(column=0, row=0, sticky="w")
        self.serial_port_combo = ttk.Combobox(
            serial_frame,
            textvariable=self.serial_port_var,
            width=10,
            values=self.available_serial_ports,
            state="normal",
        )
        self.serial_port_combo.grid(column=1, row=0, sticky="ew", padx=(6, 6))

        ttk.Label(serial_frame, text="Baud").grid(column=2, row=0, sticky="w")
        baud_entry = ttk.Entry(serial_frame, textvariable=self.serial_baud_var, width=8)
        baud_entry.grid(column=3, row=0, sticky="ew")

        refresh_button = ttk.Button(serial_frame, text="Refresh", command=self._refresh_serial_ports)
        refresh_button.grid(column=4, row=0, padx=(6, 0))

        self.serial_button = ttk.Button(serial_frame, text="Connect", command=self._toggle_serial_connection)
        self.serial_button.grid(column=5, row=0, padx=(6, 0))
        if not SERIAL_AVAILABLE:
            if self.serial_port_combo is not None:
                self.serial_port_combo.configure(state="disabled")
            baud_entry.configure(state="disabled")
            self.serial_button.state(["disabled"])
            refresh_button.state(["disabled"])

        ttk.Label(serial_frame, textvariable=self.serial_status_var).grid(
            column=0, row=1, columnspan=6, sticky="w", pady=(6, 0)
        )

        ttk.Checkbutton(
            serial_frame,
            text="Include 6-20 kHz band",
            variable=self.serial_include_high_var,
        ).grid(column=0, row=2, columnspan=6, sticky="w", pady=(4, 0))

        button_frame = ttk.Frame(main_frame)
        button_frame.grid(column=0, row=6, columnspan=2, sticky="ew", pady=(10, 0))
        button_frame.columnconfigure(1, weight=1)

        ttk.Label(button_frame, text="Band").grid(column=0, row=0, padx=(0, 8), sticky="w")
        ttk.Label(button_frame, text="Min RMS").grid(column=1, row=0, padx=(0, 6), sticky="w")
        ttk.Label(button_frame, text="Max RMS").grid(column=2, row=0, padx=(0, 6), sticky="w")
        ttk.Label(button_frame, text="Color").grid(column=3, row=0, padx=(6, 0), sticky="w")
        ttk.Label(button_frame, text="Gradient").grid(column=4, row=0, padx=(6, 0), sticky="w")

        for idx, state in enumerate(self.band_states):
            pady = (10, 0) if idx == 0 else (6, 0)
            ttk.Button(
                button_frame,
                text=f"{state['label']} Monitor",
                command=lambda st=state: self._show_band_window(st),
            ).grid(column=0, row=idx + 1, sticky="ew", pady=pady)

            min_var = tk.DoubleVar()
            max_var = tk.DoubleVar()
            min_var.set(f"{state['min']:.4f}")
            max_var.set(f"{state['max']:.4f}")
            min_entry = ttk.Entry(button_frame, textvariable=min_var, width=7)
            max_entry = ttk.Entry(button_frame, textvariable=max_var, width=7)
            min_entry.grid(column=1, row=idx + 1, sticky="ew", padx=(0, 6), pady=pady)
            max_entry.grid(column=2, row=idx + 1, sticky="ew", padx=(0, 6), pady=pady)
            state["min_var"] = min_var
            state["max_var"] = max_var

            if idx == 0:
                self.low_band_color_button = tk.Button(
                    button_frame,
                    text="Pick",
                    command=lambda st=state: self._choose_band_color(st),
                    relief="raised",
                    borderwidth=1,
                    padx=6,
                )
                self.low_band_color_button.grid(column=3, row=idx + 1, sticky="ew", padx=(6, 0), pady=pady)
                self._update_low_band_button_color(state["color"])

                self.low_band_gradient_var = tk.BooleanVar(value=state.get("gradient", False))
                gradient_cb = ttk.Checkbutton(
                    button_frame,
                    variable=self.low_band_gradient_var,
                    command=lambda st=state: self._apply_gradient_toggle(st),
                )
                gradient_cb.grid(column=4, row=idx + 1, sticky="w", padx=(6, 0), pady=pady)

            def make_apply(st, min_var=min_var, max_var=max_var) -> None:
                def apply_limits(*_args) -> None:
                    try:
                        min_val = float(min_var.get())
                        max_val = float(max_var.get())
                    except ValueError:
                        return
                    self._set_band_limits(st, min_val, max_val)
                return apply_limits

            apply_fn = make_apply(state)
            min_entry.bind("<Return>", apply_fn)
            min_entry.bind("<FocusOut>", apply_fn)
            max_entry.bind("<Return>", apply_fn)
            max_entry.bind("<FocusOut>", apply_fn)
            min_entry.bind(
                "<MouseWheel>",
                lambda event, st=state, var=min_var: self._on_scroll_limits(event, st, var, True),
            )
            max_entry.bind(
                "<MouseWheel>",
                lambda event, st=state, var=max_var: self._on_scroll_limits(event, st, var, False),
            )

        for col in range(3):
            button_frame.columnconfigure(col, weight=1)
        button_frame.columnconfigure(3, weight=0)
        button_frame.columnconfigure(4, weight=0)

        for i in range(2):
            main_frame.columnconfigure(i, weight=1)

        ttk.Button(
            main_frame,
            text="Circular 20-60 Hz Monitor",
            command=self._show_circular_window,
        ).grid(column=0, row=6 + len(self.band_states), columnspan=2, sticky="ew", pady=(10, 0))

        addon_frame = ttk.Frame(main_frame)
        addon_frame.grid(column=0, row=7 + len(self.band_states), columnspan=2, sticky="ew", pady=(6, 0))
        addon_frame.columnconfigure(1, weight=1)
        ttk.Label(addon_frame, text="Include 6-20 kHz overlay").grid(column=0, row=0, sticky="w")
        self.circular_include_high_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(addon_frame, variable=self.circular_include_high_var).grid(column=1, row=0, sticky="w")
        ttk.Label(addon_frame, text="Enable RMS smoothing").grid(column=0, row=1, sticky="w", pady=(6, 0))
        self.moving_average_var.trace_add("write", self._on_moving_average_toggle)
        ttk.Checkbutton(addon_frame, variable=self.moving_average_var).grid(column=1, row=1, sticky="w", pady=(6, 0))
        ttk.Label(addon_frame, text="Window (ms, scroll +/-1)").grid(column=0, row=2, sticky="w", pady=(6, 0))
        self.moving_average_ms_var.trace_add("write", self._on_moving_average_ms_change)
        self.moving_average_entry = ttk.Entry(addon_frame, textvariable=self.moving_average_ms_var, width=6)
        self.moving_average_entry.grid(column=1, row=2, sticky="w", pady=(6, 0))
        self.moving_average_entry.bind("<FocusOut>", self._normalize_moving_average_ms)
        self.moving_average_entry.bind("<Return>", self._normalize_moving_average_ms)
        self.moving_average_entry.bind("<MouseWheel>", self._on_scroll_moving_average_ms)

        ttk.Button(
            main_frame,
            text="Neon Energy Wave",
            command=self._show_neon_window,
        ).grid(column=0, row=8 + len(self.band_states), columnspan=2, sticky="ew", pady=(10, 0))

        self._refresh_serial_ports()

    def _set_serial_status(self, message: str) -> None:
        self.serial_status_var.set(f"Serial: {message}")

    def _toggle_serial_connection(self) -> None:
        if self.serial_conn is None:
            self._connect_serial()
        else:
            self._disconnect_serial()

    def _refresh_serial_ports(self) -> None:
        if not SERIAL_AVAILABLE or list_ports is None:
            ports: list[str] = []
        else:
            try:
                ports = [info.device for info in list_ports.comports()]
            except Exception as exc:  # pragma: no cover - hardware dependent
                self.logger.warning("Failed to enumerate COM ports: %s", exc)
                ports = []
        self.available_serial_ports = ports
        if self.serial_port_combo is not None:
            self.serial_port_combo["values"] = ports

        if self.serial_conn is not None:
            return

        if ports:
            current_port = self.serial_port_var.get().strip()
            if not current_port:
                self.serial_port_var.set(ports[0])
            self._set_serial_status(f"Disconnected ({len(ports)} port(s) found)")
        else:
            self._set_serial_status("No ports found")

    def _connect_serial(self) -> None:
        if not SERIAL_AVAILABLE or serial is None:
            messagebox.showerror(
                "Serial Unavailable",
                "pyserial is not installed. Please install pyserial to enable serial output.",
                parent=self.root,
            )
            return

        if self.serial_conn is not None:
            return

        self._refresh_serial_ports()

        port = self.serial_port_var.get().strip()
        if not port:
            self._set_serial_status("Port required")
            messagebox.showwarning("Serial Port", "Please enter a COM port name (e.g., COM3).", parent=self.root)
            return

        try:
            baud = int(str(self.serial_baud_var.get()).strip())
        except (TypeError, ValueError):
            self._set_serial_status("Invalid baud rate")
            messagebox.showwarning("Serial Baud", "Please enter a valid integer baud rate (e.g., 115200).", parent=self.root)
            return

        try:
            self.serial_conn = serial.Serial(port=port, baudrate=baud, timeout=0, write_timeout=0.1)
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.serial_conn = None
            self.logger.warning("Serial connection failed: %s", exc)
            self._set_serial_status("Connection failed")
            messagebox.showerror(
                "Serial Error",
                f"Could not open serial port {port}:\n{exc}",
                parent=self.root,
            )
            return

        self._set_serial_status(f"Connected ({port})")
        if self.serial_button is not None:
            self.serial_button.configure(text="Disconnect")

    def _disconnect_serial(self) -> None:
        conn = self.serial_conn
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # pragma: no cover - hardware dependent
                self.logger.warning("Error while closing serial port: %s", exc)
            finally:
                self.serial_conn = None
        self._set_serial_status("Disconnected")
        if self.serial_button is not None:
            self.serial_button.configure(text="Connect")
        self.serial_include_high_var.set(False)

    def _send_serial_values(self, values: list[int | None]) -> None:
        conn = self.serial_conn
        if conn is None:
            return
        if not values:
            return
        parts: list[str] = []
        for value in values:
            if value is None:
                continue
            safe_value = max(0, min(255, int(value)))
            parts.append(str(safe_value))
        if not parts:
            return
        payload = ",".join(parts) + "\n"
        try:
            conn.write(payload.encode("ascii"))
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.logger.warning("Serial write failed: %s", exc)
            self._set_serial_status("Write failed; disconnected")
            self._disconnect_serial()

    def _show_band_window(self, state: dict) -> None:
        window = state["window"]
        if window is not None and window.winfo_exists():
            window.lift()
            return

        window = BandWindow(
            self.root,
            title=f"{state['label']} RMS",
            rms_to_db=self._rms_to_db,
            get_limits=lambda st=state: self._get_band_limits(st),
            set_limits=lambda min_val, max_val, st=state: self._set_band_limits(st, min_val, max_val),
            base_color=state["color"],
            gradient_mode=state.get("gradient", False),
            on_close=lambda st=state: self._on_band_window_closed(st),
        )
        state["window"] = window
        window.update_level(state["last"])
        window.update_beat(self.beat_last)

    def _show_circular_window(self) -> None:
        low_band_state = self.band_states[0]
        high_band_state = self.band_states[-1]
        if self.circular_window is not None and self.circular_window.winfo_exists():
            self.circular_window.lift()
            return
        self.circular_window = CircularRMSWindow(
            self.root,
            title="Circular RMS (20-60 Hz)",
            get_limits=lambda: self._get_band_limits(low_band_state),
            get_high_band_limits=lambda: self._get_band_limits(high_band_state),
            include_high_var=self.circular_include_high_var,
            base_color=low_band_state["color"],
            high_color=high_band_state["color"],
            gradient_mode=low_band_state.get("gradient", False),
            on_close=self._on_circular_window_closed,
        )
        self.circular_window.update_levels(low_band_state["last"], high_band_state["last"])
        self.circular_window.update_beat(self.beat_last)

    def _show_neon_window(self) -> None:
        energy_state = self.band_states[0]
        if self.neon_window is not None and self.neon_window.winfo_exists():
            self.neon_window.lift()
            return
        self.neon_window = NeonWaveWindow(
            self.root,
            title="Neon Energy Wave",
            get_limits=lambda: self._get_band_limits(energy_state),
            on_close=self._on_neon_window_closed,
        )
        initial_level = float(np.mean(self.last_rms)) if hasattr(self.last_rms, "__len__") else float(self.last_rms)
        self.neon_window.update_wave(initial_level, self.beat_last, self._last_gui_dt)

    def _get_band_limits(self, state: dict) -> tuple[float, float]:
        return state["min"], state["max"]

    def _set_band_limits(self, state: dict, min_val: float, max_val: float) -> None:
        min_val = max(0.0, round(float(min_val), 4))
        max_val = round(float(max_val), 4)
        if max_val < min_val:
            max_val = min_val

        state["min"] = min_val
        state["max"] = max_val
        if state.get("min_var") is not None:
            state["min_var"].set(f"{state['min']:.4f}")
        if state.get("max_var") is not None:
            state["max_var"].set(f"{state['max']:.4f}")

        self.logger.debug(
            "Band limits updated for %s: min=%.4f max=%.4f",
            state["label"],
            min_val,
            max_val,
        )
        self._save_band_limits()

    def _on_band_window_closed(self, state: dict) -> None:
        state["window"] = None

    def _on_circular_window_closed(self) -> None:
        self.circular_window = None

    def _on_neon_window_closed(self) -> None:
        self.neon_window = None

    def _choose_band_color(self, state: dict) -> None:
        initial = state.get("color", "#d4af37")
        _rgb, hex_color = colorchooser.askcolor(color=initial, title=f"{state['label']} Color", parent=self.root)
        if not hex_color:
            return
        self._set_band_color(state, hex_color)

    def _set_band_color(self, state: dict, color: str) -> None:
        if not isinstance(color, str):
            return
        color = color.strip().lower()
        if not color.startswith("#") or len(color) != 7:
            return
        if state.get("color") == color:
            return
        state["color"] = color
        for band in self.band_definitions:
            if band["label"] == state["label"]:
                band["color"] = color
                break
        window = state.get("window")
        if window is not None and window.winfo_exists():
            window.set_base_color(color)
        if state is self.band_states[0]:
            if hasattr(self, "low_band_color_button"):
                self._update_low_band_button_color(color)
            if self.circular_window is not None and self.circular_window.winfo_exists():
                self.circular_window.set_base_color(color)
        self._save_band_limits()

    def _apply_gradient_toggle(self, state: dict) -> None:
        if state is self.band_states[0]:
            desired = bool(self.low_band_gradient_var.get())
        else:
            desired = False
        self._set_band_gradient(state, desired)

    def _set_band_gradient(self, state: dict, enabled: bool) -> None:
        enabled = bool(enabled)
        if state.get("gradient", False) == enabled:
            return
        state["gradient"] = enabled
        window = state.get("window")
        if window is not None and window.winfo_exists():
            window.set_gradient_mode(enabled)
        if state is self.band_states[0]:
            if hasattr(self, "low_band_gradient_var") and self.low_band_gradient_var.get() != enabled:
                self.low_band_gradient_var.set(enabled)
            if self.circular_window is not None and self.circular_window.winfo_exists():
                self.circular_window.set_gradient_mode(enabled)
        self._save_band_limits()

    def _update_low_band_button_color(self, color: str) -> None:
        if not hasattr(self, "low_band_color_button"):
            return
        button = self.low_band_color_button
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
        except (ValueError, TypeError, IndexError):
            r = g = b = 180
        brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        fg = "#000000" if brightness > 0.55 else "#ffffff"
        button.configure(bg=color, activebackground=color, fg=fg, activeforeground=fg)

    def _on_moving_average_toggle(self, *_args) -> None:
        self._reset_moving_average_state()

    def _reset_moving_average_state(self) -> None:
        self._last_update_ts = None
        self._smoothed_rms = np.array(self.last_rms, dtype=np.float32, copy=True)
        for state in self.band_states:
            state["smoothed_last"] = state["raw_last"]

    def _set_moving_average_ms(self, value: float) -> float:
        clamped = max(1.0, min(2000.0, round(float(value))))
        self._ma_ms_trace_guard += 1
        self.moving_average_ms_var.set(int(clamped))
        if hasattr(self, "root"):
            self.root.after_idle(self._release_moving_average_guard)
        return clamped

    def _release_moving_average_guard(self) -> None:
        if self._ma_ms_trace_guard > 0:
            self._ma_ms_trace_guard -= 1

    def _on_moving_average_ms_change(self, *_args) -> None:
        if self._ma_ms_trace_guard > 0:
            return
        try:
            value = float(self.moving_average_ms_var.get())
        except (tk.TclError, ValueError):
            value = 100.0
        clamped = max(1.0, min(2000.0, round(value)))
        if abs(clamped - value) > 1e-6:
            self._set_moving_average_ms(clamped)
            return
        if self.moving_average_var.get():
            self._reset_moving_average_state()

    def _normalize_moving_average_ms(self, _event=None) -> None:
        try:
            value = float(self.moving_average_ms_var.get())
        except (tk.TclError, ValueError):
            value = 100.0
        self._set_moving_average_ms(value)
        if self.moving_average_var.get():
            self._reset_moving_average_state()
        return "break"

    def _on_scroll_moving_average_ms(self, event) -> None:
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return "break"
        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1
        try:
            base_value = float(self.moving_average_ms_var.get())
        except (tk.TclError, ValueError):
            base_value = 100.0
        new_value = base_value + steps * 1.0
        self._set_moving_average_ms(new_value)
        if self.moving_average_var.get():
            self._reset_moving_average_state()
        return "break"

    def _on_scroll_limits(self, event, state: dict, var: tk.DoubleVar, is_min: bool) -> None:
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1
        step = 0.0001 * steps
        base_value = state["min"] if is_min else state["max"]
        new_value = round(base_value + step, 4)
        if is_min:
            min_val = max(0.0, new_value)
            max_val = max(state["max"], min_val)
            self._set_band_limits(state, min_val, max_val)
        else:
            min_val = state["min"]
            max_val = max(min_val, new_value)
            self._set_band_limits(state, min_val, max_val)

        window = state.get("window")
        if window is not None and window.winfo_exists():
            window.update_level(state["last"])
            window.update_beat(self.beat_last)

    def _load_band_limits(self) -> dict:
        if not self.band_config_path.exists():
            return {}
        try:
            with self.band_config_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, dict[str, float]] = {}
        for label, values in data.items():
            try:
                result[label] = {
                    "min": float(values.get("min", 0.0)),
                    "max": float(values.get("max", 0.3)),
                }
            except (TypeError, ValueError):
                continue
        return result

    def _save_band_limits(self) -> None:
        try:
            data = {}
            for state in self.band_states:
                data[state["label"]] = {
                    "min": state["min"],
                    "max": state["max"],
                    "color": state.get("color"),
                    "gradient": bool(state.get("gradient", False)),
                }
            with self.band_config_path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            self.logger.exception("Failed to save band limits.")

    def _initialize_audio(self) -> None:
        try:
            if self.pa is None:
                self.pa = pyaudio.PyAudio()
        except Exception as exc:
            messagebox.showerror("Audio Error", f"PyAudio could not start:\n{exc}")
            raise

        (
            self.input_device_index,
            self.input_device_name,
            self.channels,
            self.samplerate,
            self.using_loopback,
            self.output_device_name,
        ) = self._select_device()

        self.blocksize = max(64, int(self.samplerate / max(1, self.target_fps)))
        params: dict[str, object] = {
            "format": AUDIO_FORMAT,
            "channels": self.channels,
            "rate": self.samplerate,
            "input": True,
            "frames_per_buffer": self.blocksize,
            "stream_callback": self._audio_callback,
        }
        if self.input_device_index is not None:
            params["input_device_index"] = self.input_device_index

        try:
            self.stream = self.pa.open(**params)
            self.stream.start_stream()
        except Exception as exc:
            messagebox.showerror("Audio Error", f"Audio stream could not start:\n{exc}")
            raise

        self._configure_band_filters()
        self.beat_last = 0.0
        if TORCH_AVAILABLE:
            try:
                self.beat_detector = BeatDetectorRNN(self.channels)
                self.logger.info("BeatDetectorRNN initialized.")
            except Exception as exc:
                self.beat_detector = None
                self.logger.warning("Beat detector initialization failed: %s", exc)
        else:
            self.logger.warning("PyTorch not available; beat detector disabled.")

        desc = "loopback" if self.using_loopback else "input"
        self.status_var.set(
            f"Device: {self.input_device_name} ({desc}), {self.samplerate} Hz"
        )
        self.logger.info(
            "Stream started - output=%s input=%s loopback=%s samplerate=%s blocksize=%s",
            self.output_device_name,
            self.input_device_name,
            self.using_loopback,
            self.samplerate,
            self.blocksize,
        )

    def _select_device(
        self,
    ) -> tuple[int | None, str, int, int, bool, str]:
        """Return device info preferring WASAPI loopback when available."""
        assert self.pa is not None
        pa = self.pa
        output_name = "Default Output"

        if hasattr(pyaudio, "paWASAPI"):
            try:
                wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_out_idx = wasapi_info.get("defaultOutputDevice", -1)
                if default_out_idx != -1:
                    default_out = pa.get_device_info_by_index(default_out_idx)
                    output_name = default_out.get("name", output_name)
                    sample_rate = int(default_out.get("defaultSampleRate", 44100))
                    channels = max(1, min(2, default_out.get("maxInputChannels", 2)))
                    index = default_out_idx
                    input_name = default_out.get("name", "Loopback")

                    if not default_out.get("isLoopbackDevice", False):
                        generator = getattr(pa, "get_loopback_device_info_generator", None)
                        if generator is not None:
                            for loopback in generator():
                                loop_name = loopback.get("name", "")
                                if output_name.lower() in loop_name.lower():
                                    index = loopback["index"]
                                    input_name = loopback["name"]
                                    channels = max(
                                        1, min(2, loopback.get("maxInputChannels", channels))
                                    )
                                    break
                        else:
                            self.logger.warning(
                                "Loopback generator not available; install pyaudiowpatch for better support."
                            )

                    self.logger.debug(
                        "Loopback device selected: output=%s input=%s index=%s channels=%s samplerate=%s",
                        output_name,
                        input_name,
                        index,
                        channels,
                        sample_rate,
                    )
                    return index, input_name, channels, sample_rate, True, output_name
            except Exception as exc:
                self.logger.warning("Loopback search failed: %s", exc, exc_info=True)

        # Fallback to default input
        try:
            default_input = pa.get_default_input_device_info()
        except Exception as exc:
            messagebox.showerror("Audio Error", f"No input device found:\n{exc}")
            raise

        index = default_input.get("index")
        input_name = default_input.get("name", "Default Input")
        sample_rate = int(default_input.get("defaultSampleRate", 44100))
        channels = max(1, min(2, default_input.get("maxInputChannels", 2)))
        self.logger.warning(
            "Loopback device not found; using default input: %s (index=%s)",
            input_name,
            index,
        )
        return index, input_name, channels, sample_rate, False, output_name

    def _audio_callback(self, in_data, frame_count, time_info, status_flags):
        if status_flags:
            msg = f"Callback flags: {status_flags}"
            try:
                self.status_queue.put_nowait(msg)
            except queue.Full:
                pass
            self.logger.warning(msg)

        if not in_data:
            return (None, pyaudio.paContinue)

        try:
            raw = np.frombuffer(in_data, dtype=np.int16)
            band_values = [0.0] * len(self.band_states)
            if raw.size == 0:
                rms = np.zeros(2, dtype=np.float32)
            else:
                audio = raw.reshape(-1, self.channels).astype(np.float32) / INT16_MAX
                full_rms = np.sqrt(np.mean(np.square(audio), axis=0))
                if full_rms.size == 1:
                    rms = np.repeat(full_rms, 2)
                else:
                    rms = full_rms[:2]

                for idx, state in enumerate(self.band_states):
                    band_values[idx] = self._process_band(audio, state)

            for value, state in zip(band_values, self.band_states):
                try:
                    state["queue"].put_nowait(value)
                except queue.Full:
                    pass

            low_value = band_values[0] if band_values else 0.0
            if self.beat_detector is not None:
                beat_prob = self.beat_detector.process(low_value)
            else:
                beat_prob = float(min(1.0, max(0.0, low_value * 5.0)))
            try:
                self.beat_queue.put_nowait(beat_prob)
            except queue.Full:
                pass
        except ValueError:
            rms = np.zeros(2, dtype=np.float32)
            band_values = [0.0] * len(self.band_states)
            for value, state in zip(band_values, self.band_states):
                try:
                    state["queue"].put_nowait(value)
                except queue.Full:
                    pass
            if self.beat_detector is not None:
                beat_prob = self.beat_detector.process(0.0)
            else:
                beat_prob = 0.0
            try:
                self.beat_queue.put_nowait(beat_prob)
            except queue.Full:
                pass

        try:
            self.rms_queue.put_nowait(rms)
        except queue.Full:
            pass

        return (None, pyaudio.paContinue)

    def _configure_band_filters(self) -> None:
        for state in self.band_states:
            self._reset_band_filter(state)

    def _reset_band_filter(self, state: dict) -> None:
        state["hp_alpha"] = self._highpass_alpha(state["low_cut"])
        state["lp_alpha"] = self._lowpass_alpha(state["high_cut"])
        state["hp_prev_input"] = np.zeros(self.channels, dtype=np.float32)
        state["hp_prev_output"] = np.zeros(self.channels, dtype=np.float32)
        state["lp_prev_output"] = np.zeros(self.channels, dtype=np.float32)
        state["last"] = 0.0

    def _process_band(self, audio: np.ndarray, state: dict) -> float:
        if audio.size == 0:
            return 0.0

        if (
            state["hp_prev_input"] is None
            or state["hp_prev_output"] is None
            or state["lp_prev_output"] is None
        ):
            self._reset_band_filter(state)

        high_passed = np.empty_like(audio)
        hp_input = state["hp_prev_input"]
        hp_output = state["hp_prev_output"]
        alpha_hp = state["hp_alpha"]

        for ch in range(audio.shape[1]):
            prev_in = hp_input[ch]
            prev_out = hp_output[ch]
            channel_data = audio[:, ch]
            out_channel = high_passed[:, ch]
            for idx, sample in enumerate(channel_data):
                filtered = alpha_hp * (prev_out + sample - prev_in)
                out_channel[idx] = filtered
                prev_out = filtered
                prev_in = sample
            hp_input[ch] = prev_in
            hp_output[ch] = prev_out

        bandpassed = np.empty_like(high_passed)
        lp_output = state["lp_prev_output"]
        alpha_lp = state["lp_alpha"]

        for ch in range(high_passed.shape[1]):
            prev = lp_output[ch]
            channel_data = high_passed[:, ch]
            out_channel = bandpassed[:, ch]
            for idx, sample in enumerate(channel_data):
                prev = prev + alpha_lp * (sample - prev)
                out_channel[idx] = prev
            lp_output[ch] = prev

        rms_vals = np.sqrt(np.mean(np.square(bandpassed), axis=0))
        return float(np.mean(rms_vals))

    def _highpass_alpha(self, cutoff: float) -> float:
        dt = 1.0 / self.samplerate
        rc = 1.0 / (2.0 * math.pi * cutoff)
        return rc / (rc + dt)

    def _lowpass_alpha(self, cutoff: float) -> float:
        dt = 1.0 / self.samplerate
        rc = 1.0 / (2.0 * math.pi * cutoff)
        return dt / (rc + dt)

    def _schedule_gui_updates(self) -> None:
        interval_ms = max(1, round(1000 / max(1, self.target_fps)))
        self._update_gui()
        self.root.after(interval_ms, self._schedule_gui_updates)

    def _update_gui(self) -> None:
        now = time.perf_counter()
        if self._last_update_ts is None:
            dt = 1.0 / max(1, self.target_fps)
        else:
            dt = max(1e-4, now - self._last_update_ts)
        self._last_update_ts = now

        use_moving_average = self.moving_average_var.get()
        if use_moving_average:
            try:
                window_ms = float(self.moving_average_ms_var.get())
            except (tk.TclError, ValueError):
                window_ms = 100.0
            window_ms = max(1.0, min(2000.0, window_ms))
            window_seconds = window_ms / 1000.0
            alpha = 1.0 - math.exp(-dt / max(1e-6, window_seconds))
        else:
            alpha = 1.0
        self._last_gui_dt = dt

        try:
            while True:
                rms = self.rms_queue.get_nowait()
                self.last_rms = rms
        except queue.Empty:
            pass

        for state in self.band_states:
            queue_ref = state["queue"]
            updated = False
            try:
                while True:
                    value = queue_ref.get_nowait()
                    state["raw_last"] = value
                    updated = True
            except queue.Empty:
                value = state["raw_last"]

            if not updated:
                value = state["raw_last"]

            if use_moving_average:
                if state["smoothed_last"] is None:
                    state["smoothed_last"] = value
                else:
                    state["smoothed_last"] = (1.0 - alpha) * state["smoothed_last"] + alpha * value
                state["last"] = state["smoothed_last"]
            else:
                state["smoothed_last"] = None
                state["last"] = value

        if use_moving_average:
            self._smoothed_rms = (1.0 - alpha) * self._smoothed_rms + alpha * self.last_rms
            display_rms = self._smoothed_rms
        else:
            self._smoothed_rms = self.last_rms.copy()
            display_rms = self.last_rms

        try:
            while True:
                beat_value = self.beat_queue.get_nowait()
                self.beat_last = beat_value
        except queue.Empty:
            pass

        left, right = (float(x) for x in display_rms)

        left_db = self._rms_to_db(left)
        right_db = self._rms_to_db(right)

        self.left_db_var.set(self._db_text(left_db))
        self.right_db_var.set(self._db_text(right_db))

        self.left_meter["value"] = self._db_to_meter(left_db)
        self.right_meter["value"] = self._db_to_meter(right_db)
        energy_level = float(np.mean(display_rms))

        serial_values: list[int | None] = []
        if self.band_states:
            low_state = self.band_states[0]
            min_val, max_val = low_state["min"], low_state["max"]
            if max_val <= min_val:
                byte_value = 0
            else:
                normalized = (float(low_state["last"]) - min_val) / (max_val - min_val)
                normalized = max(0.0, min(1.0, normalized))
                byte_value = int(round(normalized * 255.0))
            self.low_band_byte_var.set(str(byte_value))
            serial_values.append(byte_value)

            if self.serial_include_high_var.get() and len(self.band_states) > 1:
                high_state = self.band_states[-1]
                h_min, h_max = high_state["min"], high_state["max"]
                if h_max <= h_min:
                    high_byte = 0
                else:
                    h_norm = (float(high_state["last"]) - h_min) / (h_max - h_min)
                    h_norm = max(0.0, min(1.0, h_norm))
                    high_byte = int(round(h_norm * 255.0))
                serial_values.append(high_byte)

            self._send_serial_values(serial_values)
        else:
            self.low_band_byte_var.set("0")

        for state in self.band_states:
            window = state.get("window")
            if window is not None and window.winfo_exists():
                window.update_level(state["last"])
                window.update_beat(self.beat_last)

        if self.circular_window is not None and self.circular_window.winfo_exists():
            low_state = self.band_states[0]
            high_state = self.band_states[-1]
            self.circular_window.update_levels(low_state["last"], high_state["last"])
            self.circular_window.update_beat(self.beat_last)

        if self.neon_window is not None and self.neon_window.winfo_exists():
            self.neon_window.update_wave(energy_level, self.beat_last, self._last_gui_dt)

        try:
            status_msg = self.status_queue.get_nowait()
            self.status_var.set(status_msg)
        except queue.Empty:
            pass

    @staticmethod
    def _rms_to_db(value: float) -> float:
        value = max(value, 1e-10)
        return 20.0 * math.log10(value)

    @staticmethod
    def _db_text(db_value: float) -> str:
        if db_value <= -120.0:
            return "-inf dBFS"
        return f"{db_value:5.1f} dBFS"

    @staticmethod
    def _db_to_meter(db_value: float, floor: float = -60.0) -> float:
        clamped = max(floor, min(0.0, db_value))
        return ((clamped - floor) / -floor) * 100.0

    def _on_close(self) -> None:
        self._save_band_limits()
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            self.logger.exception("Error while stopping stream.")
        finally:
            self.stream = None

        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                self.logger.exception("Error while terminating PyAudio.")
            finally:
                self.pa = None

        for state in self.band_states:
            window = state.get("window")
            if window is not None and window.winfo_exists():
                window.destroy()
            state["window"] = None

        if self.circular_window is not None and self.circular_window.winfo_exists():
            self.circular_window.destroy()
        self.circular_window = None

        if self.neon_window is not None and self.neon_window.winfo_exists():
            self.neon_window.destroy()
        self.neon_window = None

        self._disconnect_serial()

        self.root.destroy()
        self.logger.info("Application closed.")


if TORCH_AVAILABLE:

    class BeatNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.gru = nn.GRU(input_size=1, hidden_size=8, batch_first=True)
            self.fc = nn.Linear(8, 1)

        def forward(self, x: torch.Tensor, hidden: torch.Tensor | None = None):
            out, hidden = self.gru(x, hidden)
            y = torch.sigmoid(self.fc(out))
            return y, hidden


    class BeatDetectorRNN:
        """Small GRU-based beat detector trained offline on synthetic pulses."""

        def __init__(self, channels: int) -> None:
            self.channels = channels
            self.device = torch.device("cpu")
            self.model = BeatNet().to(self.device)
            self.model.eval()
            self._load_weights()
            self.hidden = torch.zeros(1, 1, 8, dtype=torch.float32, device=self.device)
            self.visual_value = 0.0

        def reset(self) -> None:
            self.hidden.zero_()
            self.visual_value = 0.0

        def _load_weights(self) -> None:
            with torch.no_grad():
                self.model.gru.weight_ih_l0.copy_(
                    torch.tensor(
                        [
                            [-1.148892],
                            [-0.7618658],
                            [-0.4945977],
                            [0.47545704],
                            [2.28191],
                            [-1.4810909],
                            [0.3239962],
                            [2.215945],
                            [-1.284124],
                            [-0.29086646],
                            [-1.1755124],
                            [-0.4562066],
                            [-1.8371308],
                            [-1.1478351],
                            [-0.37523866],
                            [-1.9411281],
                            [-1.6829233],
                            [1.5801933],
                            [-1.763592],
                            [0.471502],
                            [-1.8385562],
                            [-1.6221057],
                            [-0.2342265],
                            [1.5579345],
                        ],
                        dtype=torch.float32,
                    )
                )
                self.model.gru.weight_hh_l0.copy_(
                    torch.tensor(
                        [
                            [0.21837276, -0.18894187, -1.377557, 0.02368363, 0.428687, -0.33761144, 0.20816484, -0.53279597],
                            [0.08806777, -0.07281633, -1.0372438, 0.01798264, 0.5992356, -0.14019994, 0.71158516, -0.30733213],
                            [-0.1914122, -0.35667768, 0.72911865, 0.05730215, -0.70578635, -0.37561318, 0.34918255, 1.4140136],
                            [-0.02710954, 0.08384288, -1.2162979, 0.2806858, 0.0313941, 0.04940996, 0.41427082, -0.3163139],
                            [-0.6059817, 0.7722626, -0.9891586, -0.6886831, 0.69048965, -0.2852175, -0.5994332, 0.2317115],
                            [0.24844006, -0.10947371, -1.2708198, 0.09581696, 0.7782825, -0.17305057, -0.41211772, -1.0803086],
                            [0.3273324, 0.07578273, -1.0008, 0.54624504, -0.06627015, 0.35076398, 0.5394633, 0.48873448],
                            [-0.6068607, 0.23890634, -0.37038577, -0.9960145, 0.49631986, -1.0374227, 0.0684825, 0.48175257],
                            [-1.0526305, 1.2339334, -0.03144038, -0.66427606, 0.24671665, -0.932715, -1.4414837, 2.1776178],
                            [-0.6819725, 0.21156597, -0.3578775, -0.5848043, 0.10369989, -0.0182352, -0.84986025, 1.3318975],
                            [-0.38235265, 0.7737251, -0.8743264, -1.3877537, 1.088845, -1.1460638, -1.5904802, -0.20761111],
                            [0.7470299, -0.1357011, 1.2817012, -0.36674356, 0.78291, 0.572283, -0.42792276, 0.21881399],
                            [-0.3608182, 0.8256266, -0.05957543, -1.1210523, 1.1693833, -0.24800867, -1.3036101, 0.04563542],
                            [-1.4204804, 1.3156079, 0.39886555, -1.2538626, -0.05922655, -1.2726437, -1.3260349, 1.6883177],
                            [0.36133656, -0.28713486, 0.3679666, 0.40583095, 0.5873029, 0.70440596, 0.09460878, -0.9911537],
                            [-0.62175137, 0.83324456, 0.3323098, -1.0844731, 1.2309693, -0.64856, -1.1452628, 0.06438428],
                            [0.06109472, -0.1794408, -0.91120374, 0.43881962, 0.6983807, 0.45872056, 0.53213686, -0.36537486],
                            [-0.01429755, 0.11511804, 1.2281008, -0.03711872, -0.18212391, -0.33510554, 0.08330284, 0.65482914],
                            [0.7881119, 0.5044115, 0.54973793, 0.00719606, 0.12785113, 0.09230851, -0.33757675, -0.94784606],
                            [-0.00435512, -0.43385676, -1.5128944, 0.39419138, -0.2159529, -0.13263425, 0.21967444, 0.05095883],
                            [-0.57904184, 0.11814177, -1.4495949, 0.21581346, -0.5324543, -0.40867063, 0.18473652, 0.01892551],
                            [0.55530596, -0.6954124, -0.90806216, 0.5894174, 0.21790019, 0.3712376, 0.29538184, -0.45083544],
                            [-0.24236497, -0.48168987, -1.0717871, 0.4513364, -0.5515471, 0.22251658, 0.00328223, 0.245705],
                            [0.46513963, -0.20012224, 1.5028826, -0.72579646, 0.733144, 0.61923957, 0.09090568, -0.08347078],
                        ],
                        dtype=torch.float32,
                    )
                )
                self.model.gru.bias_ih_l0.copy_(
                    torch.tensor(
                        [
                            0.22661082,
                            0.39329848,
                            0.53108484,
                            -0.11586303,
                            -0.3148793,
                            -0.13635144,
                            0.67471033,
                            -0.08181308,
                            -1.0490254,
                            -1.1522759,
                            -1.4128138,
                            -0.04944327,
                            -1.1883045,
                            -1.1550485,
                            -0.16908136,
                            -1.5636152,
                            0.246008,
                            -0.5016309,
                            0.43630338,
                            0.17391193,
                            0.58142823,
                            -0.04872124,
                            0.37669796,
                            -0.8089754,
                        ],
                        dtype=torch.float32,
                    )
                )
                self.model.gru.bias_hh_l0.copy_(
                    torch.tensor(
                        [
                            0.26528597,
                            0.22231632,
                            0.5749436,
                            0.19007114,
                            -0.05320328,
                            0.47610134,
                            0.4807601,
                            -0.03353593,
                            -0.67226356,
                            -1.544395,
                            -1.5849437,
                            -0.23120898,
                            -1.4601043,
                            -0.8977899,
                            -0.08579141,
                            -1.4269685,
                            0.30159336,
                            -0.41255045,
                            -0.25528878,
                            -0.01228876,
                            -0.09579726,
                            0.77717686,
                            0.5596912,
                            0.11125406,
                        ],
                        dtype=torch.float32,
                    )
                )
                self.model.fc.weight.copy_(
                    torch.tensor(
                        [[-1.3896451, 0.91180396, -1.4389762, -0.39588606, -0.7805852, -1.3019185, -0.51872027, 1.1329138]],
                        dtype=torch.float32,
                    )
                )
                self.model.fc.bias.copy_(torch.tensor([-0.6675817], dtype=torch.float32))

        def process(self, rms_value: float) -> float:
            sample = max(0.0, min(1.0, float(rms_value) * 4.0))
            x = torch.tensor([[[sample]]], dtype=torch.float32, device=self.device)
            with torch.no_grad():
                output, hidden = self.model(x, self.hidden)
            self.hidden = hidden.detach()
            beat_prob = float(output[:, -1, :].cpu().item())
            self.visual_value = 0.7 * self.visual_value + 0.3 * beat_prob
            return self.visual_value


else:

    class BeatDetectorRNN:
        def __init__(self, channels: int) -> None:
            raise RuntimeError("PyTorch is required for BeatDetectorRNN.")


class NeonWaveWindow(tk.Toplevel):
    """Cyberpunk-inspired neon waveform driven by RMS energy."""

    def __init__(self, master: tk.Misc, title: str, get_limits, on_close) -> None:
        super().__init__(master)
        self.get_limits = get_limits
        self._on_close_callback = on_close
        self._current_level = 0.0
        self._last_beat = 0.0

        self.title(title)
        self.configure(bg="#02010a")
        self.geometry("900x420")
        self.resizable(True, True)

        self.canvas = tk.Canvas(
            self,
            bg="#02010a",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

        self._width = 900
        self._height = 420
        self._baseline = self._height * 0.7
        self._amplitude = self._height * 0.55
        self._history_len = 240
        self._history: deque[float] = deque([0.0] * self._history_len, maxlen=self._history_len)

        self.glow_line: int | None = None
        self.main_line: int | None = None
        self.highlight_line: int | None = None
        self.baseline_line: int | None = None
        self.grid_items: list[int] = []

        self._draw_background()
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

    def update_wave(self, rms_value: float, beat_level: float, dt: float) -> None:
        min_val, max_val = self.get_limits()
        if max_val <= min_val:
            max_val = min_val + 1e-6
        normalized = (float(rms_value) - min_val) / (max_val - min_val)
        normalized = max(0.0, min(1.0, normalized))

        self._current_level = normalized
        self._last_beat = max(0.0, min(1.0, float(beat_level)))
        self._history.append(normalized)
        self._draw_wave()

    def _draw_background(self) -> None:
        for item in self.grid_items:
            self.canvas.delete(item)
        self.grid_items.clear()

        rows = 6
        cols = 12
        for i in range(1, rows):
            y = self._baseline - (self._amplitude * (i / rows))
            if y < 0 or y > self._height:
                continue
            self.grid_items.append(
                self.canvas.create_line(
                    0,
                    y,
                    self._width,
                    y,
                    fill="#07102e",
                    dash=(4, 10),
                )
            )
        for i in range(1, cols):
            x = self._width * (i / cols)
            self.grid_items.append(
                self.canvas.create_line(
                    x,
                    0,
                    x,
                    self._height,
                    fill="#050b1c",
                    dash=(2, 12),
                )
            )

        if self.baseline_line is not None:
            self.canvas.delete(self.baseline_line)
        self.baseline_line = self.canvas.create_line(
            0,
            self._baseline,
            self._width,
            self._baseline,
            fill="#101a3c",
            width=2,
        )

    def _draw_wave(self) -> None:
        if len(self._history) < 4:
            return

        samples = list(self._history)
        count = len(samples)
        step_x = self._width / max(1, count - 1)
        coords: list[float] = []
        for idx, value in enumerate(samples):
            x = idx * step_x
            y_offset = value * self._amplitude
            y = self._baseline - y_offset
            coords.extend([x, y])

        line_width = 3.0 + 10.0 * self._current_level + 6.0 * self._last_beat
        glow_width = max(line_width * 2.4, line_width + 6.0)
        highlight_width = max(1.6, line_width * 0.45)

        base_color = self._mix_color("#6400ff", "#45f7ff", self._current_level)
        glow_color = self._mix_color(base_color, "#9afaff", min(1.0, 0.35 + 0.45 * self._last_beat))
        highlight_color = self._mix_color(base_color, "#ffffff", min(1.0, 0.5 + 0.4 * self._last_beat))

        if self.glow_line is None:
            self.glow_line = self.canvas.create_line(
                *coords,
                smooth=True,
                splinesteps=24,
                fill=glow_color,
                width=glow_width,
                capstyle="round",
            )
        else:
            self.canvas.coords(self.glow_line, *coords)
            self.canvas.itemconfigure(self.glow_line, width=glow_width, fill=glow_color)

        if self.main_line is None:
            self.main_line = self.canvas.create_line(
                *coords,
                smooth=True,
                splinesteps=36,
                fill=base_color,
                width=line_width,
                capstyle="round",
            )
        else:
            self.canvas.coords(self.main_line, *coords)
            self.canvas.itemconfigure(self.main_line, width=line_width, fill=base_color)

        if self.highlight_line is None:
            self.highlight_line = self.canvas.create_line(
                *coords,
                smooth=True,
                splinesteps=24,
                fill=highlight_color,
                width=highlight_width,
                capstyle="round",
            )
        else:
            self.canvas.coords(self.highlight_line, *coords)
            self.canvas.itemconfigure(self.highlight_line, width=highlight_width, fill=highlight_color)

        if self.baseline_line is not None:
            self.canvas.tag_lower(self.baseline_line, self.glow_line)
        for item in self.grid_items:
            self.canvas.tag_lower(item, self.glow_line)

    def _on_resize(self, event) -> None:
        self._width = max(360, int(event.width))
        self._height = max(220, int(event.height))
        self._baseline = self._height * 0.72
        self._amplitude = self._height * 0.6
        desired_len = max(120, min(480, int(self._width / 3)))
        if desired_len != self._history_len:
            samples = list(self._history)
            if desired_len > len(samples):
                pad_value = samples[0] if samples else 0.0
                pad = [pad_value] * (desired_len - len(samples))
                samples = pad + samples
            else:
                samples = samples[-desired_len:]
            self._history_len = desired_len
            self._history = deque(samples, maxlen=self._history_len)
        self._draw_background()
        self._draw_wave()

    def _handle_close(self) -> None:
        if callable(self._on_close_callback):
            self._on_close_callback()
        self.destroy()

    @staticmethod
    def _mix_color(start_hex: str, end_hex: str, t: float) -> str:
        t = max(0.0, min(1.0, t))

        def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
            hex_color = hex_color.lstrip("#")
            return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))

        def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
            return "#%02x%02x%02x" % rgb

        start_rgb = hex_to_rgb(start_hex)
        end_rgb = hex_to_rgb(end_hex)
        mixed = tuple(
            int(round(start_rgb[i] + (end_rgb[i] - start_rgb[i]) * t))
            for i in range(3)
        )
        return rgb_to_hex(mixed)


class CircularRMSWindow(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        get_limits,
        get_high_band_limits,
        include_high_var,
        base_color: str,
        high_color: str,
        gradient_mode: bool,
        on_close,
    ) -> None:
        super().__init__(master)
        self.get_limits = get_limits
        self.get_high_band_limits = get_high_band_limits
        self.include_high_var = include_high_var
        self.base_color = base_color
        self.target_color = "#f8f8f8"
        self.high_base_color = high_color
        self.high_target_color = "#fefefe"
        self.gradient_mode = bool(gradient_mode)
        self._on_close_callback = on_close
        self._current_rms = 0.0
        self._current_high_rms = 0.0
        self._beat_visual = 0.0
        self._low_normalized = 0.0
        self._high_normalized = 0.0
        self._include_high_trace = self.include_high_var.trace_add(
            "write", self._on_include_high_changed
        )

        self.title(title)
        self.geometry("900x900")
        self.resizable(True, True)

        self.canvas = tk.Canvas(self, bg="#101010", highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

        self.width = 900
        self.height = 900
        self.low_circle = self.canvas.create_oval(0, 0, 0, 0, fill=self.base_color, outline="")
        self.high_circle = self.canvas.create_oval(0, 0, 0, 0, fill=self.high_base_color, outline="")
        self.canvas.itemconfigure(self.high_circle, state="hidden")
        self.canvas.tag_raise(self.high_circle)

        self.protocol("WM_DELETE_WINDOW", self._handle_close)

    def update_levels(self, low_rms: float, high_rms: float | None = None) -> None:
        self._current_rms = float(low_rms)
        if high_rms is not None:
            self._current_high_rms = float(high_rms)
        self._redraw_circles()

    def update_beat(self, probability: float) -> None:
        probability = max(0.0, min(1.0, probability))
        self._beat_visual = 0.6 * self._beat_visual + 0.4 * probability
        self._update_colors()

    def _on_resize(self, event) -> None:
        self.width = max(100, int(event.width))
        self.height = max(100, int(event.height))
        self._redraw_circles()

    def _redraw_circles(self) -> None:
        cx = self.width / 2
        cy = self.height / 2
        span = min(self.width, self.height) / 2.0

        min_val, max_val = self.get_limits()
        if max_val <= min_val:
            max_val = min_val + 1e-6
        normalized = (self._current_rms - min_val) / (max_val - min_val)
        normalized = max(0.0, min(1.0, normalized))
        radius = span * normalized
        if radius <= 0.0:
            low_coords = (cx, cy, cx, cy)
        else:
            low_coords = (cx - radius, cy - radius, cx + radius, cy + radius)
        self.canvas.coords(self.low_circle, *low_coords)
        self._low_normalized = normalized

        if self.include_high_var.get():
            high_min, high_max = self.get_high_band_limits()
            if high_max <= high_min:
                high_max = high_min + 1e-6
            high_norm = (self._current_high_rms - high_min) / (high_max - high_min)
            high_norm = max(0.0, min(1.0, high_norm))
            high_radius = span * high_norm
            if high_radius <= 0.0:
                high_coords = (cx, cy, cx, cy)
            else:
                high_coords = (cx - high_radius, cy - high_radius, cx + high_radius, cy + high_radius)
            self.canvas.coords(self.high_circle, *high_coords)
            self.canvas.itemconfigure(self.high_circle, state="normal")
            self._high_normalized = high_norm
        else:
            self.canvas.itemconfigure(self.high_circle, state="hidden")
            self._high_normalized = 0.0

        self._update_colors()

    def _update_colors(self) -> None:
        beat_mix = max(0.0, min(1.0, self._beat_visual))
        gradient_mix = max(0.0, min(1.0, self._low_normalized if self.gradient_mode else 0.0))
        mix = max(beat_mix, gradient_mix)
        low_color = BandWindow._mix_color(self.base_color, self.target_color, mix)
        self.canvas.itemconfigure(self.low_circle, fill=low_color)
        if self.include_high_var.get():
            high_color = BandWindow._mix_color(self.high_base_color, self.high_target_color, mix)
            self.canvas.itemconfigure(self.high_circle, fill=high_color, state="normal")
        else:
            self.canvas.itemconfigure(self.high_circle, state="hidden")

    def set_base_color(self, color: str) -> None:
        self.base_color = color
        self._update_colors()

    def set_gradient_mode(self, enabled: bool) -> None:
        self.gradient_mode = bool(enabled)
        self._update_colors()

    def _on_include_high_changed(self, *_args) -> None:
        self._redraw_circles()

    def _handle_close(self) -> None:
        if self._include_high_trace is not None:
            try:
                self.include_high_var.trace_remove("write", self._include_high_trace)
            except tk.TclError:
                pass
            self._include_high_trace = None
        if callable(self._on_close_callback):
            self._on_close_callback()
        self.destroy()


class BandWindow(tk.Toplevel):
    """Secondary window that visualizes a frequency band RMS around a center line."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        rms_to_db,
        get_limits,
        set_limits,
        base_color: str,
        gradient_mode: bool,
        on_close,
    ) -> None:
        super().__init__(master)
        self.rms_to_db = rms_to_db
        self.get_limits = get_limits
        self.set_limits = set_limits
        self.base_color = base_color
        self.target_color = "#f8f8f8"
        self.gradient_mode = bool(gradient_mode)
        self._on_close_callback = on_close

        self.title(title)
        self.resizable(True, True)
        self.overrideredirect(False)
        self.attributes("-toolwindow", True)

        self.min_canvas_width = 100
        self.min_canvas_height = 300
        self.max_canvas_width = 260

        self.canvas = tk.Canvas(
            self,
            bg="#101010",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(padx=0, pady=0, fill="both", expand=True)

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas_width = self.min_canvas_width
        self.canvas_height = self.min_canvas_height
        self.geometry("100x900")
        self.center_y = self.canvas_height / 2
        self.mid_line = self.canvas.create_line(
            0,
            self.center_y,
            self.canvas_width,
            self.center_y,
            fill="#444444",
            dash=(2, 4),
        )
        self.border_rect = self.canvas.create_rectangle(
            0,
            0,
            self.canvas_width,
            self.canvas_height,
            outline="#333333",
        )
        self.bar = self.canvas.create_rectangle(
            self.canvas_width / 2 - 20,
            self.center_y,
            self.canvas_width / 2 + 20,
            self.center_y,
            fill=self.base_color,
            outline="",
        )

        self._beat_visual = 0.0
        self._current_rms = 0.0
        self._current_normalized = 0.0
        self._update_bar_color()

    def update_level(self, rms_value: float) -> None:
        self._current_rms = float(rms_value)
        self._redraw_bar()
        self._update_bar_color()

    def update_beat(self, probability: float) -> None:
        probability = max(0.0, min(1.0, probability))
        self._beat_visual = 0.6 * self._beat_visual + 0.4 * probability
        self._update_bar_color()

    def _redraw_bar(self) -> None:
        min_val, max_val = self.get_limits()
        if max_val <= min_val:
            max_val = min_val + 1e-6
        normalized = (self._current_rms - min_val) / (max_val - min_val)
        normalized = max(0.0, min(1.0, normalized))
        if normalized <= 0.0:
            y_top = self.center_y
            y_bottom = self.center_y
        else:
            half_span = normalized * (self.canvas_height / 2.0)
            half_span = max(half_span, 0.5)
            y_top = max(0.0, self.center_y - half_span)
            y_bottom = min(self.canvas_height, self.center_y + half_span)
        self.canvas.coords(self.bar, 0, y_top, self.canvas_width, y_bottom)
        self._current_normalized = normalized

    def _update_bar_color(self) -> None:
        beat_mix = max(0.0, min(1.0, self._beat_visual))
        gradient_mix = max(0.0, min(1.0, self._current_normalized))
        if self.gradient_mode:
            mix = max(beat_mix, gradient_mix)
        else:
            mix = beat_mix
        bar_color = self._mix_color(self.base_color, self.target_color, mix)
        self.canvas.itemconfigure(self.bar, fill=bar_color)

    def _apply_limits(self) -> None:
        pass

    def _apply_limits_event(self, _event) -> None:
        self._apply_limits()

    def _on_canvas_resize(self, event) -> None:
        new_width = max(self.min_canvas_width, min(self.max_canvas_width, int(event.width)))
        new_height = max(self.min_canvas_height, int(event.height))
        self.canvas_width = new_width
        self.canvas_height = new_height
        self.center_y = self.canvas_height / 2
        self.canvas.coords(self.mid_line, 0, self.center_y, self.canvas_width, self.center_y)
        self.canvas.coords(self.border_rect, 0, 0, self.canvas_width, self.canvas_height)
        self._redraw_bar()
        self._update_bar_color()

    def set_base_color(self, new_color: str) -> None:
        self.base_color = new_color
        self._update_bar_color()

    def set_gradient_mode(self, enabled: bool) -> None:
        self.gradient_mode = bool(enabled)
        self._update_bar_color()

    def _handle_close(self) -> None:
        if callable(self._on_close_callback):
            self._on_close_callback()
        self.destroy()

    @staticmethod
    def _mix_color(start_hex: str, end_hex: str, t: float) -> str:
        def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
            hex_color = hex_color.lstrip("#")
            return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))

        def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
            return "#%02x%02x%02x" % rgb

        start_rgb = hex_to_rgb(start_hex)
        end_rgb = hex_to_rgb(end_hex)
        mixed = tuple(
            int(round(start_rgb[i] + (end_rgb[i] - start_rgb[i]) * t))
            for i in range(3)
        )
        return rgb_to_hex(mixed)


def main() -> None:
    log_path = Path(__file__).with_name("loopback_monitor.log")
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger(__name__).info("Application starting. Log: %s", log_path)

    root = tk.Tk()
    try:
        LoopbackMonitorApp(root)
        root.mainloop()
    except Exception:
        logging.getLogger(__name__).exception("Unhandled error while running application.")
        print("A fatal error occurred while starting the application.", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
