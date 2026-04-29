#!/usr/bin/env python3
"""
ABB Crush Tester Data Reader
============================
Session-based tool that monitors an ABB LW Crush Tester via FTP, collects
replicates into test sessions, computes summary statistics, supports
ECT/FCT/Generic test modes with per-sample editable parameters, and plots
overlaid force-displacement curves with peak markers.

Formulas:
  ECT (kN/m) = Peak Force (N) / Specimen Length (mm)
               [since 1 N/mm = 1 kN/m]
  FCT (kPa)  = Peak Force (N) / Specimen Area (m²) / 1000
             = Peak Force (N) * 10 / Specimen Area (cm²)

Usage:  python crush_reader.py
Requirements:  Python 3.8+, matplotlib (pip install matplotlib)
"""

from __future__ import annotations

import csv
import ftplib
import hashlib
import math
import os
import re
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional
from xml.etree import ElementTree as ET

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


__version__ = "3.2.0"

# ---------------------------------------------------------------------------
#  CONSTANTS
# ---------------------------------------------------------------------------

# FTP defaults — override via the connection panel.
DEFAULT_HOST = "192.168.0.3"
DEFAULT_PORT = 21
DEFAULT_USER = "lwuser"
DEFAULT_PASS = "lwapp"
DEFAULT_REMOTE_DIR = "/results"
DEFAULT_POLL_SECONDS = 5.0
FTP_TIMEOUT_SECONDS = 10
RECONNECT_BACKOFF_MAX = 30  # seconds

# Test defaults.
DEFAULT_PARAM_VALUE = 100.0       # ECT length (mm) or FCT area (cm²)
DEFAULT_THRESHOLD_N = 10.0        # zeroing threshold in newtons

# CSV / file IO. utf-8-sig ensures Excel renders cm², kPa, etc. correctly
# on Windows — vanilla utf-8 gets misinterpreted as cp1252 by default.
CSV_ENCODING = "utf-8-sig"

# UI — column definition for the replicate tree. Order is the source of
# truth: handlers index into it by name, never by hardcoded "#N".
REP_COLS: tuple[str, ...] = (
    "Plot", "#", "Sample ID", "Sample No",
    "Peak Force (N)", "Param", "Computed", "Unit",
)
REP_COL_WIDTHS: tuple[int, ...] = (40, 30, 100, 60, 90, 80, 90, 50)
COL_PLOT = REP_COLS.index("Plot")
COL_PARAM = REP_COLS.index("Param")


# ---------------------------------------------------------------------------
#  XML PARSER
# ---------------------------------------------------------------------------

# Thousand-group continuation: exactly three digits, optionally followed by a
# decimal part. Matches the second-and-later groups of "1,033.50" but not
# standalone values like "10.19" or "5".
_THOUSAND_CONT_RE = re.compile(r"\d{3}(\.\d+)?")


def parse_comma_values(raw: str) -> list[float]:
    """Parse the machine's mixed comma-as-delimiter / comma-as-thousand-separator
    encoding. The XML emits raw data like ``'996.97,1,033.50,1,068.08'`` —
    standard CSV split would yield five tokens, four of which are nonsense.

    Heuristic: if the accumulator already has a decimal point it's a complete
    value; otherwise, a 3-digit (optionally fractional) token is treated as the
    continuation of a thousand group. Works because every value in the file
    has a decimal part, so two integers in a row never legitimately appear.
    """
    if not raw or not raw.strip():
        return []
    values: list[float] = []
    acc = ""
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        is_continuation = (
            acc != ""
            and "." not in acc
            and _THOUSAND_CONT_RE.fullmatch(token) is not None
        )
        if is_continuation:
            acc += token
        else:
            if acc:
                values.append(float(acc))
            acc = token
    if acc:
        values.append(float(acc))
    return values


def is_sample_xml(xml_bytes: bytes) -> bool:
    """True iff ``xml_bytes`` parses as a single-test ``<SAMPLE>`` document
    (i.e. not a ``<SAMPLESET>`` summary).
    """
    if not xml_bytes:
        return False
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    return root.tag == "SAMPLE"


def parse_sample_xml_bytes(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    data: dict = {
        "code_id": root.findtext("CODEID", ""),
        "sample_id": root.findtext("SAMPLEID", ""),
        "operator_id": root.findtext("OPERATORID", ""),
        "program_name": root.findtext("PROGRAMNAME", ""),
        "end_serie": root.findtext("ENDSERIE", "0"),
        "sample_no": root.findtext("SAMPLENO", "0"),
        "results": [],
        "x_values": [],
        "y_values": [],
    }
    for item in root.findall(".//RESULTS/ITEM"):
        data["results"].append({
            "property_id": item.findtext("PROPERTYID", ""),
            "property_name": item.findtext("PROPERTYNAME", ""),
            "unit": item.findtext("UNIT", ""),
            "value": item.findtext("VALUE", ""),
        })
    raw = root.find("RAWDATA")
    if raw is not None:
        data["x_values"] = parse_comma_values(raw.findtext("XVALUES", ""))
        data["y_values"] = parse_comma_values(raw.findtext("YVALUES", ""))
    return data


def parse_sample_xml(xml_path: str | Path) -> dict:
    return parse_sample_xml_bytes(Path(xml_path).read_bytes())


def parse_summary_xml_bytes(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    data: dict = {
        "code_id": root.findtext("CODEID", ""),
        "program_name": root.findtext("PROGRAMNAME", ""),
        "end_serie": root.findtext("ENDSERIE", "0"),
        "items": [],
    }
    for item in root.findall(".//SUMMARY/ITEM"):
        data["items"].append({
            "property_name": item.findtext("PROPERTYNAME", ""),
            "unit": item.findtext("UNIT", ""),
            "mean": item.findtext("MEAN", ""),
            "cov": item.findtext("COV", ""),
            "std": item.findtext("STD", ""),
            "n_values": item.findtext("NOVALUES", ""),
        })
    return data


# ---------------------------------------------------------------------------
#  THRESHOLD ZEROING
# ---------------------------------------------------------------------------

def apply_threshold_zeroing(
    x_vals: list[float],
    y_vals: list[float],
    threshold: float = DEFAULT_THRESHOLD_N,
) -> tuple[list[float], list[float]]:
    """Trim the lead-in region and re-zero displacement.

    Returns ``(x, y)`` starting at the first index where ``y >= threshold``
    — i.e. the platen has engaged in the compressive direction. Idle noise
    on this load cell sits around −10 N, so an unsigned check would never
    trim anything; only positive force indicates real contact. If no sample
    crosses the threshold the function falls back to keeping the full curve
    zeroed at ``x[0]``.
    """
    if not x_vals or not y_vals:
        return [], []
    start_idx = 0
    for i, y in enumerate(y_vals):
        if y >= threshold:
            start_idx = i
            break
    x_t = x_vals[start_idx:]
    y_t = y_vals[start_idx:]
    if not x_t:
        return [], []
    x0 = x_t[0]
    return [abs(x0 - x) for x in x_t], y_t


# ---------------------------------------------------------------------------
#  TEST SESSION
# ---------------------------------------------------------------------------

def compute_value(sample: dict, test_type: str, param: float) -> dict:
    """Compute the test-specific value for a single replicate.

    ``param`` is the specimen length in mm for ECT, the specimen area in cm²
    for FCT, and ignored for Generic. Returns a dict with the display name,
    value, unit, raw peak force, and the param used (so per-sample edits can
    be displayed and exported).
    """
    peak = max(sample["y_values"]) if sample["y_values"] else 0.0
    if test_type == "ECT" and param > 0:
        val = peak / param  # N/mm == kN/m, no conversion factor
        return {"name": "ECT", "value": round(val, 2), "unit": "kN/m",
                "peak_force": round(peak, 1), "param": param}
    if test_type == "FCT" and param > 0:
        val = peak * 10.0 / param  # kPa
        return {"name": "FCT", "value": round(val, 1), "unit": "kPa",
                "peak_force": round(peak, 1), "param": param}
    return {"name": "Peak Force", "value": round(peak, 1), "unit": "N",
            "peak_force": round(peak, 1), "param": param}


class TestSession:
    """A run of replicates collected against the same specimen / program.

    Mutation is not thread-safe; mutate from a single thread (the Tk main
    thread in this app). The FTP worker parses + archives in the background
    and then schedules ``add_sample`` via ``Tk.after``.
    """

    def __init__(self, project_name: str = "", test_type: str = "Generic",
                 default_param: float = 0.0):
        self.project_name = project_name
        self.test_type = test_type
        self.default_param = default_param
        self.samples: list[dict] = []
        self.xml_bytes_list: list[bytes] = []
        self.included: list[bool] = []
        self.sample_params: list[float] = []
        self.last_summary: Optional[dict] = None
        self.created_at = datetime.now()

    @property
    def count(self) -> int:
        return len(self.samples)

    def add_sample(self, parsed: dict, xml_bytes: bytes,
                   param: Optional[float] = None) -> None:
        p = self.default_param if param is None else param
        parsed["computed"] = compute_value(parsed, self.test_type, p)
        self.samples.append(parsed)
        self.xml_bytes_list.append(xml_bytes)
        self.included.append(True)
        self.sample_params.append(p)

    def update_param(self, idx: int, new_param: float) -> None:
        """Override the param for a single sample and recompute it."""
        self.sample_params[idx] = new_param
        self.samples[idx]["computed"] = compute_value(
            self.samples[idx], self.test_type, new_param)

    def set_test_type(self, test_type: str, default_param: float) -> None:
        """Switch test type. Resets every per-sample param to the new default
        and recomputes the derived values for the whole session.
        """
        self.test_type = test_type
        self.default_param = default_param
        for i, sample in enumerate(self.samples):
            self.sample_params[i] = default_param
            sample["computed"] = compute_value(sample, test_type, default_param)

    def toggle_included(self, idx: int) -> None:
        self.included[idx] = not self.included[idx]

    def get_included_indices(self) -> list[int]:
        return [i for i, inc in enumerate(self.included) if inc]

    def get_summary_stats(self) -> dict:
        """Mean/std/COV/min/max across **included** samples only."""
        idxs = self.get_included_indices()
        if not idxs:
            return {}
        first = self.samples[idxs[0]]["computed"]
        vals = [self.samples[i]["computed"]["value"] for i in idxs]
        peaks = [self.samples[i]["computed"]["peak_force"] for i in idxs]
        return {
            **self._stats(vals, first["name"], first["unit"]),
            "peak": self._stats(peaks, "Peak Force", "N"),
        }

    @staticmethod
    def _stats(vals: list[float], name: str, unit: str) -> dict:
        n = len(vals)
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        cov = (std / mean * 100) if mean != 0 else 0.0
        return {"name": name, "unit": unit, "n": n,
                "mean": round(mean, 2), "std": round(std, 2),
                "cov": round(cov, 2),
                "min": round(min(vals), 2), "max": round(max(vals), 2)}


# ---------------------------------------------------------------------------
#  FILE ARCHIVER
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Strip anything that isn't a word char, hyphen, or dot. Used for
    session/sample names that become folder names and filename stems.
    """
    return re.sub(r"[^\w\-.]", "_", name).strip("_")


def archive_sample(
    xml_bytes: bytes,
    parsed: dict,
    output_dir: str | Path,
    session_folder: str = "",
) -> tuple[Path, Path]:
    """Write the raw XML and a human-readable CSV of one replicate.

    Returns ``(xml_path, csv_path)``. The CSV uses utf-8-sig so Excel on
    Windows renders non-ASCII units (cm², kPa, …) correctly.
    """
    save_dir = Path(output_dir) / session_folder if session_folder else Path(output_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    program = sanitize_filename(parsed["program_name"])
    sid = sanitize_filename(parsed["sample_id"]) or "unknown"
    sno = parsed["sample_no"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{program}_{sid}_{sno:>02s}_{ts}"
    xml_path = save_dir / f"{base}.xml"
    csv_path = save_dir / f"{base}.csv"
    xml_path.write_bytes(xml_bytes)
    with csv_path.open("w", newline="", encoding=CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(["# Program", parsed["program_name"]])
        w.writerow(["# Sample ID", parsed["sample_id"]])
        w.writerow(["# Sample No", parsed["sample_no"]])
        w.writerow(["# Retrieved", ts])
        c = parsed.get("computed")
        if c:
            w.writerow(["# Computed", f"{c['name']}: {c['value']} {c['unit']}"])
        w.writerow([])
        w.writerow(["Property", "Value", "Unit"])
        for r in parsed["results"]:
            w.writerow([r["property_name"], r["value"], r["unit"]])
        w.writerow([])
        w.writerow(["Displacement (mm)", "Force (N)"])
        xz, yz = apply_threshold_zeroing(parsed["x_values"], parsed["y_values"])
        for x, y in zip(xz, yz):
            w.writerow([f"{x:.4f}", f"{y:.2f}"])
    return xml_path, csv_path


def export_session_summary(
    session: "TestSession",
    output_dir: str | Path,
    session_folder: str = "",
) -> Path:
    """Write the session-wide summary CSV. Returns the file path."""
    save_dir = Path(output_dir) / session_folder if session_folder else Path(output_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = sanitize_filename(session.project_name) or "session"
    path = save_dir / f"{name}_summary_{ts}.csv"
    stats = session.get_summary_stats()
    with path.open("w", newline="", encoding=CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(["Session Summary"])
        w.writerow(["Project", session.project_name])
        w.writerow(["Test Type", session.test_type])
        w.writerow(["Included Replicates", len(session.get_included_indices())])
        w.writerow(["Total Replicates", session.count])
        w.writerow(["Date", session.created_at.strftime("%Y-%m-%d %H:%M")])
        w.writerow([])
        if stats:
            w.writerow([f"{stats['name']} ({stats['unit']})"])
            for k in ("mean", "std", "cov", "min", "max"):
                w.writerow([k.upper() if k != "cov" else "COV (%)", stats[k]])
            w.writerow([])
            ps = stats["peak"]
            w.writerow(["Peak Force (N)"])
            for k in ("mean", "std", "cov", "min", "max"):
                w.writerow([k.upper() if k != "cov" else "COV (%)", ps[k]])
            w.writerow([])
        # Per-replicate
        param_col = {"ECT": "Length (mm)", "FCT": "Area (cm²)"}.get(
            session.test_type, "Param")
        header = ["#", "Included", "Sample ID", "Peak Force (N)",
                  param_col, f"{stats['name']} ({stats['unit']})" if stats else "Value"]
        for r in (session.samples[0]["results"] if session.samples else []):
            header.append(f"{r['property_name']} ({r['unit']})")
        w.writerow(header)
        for i, s in enumerate(session.samples):
            c = s["computed"]
            row = [i + 1, "Y" if session.included[i] else "N",
                   s["sample_id"], c["peak_force"],
                   session.sample_params[i], c["value"]]
            for r in s["results"]:
                row.append(r["value"])
            w.writerow(row)
    return path


# ---------------------------------------------------------------------------
#  FTP MONITOR
# ---------------------------------------------------------------------------

StatusCallback = Callable[[str, bool], None]
BytesCallback = Callable[[bytes], None]
LogCallback = Callable[[str], None]


class FTPMonitor:
    """Polls the crush tester for ``sample.xml`` / ``summary.xml`` changes.

    Runs a single background worker that owns the FTP socket; callbacks fire
    on that worker thread. Callers must marshal UI work back to the main
    thread (``Tk.after``). Stop is non-blocking by default but waits up to
    ``stop_timeout`` for the worker to exit cleanly.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASS,
        remote_dir: str = DEFAULT_REMOTE_DIR,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        load_existing: bool = False,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.remote_dir = remote_dir
        self.poll_interval = poll_interval
        self.load_existing = load_existing
        self._ftp: Optional[ftplib.FTP] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._file_hashes: dict[str, str] = {}
        self._hashes_lock = threading.Lock()
        self.on_status_change: Optional[StatusCallback] = None
        self.on_sample_changed: Optional[BytesCallback] = None
        self.on_summary_changed: Optional[BytesCallback] = None
        self.on_log: Optional[LogCallback] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _set_status(self, msg: str, connected: bool) -> None:
        if self.on_status_change:
            self.on_status_change(msg, connected)

    def _close_ftp(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except Exception:
            try:
                self._ftp.close()
            except Exception:
                pass
        self._ftp = None

    def _connect(self) -> bool:
        self._close_ftp()
        try:
            self._ftp = ftplib.FTP()
            self._ftp.connect(self.host, self.port, timeout=FTP_TIMEOUT_SECONDS)
            self._ftp.login(self.user, self.password)
            self._ftp.cwd(self.remote_dir)
        except (ftplib.all_errors, OSError) as e:
            self._ftp = None
            self._set_status(f"Connection failed: {e}", False)
            self._log(f"Connection failed: {e}")
            return False
        self._set_status(f"Connected to {self.host}", True)
        self._log(f"Connected to {self.host}:{self.port}")
        return True

    def _download(self, fn: str) -> Optional[bytes]:
        try:
            buf = bytearray()
            self._ftp.retrbinary(f"RETR {fn}", buf.extend)
            return bytes(buf)
        except (ftplib.all_errors, OSError) as e:
            self._log(f"Download error ({fn}): {e}")
            return None

    def _check(self, fn: str) -> Optional[bytes]:
        data = self._download(fn)
        if data is None:
            return None
        h = hashlib.md5(data).hexdigest()
        with self._hashes_lock:
            prev = self._file_hashes.get(fn)
            self._file_hashes[fn] = h
        if prev is None:
            # First time seeing this file after connect
            if self.load_existing:
                self._log(f"Initial load: {fn} ({len(data)} bytes)")
                return data
            else:
                self._log(f"Baseline recorded: {fn} ({len(data)} bytes, skipped)")
                return None
        if h != prev:
            self._log(f"Changed: {fn} ({len(data)} bytes)")
            return data
        return None

    def _loop(self) -> None:
        backoff = 1
        while not self._stop_event.is_set():
            if self._ftp is None:
                if not self._connect():
                    if self._stop_event.wait(min(backoff, RECONNECT_BACKOFF_MAX)):
                        break
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
                    continue
                backoff = 1
            try:
                data = self._check("sample.xml")
                if data and self.on_sample_changed:
                    self.on_sample_changed(data)
                data = self._check("summary.xml")
                if data and self.on_summary_changed:
                    self.on_summary_changed(data)
                self._ftp.voidcmd("NOOP")
            except (ftplib.all_errors, OSError) as e:
                self._log(f"Connection lost: {e}")
                self._set_status("Disconnected — reconnecting...", False)
                self._close_ftp()
                backoff = 1
                continue
            self._stop_event.wait(self.poll_interval)
        self._close_ftp()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        with self._hashes_lock:
            self._file_hashes.clear()
        self._thread = threading.Thread(
            target=self._loop, name="FTPMonitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        self._set_status("Disconnected", False)

    def force_refresh(self) -> None:
        with self._hashes_lock:
            self._file_hashes.clear()
        self._log("Forced refresh — will re-download on next poll")


# ---------------------------------------------------------------------------
#  PLOT COLORS
# ---------------------------------------------------------------------------

COLORS = [
    "#2962ff", "#d32f2f", "#2e7d32", "#ff6f00", "#6a1b9a",
    "#00838f", "#c62828", "#1565c0", "#558b2f", "#e65100",
    "#4527a0", "#00695c", "#ad1457", "#283593", "#9e9d24",
    "#bf360c", "#0277bd", "#1b5e20", "#ff8f00", "#4a148c",
]


# ---------------------------------------------------------------------------
#  GUI
# ---------------------------------------------------------------------------

def _resource_path(relative: str) -> str:
    """Resolve a path that works both in dev and in a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


class CrushReaderApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"ABB Crush Tester Data Reader  v{__version__}")
        self.root.geometry("1250x860")
        self.root.minsize(1050, 720)
        # Window icon (taskbar + title bar)
        ico = _resource_path("corrugated_crush_icon.ico")
        if os.path.isfile(ico):
            try:
                self.root.iconbitmap(ico)
            except tk.TclError:
                pass  # non-Windows or unsupported format — skip gracefully
        self.monitor: Optional[FTPMonitor] = None
        self.output_dir: str = ""
        self.session: Optional[TestSession] = None
        self.threshold_var = tk.StringVar(value=str(int(DEFAULT_THRESHOLD_N)))
        self._build_ui()

    # ========== UI ==========

    def _build_ui(self):
        self._build_session_bar(self.root)
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        left = ttk.Frame(main, width=340)
        main.add(left, weight=1)
        right = ttk.Frame(main)
        main.add(right, weight=3)
        self._build_settings_panel(left)
        self._build_test_params_panel(left)
        self._build_log_panel(left)
        self._build_replicates_panel(right)
        self._build_summary_panel(right)
        self._build_plot_panel(right)

    def _build_session_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, padx=6, pady=(6, 2))
        ttk.Button(bar, text="New Session", command=self._new_session_dialog).pack(side=tk.LEFT)
        ttk.Button(bar, text="Import Files...", command=self._import_batch).pack(side=tk.LEFT, padx=(6, 0))
        self.session_label = ttk.Label(bar, text="No active session", font=("Segoe UI", 10))
        self.session_label.pack(side=tk.LEFT, padx=12)
        self.export_btn = ttk.Button(bar, text="Export Summary CSV", command=self._export_summary, state=tk.DISABLED)
        self.export_btn.pack(side=tk.RIGHT)

    def _build_settings_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="FTP Connection", padding=8)
        frame.pack(fill=tk.X, padx=4, pady=(0, 4))
        # (label, attr name, default, masked)
        fields = [
            ("Host:",       "host_var", DEFAULT_HOST,        False),
            ("Port:",       "port_var", str(DEFAULT_PORT),   False),
            ("Username:",   "user_var", DEFAULT_USER,        False),
            ("Password:",   "pass_var", DEFAULT_PASS,        True),
            ("Remote dir:", "dir_var",  DEFAULT_REMOTE_DIR,  False),
            ("Poll (sec):", "poll_var", str(int(DEFAULT_POLL_SECONDS)), False),
        ]
        for i, (label, attr, default, masked) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=1)
            v = tk.StringVar(value=default)
            setattr(self, attr, v)
            e = ttk.Entry(frame, textvariable=v, width=20)
            if masked:
                e.config(show="*")
            e.grid(row=i, column=1, sticky=tk.EW, padx=(4, 0), pady=1)
        frame.columnconfigure(1, weight=1)
        r = len(fields)
        df = ttk.Frame(frame)
        df.grid(row=r, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
        ttk.Label(df, text="Save to:").pack(side=tk.LEFT)
        self.dir_label = ttk.Label(df, text="(not set)", foreground="gray")
        self.dir_label.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
        ttk.Button(df, text="Browse...", command=self._pick_dir, width=8).pack(side=tk.RIGHT)
        self.load_existing_var = tk.BooleanVar(value=False)
        cb_frame = ttk.Frame(frame)
        cb_frame.grid(row=r+1, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
        ttk.Checkbutton(cb_frame, text="Load last test on connect",
                        variable=self.load_existing_var).pack(anchor=tk.W)

        bf = ttk.Frame(frame)
        bf.grid(row=r+2, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
        self.connect_btn = ttk.Button(bf, text="Connect & Monitor", command=self._toggle_mon)
        self.connect_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.refresh_btn = ttk.Button(bf, text="Refresh", width=7, command=self._force_refresh, state=tk.DISABLED)
        self.refresh_btn.pack(side=tk.RIGHT, padx=(4,0))
        sf = ttk.Frame(frame)
        sf.grid(row=r+3, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
        self.status_dot = tk.Canvas(sf, width=12, height=12, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT); self._draw_dot("gray")
        self.status_label = ttk.Label(sf, text="Not connected")
        self.status_label.pack(side=tk.LEFT, padx=4)

    def _build_test_params_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Test Parameters", padding=8)
        frame.pack(fill=tk.X, padx=4, pady=(0, 4))
        # Test type
        r1 = ttk.Frame(frame); r1.pack(fill=tk.X)
        ttk.Label(r1, text="Test type:").pack(side=tk.LEFT)
        self.test_type_var = tk.StringVar(value="Generic")
        cb = ttk.Combobox(r1, textvariable=self.test_type_var,
                          values=["Generic", "ECT", "FCT"], state="readonly", width=10)
        cb.pack(side=tk.LEFT, padx=4)
        cb.bind("<<ComboboxSelected>>", self._on_test_type_changed)
        # Default param
        self.param_container = ttk.Frame(frame)
        self.param_container.pack(fill=tk.X, pady=(4,0))
        self.param_var = tk.StringVar(value=str(DEFAULT_PARAM_VALUE))
        self._update_param_fields()
        # Zeroing threshold
        r3 = ttk.Frame(frame); r3.pack(fill=tk.X, pady=(6,0))
        ttk.Label(r3, text="Zeroing threshold:").pack(side=tk.LEFT)
        ttk.Entry(r3, textvariable=self.threshold_var, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(r3, text="N").pack(side=tk.LEFT)
        ttk.Button(r3, text="Apply", command=self._apply_threshold, width=6).pack(side=tk.RIGHT)

    def _update_param_fields(self):
        for w in self.param_container.winfo_children():
            w.destroy()
        tt = self.test_type_var.get()
        if tt in ("ECT", "FCT"):
            label_text = "Default length:" if tt == "ECT" else "Default area:"
            unit_text = "mm" if tt == "ECT" else "cm²"
            ttk.Label(self.param_container, text=label_text).pack(side=tk.LEFT)
            self.param_var.set(str(DEFAULT_PARAM_VALUE))
            ttk.Entry(self.param_container, textvariable=self.param_var,
                      width=8).pack(side=tk.LEFT, padx=4)
            ttk.Label(self.param_container, text=unit_text).pack(side=tk.LEFT)
            ttk.Button(self.param_container, text="Apply to all",
                       command=self._apply_param_all, width=10).pack(side=tk.RIGHT)
        else:
            ttk.Label(self.param_container, text="Reports peak load",
                      foreground="gray").pack(side=tk.LEFT)

    def _on_test_type_changed(self, event=None):
        self._update_param_fields()
        if self.session:
            self._apply_param_all()

    def _read_param(self) -> float:
        try:
            return float(self.param_var.get())
        except ValueError:
            return DEFAULT_PARAM_VALUE

    def _apply_param_all(self):
        """Apply current test type + default param to every sample."""
        if not self.session:
            return
        tt = self.test_type_var.get()
        p = self._read_param()
        self.session.set_test_type(tt, p)
        self._refresh_table()
        self._update_summary()
        self._update_plot()
        self._log(f"Applied {tt} with param={p} to all {self.session.count} samples")

    def _apply_threshold(self):
        self._update_plot()
        self._log(f"Zeroing threshold set to {self.threshold_var.get()} N")

    def _get_threshold(self) -> float:
        try:
            return float(self.threshold_var.get())
        except ValueError:
            return DEFAULT_THRESHOLD_N

    def _build_log_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Log", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.log_text = scrolledtext.ScrolledText(frame, height=8, wrap=tk.WORD,
                                                   font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_replicates_panel(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="Replicates  (double-click param to edit, click Plot to toggle)",
            padding=4)
        frame.pack(fill=tk.X, padx=4, pady=(0, 2))
        self.meta_label = ttk.Label(frame, text="No active session", foreground="gray")
        self.meta_label.pack(anchor=tk.W)
        self.rep_tree = ttk.Treeview(
            frame, columns=REP_COLS, show="headings",
            height=7, selectmode="browse")
        for col, w in zip(REP_COLS, REP_COL_WIDTHS):
            self.rep_tree.heading(col, text=col)
            self.rep_tree.column(col, width=w, minwidth=w)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.rep_tree.yview)
        self.rep_tree.configure(yscrollcommand=sb.set)
        self.rep_tree.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=(4, 0))
        sb.pack(side=tk.RIGHT, fill=tk.Y, pady=(4, 0))
        self.rep_tree.bind("<Double-1>", self._on_tree_double_click)
        self.rep_tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.rep_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _clicked_col_index(self, event) -> Optional[int]:
        """Return the 0-based index of the column at the click, or None if
        the click was outside a cell. Decouples handlers from display order.
        """
        if self.rep_tree.identify_region(event.x, event.y) != "cell":
            return None
        col_id = self.rep_tree.identify_column(event.x)  # "#1", "#2", ...
        if not col_id or not col_id.startswith("#"):
            return None
        try:
            return int(col_id[1:]) - 1
        except ValueError:
            return None

    def _build_summary_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Session Summary", padding=4)
        frame.pack(fill=tk.X, padx=4, pady=(0, 2))
        self.summary_text = ttk.Label(frame, text="Start a session to see statistics",
                                      foreground="gray", wraplength=750, justify=tk.LEFT)
        self.summary_text.pack(anchor=tk.W, fill=tk.X)
        self.machine_summary_label = ttk.Label(frame, text="", foreground="#555",
                                               wraplength=750, justify=tk.LEFT)
        self.machine_summary_label.pack(anchor=tk.W, fill=tk.X, pady=(2,0))

    def _build_plot_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Force-Displacement Curves", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.fig = Figure(figsize=(7, 3.5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Displacement (mm)"); self.ax.set_ylabel("Force (N)")
        self.ax.set_title("Waiting for data..."); self.ax.grid(True, alpha=0.3)
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.draw()
        toolbar = NavigationToolbar2Tk(self.canvas, frame); toolbar.update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ========== HELPERS ==========

    def _draw_dot(self, c):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2,2,10,10, fill=c, outline=c)

    def _log(self, m):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, self._append_log, f"[{ts}] {m}")

    def _append_log(self, t):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, t + "\n"); self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _update_status(self, m, c):
        self.root.after(0, self._set_status_ui, m, c)

    def _set_status_ui(self, m, c):
        self.status_label.config(text=m)
        self._draw_dot("#2ecc71" if c else "#e74c3c")

    def _pick_dir(self):
        p = filedialog.askdirectory(title="Choose output folder")
        if p:
            self.output_dir = p
            d = p if len(p) < 35 else "..." + p[-32:]
            self.dir_label.config(text=d, foreground="black")

    # ========== TABLE INTERACTIONS ==========

    def _on_tree_click(self, event):
        """Toggle include/exclude when clicking the Plot column."""
        col_idx = self._clicked_col_index(event)
        if col_idx != COL_PLOT:
            return
        iid = self.rep_tree.identify_row(event.y)
        if not iid:
            return
        idx = int(iid)
        if not self.session or idx >= self.session.count:
            return
        self.session.toggle_included(idx)
        self._refresh_table()
        self._update_summary()
        self._update_plot()

    def _on_tree_double_click(self, event):
        """Open an inline editor over the Param cell."""
        col_idx = self._clicked_col_index(event)
        if col_idx != COL_PARAM:
            return
        iid = self.rep_tree.identify_row(event.y)
        if not iid:
            return
        idx = int(iid)
        if not self.session or idx >= self.session.count:
            return
        if self.session.test_type == "Generic":
            return  # nothing to edit

        bbox = self.rep_tree.bbox(iid, column=REP_COLS[COL_PARAM])
        if not bbox:
            return
        x, y, w, h = bbox

        current_val = self.session.sample_params[idx]
        entry_var = tk.StringVar(value=str(current_val))
        entry = ttk.Entry(self.rep_tree, textvariable=entry_var, width=8)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, tk.END)

        def commit(event=None):
            try:
                new_val = float(entry_var.get())
            except ValueError:
                entry.destroy()
                return
            self.session.update_param(idx, new_val)
            self._refresh_table()
            self._update_summary()
            self._update_plot()
            self._log(f"Sample #{idx + 1} param -> {new_val}")
            entry.destroy()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: entry.destroy())

    def _on_tree_select(self, event):
        """Highlight selected curve."""
        self._update_plot()

    # ========== SESSION MANAGEMENT ==========

    def _new_session_dialog(self):
        if self.session and self.session.count > 0:
            if not messagebox.askyesno("End Session",
                f"Current session has {self.session.count} replicates.\n"
                "Export and start new?"):
                return
            if self.output_dir: self._export_summary()

        dlg = tk.Toplevel(self.root)
        dlg.title("New Test Session"); dlg.geometry("380x200")
        dlg.transient(self.root); dlg.grab_set()

        ttk.Label(dlg, text="Project / Sample Name:").pack(anchor=tk.W, padx=12, pady=(12,2))
        nv = tk.StringVar()
        ttk.Entry(dlg, textvariable=nv, width=40).pack(padx=12, fill=tk.X)

        ttk.Label(dlg, text="Test Type:").pack(anchor=tk.W, padx=12, pady=(8,2))
        tv = tk.StringVar(value=self.test_type_var.get())
        ttk.Combobox(dlg, textvariable=tv, values=["Generic","ECT","FCT"],
                     state="readonly", width=14).pack(anchor=tk.W, padx=12)

        ttk.Label(dlg, text="(Leave name blank to auto-fill from machine)",
                  foreground="gray", font=("Segoe UI", 8)).pack(anchor=tk.W, padx=12, pady=(4,0))

        def ok():
            self.test_type_var.set(tv.get())
            self._update_param_fields()
            tt = tv.get()
            p = self._read_param()
            self.session = TestSession(nv.get().strip(), tt, p)
            self._update_session_label()
            self._clear_table()
            self._update_summary()
            self._update_plot()
            self.export_btn.config(state=tk.NORMAL)
            self._log(f"New session: '{nv.get().strip() or '(auto)'}' [{tt}]")
            dlg.destroy()

        bf = ttk.Frame(dlg); bf.pack(pady=12)
        ttk.Button(bf, text="Start Session", command=ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=4)
        dlg.bind("<Return>", lambda e: ok())

    def _update_session_label(self):
        if not self.session:
            self.session_label.config(text="No active session"); return
        s = self.session
        name = s.project_name or "(waiting for first sample)"
        n_inc = len(s.get_included_indices())
        self.session_label.config(
            text=f"Session: {name}  |  {s.test_type}  |  "
                 f"{n_inc}/{s.count} included")

    # ========== MONITORING ==========

    def _toggle_mon(self):
        if self.monitor and self.monitor.is_running():
            self._stop_mon()
        else:
            self._start_mon()

    def _start_mon(self):
        if not self.output_dir:
            messagebox.showwarning("No folder", "Choose an output folder first.")
            return
        try:
            port = int(self.port_var.get())
            poll = float(self.poll_var.get())
        except ValueError:
            messagebox.showerror("Error", "Port must be an integer and poll must be a number.")
            return
        if poll <= 0:
            messagebox.showerror("Error", "Poll interval must be positive.")
            return
        self.monitor = FTPMonitor(
            self.host_var.get(), port, self.user_var.get(),
            self.pass_var.get(), self.dir_var.get(), poll,
            load_existing=self.load_existing_var.get())
        self.monitor.on_status_change = self._update_status
        self.monitor.on_log = self._log
        self.monitor.on_sample_changed = self._on_ftp_sample
        self.monitor.on_summary_changed = self._on_ftp_summary
        self.monitor.start()
        self.connect_btn.config(text="Stop Monitoring")
        self.refresh_btn.config(state=tk.NORMAL)
        self._log("Monitoring started")

    def _stop_mon(self):
        if self.monitor:
            self.monitor.stop()
        self.connect_btn.config(text="Connect & Monitor")
        self.refresh_btn.config(state=tk.DISABLED)
        self._log("Stopped")
        self._update_status("Disconnected", False)

    def _force_refresh(self):
        if self.monitor:
            self.monitor.force_refresh()

    # ========== FTP CALLBACKS ==========
    # These run on the FTPMonitor worker thread. They do parse + disk archive
    # off the UI thread, then schedule the session mutation back onto the Tk
    # main thread via root.after — `self.session` is owned by the UI thread.

    def _on_ftp_sample(self, xml_bytes: bytes) -> None:
        try:
            if not is_sample_xml(xml_bytes):
                return
            parsed = parse_sample_xml_bytes(xml_bytes)
        except (ET.ParseError, ValueError) as e:
            self._log(f"Parse error on sample.xml: {e}")
            return
        # Disk IO is fine on the worker; UI mutation is not.
        try:
            session_folder = sanitize_filename(self._derive_session_name(parsed))
            xml_path, _ = archive_sample(
                xml_bytes, parsed, self.output_dir, session_folder)
        except OSError as e:
            self._log(f"Archive error: {e}")
            xml_path = None
        self.root.after(0, self._ingest_parsed_sample, xml_bytes, parsed, xml_path)

    def _on_ftp_summary(self, xml_bytes: bytes) -> None:
        try:
            parsed = parse_summary_xml_bytes(xml_bytes)
        except (ET.ParseError, ValueError) as e:
            self._log(f"Summary parse error: {e}")
            return
        self.root.after(0, self._ingest_machine_summary, parsed)

    @staticmethod
    def _derive_session_name(parsed: dict) -> str:
        name = parsed.get("program_name", "") or "session"
        if parsed.get("sample_id"):
            name = f"{name}_{parsed['sample_id']}"
        return name

    def _ingest_parsed_sample(self, xml_bytes: bytes, parsed: dict,
                              xml_path: Optional[Path]) -> None:
        """UI-thread half of _on_ftp_sample: mutate session and refresh views."""
        if not self.session:
            tt = self.test_type_var.get()
            self.session = TestSession(self._derive_session_name(parsed),
                                       tt, self._read_param())
            self.export_btn.config(state=tk.NORMAL)
        elif not self.session.project_name and parsed.get("program_name"):
            self.session.project_name = self._derive_session_name(parsed)
        self.session.add_sample(parsed, xml_bytes)
        if xml_path is not None:
            self._log(f"[#{self.session.count}] {xml_path.name}")
        else:
            self._log(f"[#{self.session.count}] (archive failed)")
        self._update_session_label()
        self._refresh_table()
        self._update_summary()
        self._update_plot()

    def _ingest_machine_summary(self, parsed: dict) -> None:
        if self.session:
            self.session.last_summary = parsed
        self._log(f"Machine summary: {parsed['program_name']}")
        self._show_machine_summary(parsed)

    # ========== BATCH IMPORT ==========

    def _import_batch(self):
        paths = filedialog.askopenfilenames(
            title="Select sample XML files",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")])
        if not paths:
            return

        if self.session and self.session.count > 0:
            ch = messagebox.askyesnocancel(
                "Active Session",
                f"{self.session.count} replicates exist.\n\n"
                "Yes = Export & start fresh\nNo = Add to current\nCancel = Abort")
            if ch is None:
                return
            if ch:
                if self.output_dir:
                    self._export_summary()
                self.session = None

        loaded, skipped, errors = 0, 0, []
        for path_str in sorted(paths):
            path = Path(path_str)
            try:
                raw = path.read_bytes()
            except OSError as e:
                errors.append(f"{path.name}: {e}")
                continue
            if not is_sample_xml(raw):
                skipped += 1
                self._log(f"  Skipped (not sample): {path.name}")
                continue
            try:
                self._load_sample_bytes(raw, path.name)
                loaded += 1
            except (ET.ParseError, ValueError) as e:
                errors.append(f"{path.name}: {e}")

        self._log(f"Import: {loaded} loaded, {skipped} skipped, {len(errors)} errors")
        if loaded > 0:
            msg = f"Imported {loaded} sample{'s' if loaded != 1 else ''}"
            if skipped:
                msg += f"\n({skipped} non-sample files skipped)"
            if self.session:
                st = self.session.get_summary_stats()
                if st:
                    msg += f"\n\n{st['name']}: Mean={st['mean']} {st['unit']}, COV={st['cov']}%"
            messagebox.showinfo("Import Complete", msg)
        if errors:
            self._log("Import errors:\n  " + "\n  ".join(errors))

    def _load_sample_bytes(self, xml_bytes: bytes, filename: str = "") -> None:
        """Parse a sample XML and add it to the (possibly new) session.
        Runs on the UI thread — used by batch import.
        """
        parsed = parse_sample_xml_bytes(xml_bytes)
        if not self.session:
            tt = self.test_type_var.get()
            self.session = TestSession(self._derive_session_name(parsed),
                                       tt, self._read_param())
            self.export_btn.config(state=tk.NORMAL)
        elif not self.session.project_name:
            self.session.project_name = self._derive_session_name(parsed)
        self.session.add_sample(parsed, xml_bytes)
        self._update_session_label()
        self._refresh_table()
        self._update_summary()
        self._update_plot()
        self._log(f"  Loaded: {filename} (#{self.session.count})")

    # ========== TABLE UPDATES ==========

    def _clear_table(self):
        for i in self.rep_tree.get_children(): self.rep_tree.delete(i)
        self.meta_label.config(text="No replicates yet", foreground="gray")

    def _refresh_table(self):
        for i in self.rep_tree.get_children(): self.rep_tree.delete(i)
        if not self.session or not self.session.samples:
            self.meta_label.config(text="No replicates yet", foreground="gray"); return
        s = self.session
        param_label = {"ECT": "Length (mm)", "FCT": "Area (cm²)"}.get(s.test_type, "—")
        self.rep_tree.heading("Param", text=param_label)
        comp_label = {"ECT": "ECT (kN/m)", "FCT": "FCT (kPa)"}.get(s.test_type, "Peak (N)")
        self.rep_tree.heading("Computed", text=comp_label)
        n_inc = len(s.get_included_indices())
        self.meta_label.config(
            text=f"{s.project_name}  |  {s.test_type}  |  {n_inc}/{s.count} included",
            foreground="black")
        for i, sample in enumerate(s.samples):
            c = sample.get("computed", {})
            check = "✓" if s.included[i] else ""
            param_val = s.sample_params[i] if s.test_type != "Generic" else ""
            self.rep_tree.insert("", tk.END, iid=str(i), values=(
                check, i+1, sample["sample_id"], sample["sample_no"],
                c.get("peak_force", ""),
                param_val, c.get("value", ""), c.get("unit", "")))

    # ========== SUMMARY ==========

    def _update_summary(self):
        if not self.session or self.session.count == 0:
            self.summary_text.config(text="No data yet", foreground="gray"); return
        st = self.session.get_summary_stats()
        if not st:
            self.summary_text.config(text="No included samples", foreground="gray"); return
        txt = (f"{st['name']}:  Mean = {st['mean']} {st['unit']},  "
               f"Std = {st['std']},  COV = {st['cov']}%,  "
               f"Range = [{st['min']} – {st['max']}]  (n={st['n']})")
        ps = st["peak"]
        txt += (f"\nPeak Force:  Mean = {ps['mean']} N,  "
                f"Std = {ps['std']},  COV = {ps['cov']}%,  "
                f"Range = [{ps['min']} – {ps['max']}] N  (n={ps['n']})")
        self.summary_text.config(text=txt, foreground="black")

    def _show_machine_summary(self, parsed):
        if not parsed["items"]: return
        parts = [f"{it['property_name']}: {it['mean']} {it['unit']} "
                 f"(n={it['n_values']}, COV={it['cov']}%)" for it in parsed["items"]]
        self.machine_summary_label.config(text="Machine:  " + "  |  ".join(parts))

    # ========== PLOT ==========

    def _update_plot(self):
        self.ax.clear()
        if not self.session or not self.session.samples:
            self.ax.set_title("Waiting for data...")
            self.ax.set_xlabel("Displacement (mm)"); self.ax.set_ylabel("Force (N)")
            self.ax.grid(True, alpha=0.3); self.fig.tight_layout()
            self.canvas.draw(); return

        s = self.session
        th = self._get_threshold()
        sel = self.rep_tree.selection()
        sel_idx = int(sel[0]) if sel else None
        plotted = 0

        for i, sample in enumerate(s.samples):
            if not s.included[i]: continue
            xz, yz = apply_threshold_zeroing(sample["x_values"], sample["y_values"], th)
            if not xz: continue
            color = COLORS[i % len(COLORS)]
            is_sel = (sel_idx == i)
            alpha = 1.0 if (sel_idx is None or is_sel) else 0.25
            lw = 2.0 if is_sel else 1.0
            self.ax.plot(xz, yz, lw=lw, color=color, alpha=alpha, label=f"#{i+1}")

            # Peak marker
            peak_y = max(yz)
            peak_idx = yz.index(peak_y)
            peak_x = xz[peak_idx]
            marker_size = 8 if is_sel else 5
            self.ax.plot(peak_x, peak_y, 'o', color=color, markersize=marker_size,
                         alpha=alpha, zorder=5)

            plotted += 1

        self.ax.set_xlabel("Displacement (mm)"); self.ax.set_ylabel("Force (N)")
        title = s.project_name or s.test_type
        self.ax.set_title(f"{title}  ({plotted} curve{'s' if plotted!=1 else ''})")
        self.ax.grid(True, alpha=0.3)
        if 0 < plotted <= 15:
            self.ax.legend(fontsize=7, ncol=min(plotted, 5), loc="upper left", framealpha=0.7)
        self.fig.tight_layout()
        self.canvas.draw()

    # ========== EXPORT ==========

    def _export_summary(self):
        if not self.session or self.session.count == 0:
            messagebox.showinfo("Nothing", "No replicates.")
            return
        if not self.output_dir:
            self._pick_dir()
            if not self.output_dir:
                return
        sf = sanitize_filename(self.session.project_name or "session")
        path = export_session_summary(self.session, self.output_dir, sf)
        self._log(f"Exported: {path.name}")
        messagebox.showinfo("Exported", f"Saved to:\n{path}")

    # ========== RUN ==========

    def run(self):
        mb = tk.Menu(self.root)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Import Files...", command=self._import_batch)
        fm.add_separator()
        fm.add_command(label="New Session...", command=self._new_session_dialog)
        fm.add_command(label="Export Summary", command=self._export_summary)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        mb.add_cascade(label="File", menu=fm)
        self.root.config(menu=mb)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self.session and self.session.count > 0 and self.output_dir:
            if messagebox.askyesno(
                "Export?",
                f"Export {self.session.count} replicates before closing?"):
                self._export_summary()
        if self.monitor:
            self.monitor.stop()
        self.root.destroy()


def main() -> None:
    try:
        CrushReaderApp().run()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # Best-effort dialog; fall back to stderr if Tk is unavailable.
        try:
            messagebox.showerror("Fatal Error", f"{e}\n\n{tb}")
        except Exception:
            import sys
            sys.stderr.write(tb)
        raise


if __name__ == "__main__":
    main()
