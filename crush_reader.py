#!/usr/bin/env python3
"""
ABB Crush Tester Data Reader  v3.0
====================================
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

import os
import re
import csv
import math
import time
import ftplib
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
from xml.etree import ElementTree as ET

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


# ---------------------------------------------------------------------------
#  XML PARSER
# ---------------------------------------------------------------------------

def parse_comma_values(raw: str) -> list[float]:
    """Parse comma-separated values handling thousand separators.
    E.g. '996.97,1,033.50,1,068.08' -> [996.97, 1033.50, 1068.08]
    """
    if not raw or not raw.strip():
        return []
    tokens = [t.strip() for t in raw.split(",")]
    values, acc = [], ""
    for token in tokens:
        if not token:
            continue
        is_tc = (acc != "" and "." not in acc
                 and re.fullmatch(r"\d{3}(\.\d+)?", token) is not None)
        if is_tc:
            acc += token
        else:
            if acc:
                values.append(float(acc))
            acc = token
    if acc:
        values.append(float(acc))
    return values


def is_sample_xml(xml_bytes: bytes) -> bool:
    """Check if XML bytes represent a <SAMPLE> file (not <SAMPLESET>/summary)."""
    try:
        root = ET.fromstring(xml_bytes)
        return root.tag == "SAMPLE"
    except ET.ParseError:
        return False


def parse_sample_xml_bytes(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    data = {
        "code_id": root.findtext("CODEID", ""),
        "sample_id": root.findtext("SAMPLEID", ""),
        "operator_id": root.findtext("OPERATORID", ""),
        "program_name": root.findtext("PROGRAMNAME", ""),
        "end_serie": root.findtext("ENDSERIE", "0"),
        "sample_no": root.findtext("SAMPLENO", "0"),
        "results": [], "x_values": [], "y_values": [],
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


def parse_sample_xml(xml_path: str) -> dict:
    with open(xml_path, "rb") as f:
        return parse_sample_xml_bytes(f.read())


def parse_summary_xml_bytes(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    data = {
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

def apply_threshold_zeroing(x_vals, y_vals, threshold=10.0):
    if not x_vals or not y_vals:
        return [], []
    start_idx = 0
    for i, y in enumerate(y_vals):
        if abs(y) >= threshold:
            start_idx = i
            break
    x_t, y_t = x_vals[start_idx:], y_vals[start_idx:]
    if not x_t:
        return [], []
    x0 = x_t[0]
    return [abs(x0 - x) for x in x_t], y_t


# ---------------------------------------------------------------------------
#  TEST SESSION
# ---------------------------------------------------------------------------

def compute_value(sample: dict, test_type: str, param: float) -> dict:
    """Compute test-specific value for a single sample.
    param = length_mm for ECT, area_cm2 for FCT, ignored for Generic.
    """
    peak = max(sample["y_values"]) if sample["y_values"] else 0
    if test_type == "ECT" and param > 0:
        val = peak / param  # N/mm = kN/m
        return {"name": "ECT", "value": round(val, 2), "unit": "kN/m",
                "peak_force": round(peak, 1), "param": param}
    elif test_type == "FCT" and param > 0:
        val = peak * 10 / param  # kPa
        return {"name": "FCT", "value": round(val, 1), "unit": "kPa",
                "peak_force": round(peak, 1), "param": param}
    else:
        return {"name": "Peak Force", "value": round(peak, 1), "unit": "N",
                "peak_force": round(peak, 1), "param": param}


class TestSession:
    def __init__(self, project_name="", test_type="Generic", default_param=0):
        self.project_name = project_name
        self.test_type = test_type
        self.default_param = default_param  # length or area
        self.samples = []        # parsed sample dicts
        self.xml_bytes_list = []
        self.included = []       # bool per sample — include in plot/stats
        self.sample_params = []  # per-sample param value
        self.last_summary = None
        self.created_at = datetime.now()

    @property
    def count(self):
        return len(self.samples)

    def add_sample(self, parsed, xml_bytes, param=None):
        p = param if param is not None else self.default_param
        parsed["computed"] = compute_value(parsed, self.test_type, p)
        self.samples.append(parsed)
        self.xml_bytes_list.append(xml_bytes)
        self.included.append(True)
        self.sample_params.append(p)

    def update_param(self, idx, new_param):
        """Update parameter for a single sample and recompute."""
        self.sample_params[idx] = new_param
        self.samples[idx]["computed"] = compute_value(
            self.samples[idx], self.test_type, new_param)

    def set_test_type(self, test_type, default_param):
        """Change test type and recompute everything."""
        self.test_type = test_type
        self.default_param = default_param
        for i in range(len(self.samples)):
            self.sample_params[i] = default_param
            self.samples[i]["computed"] = compute_value(
                self.samples[i], self.test_type, default_param)

    def toggle_included(self, idx):
        self.included[idx] = not self.included[idx]

    def get_included_indices(self):
        return [i for i, inc in enumerate(self.included) if inc]

    def get_summary_stats(self):
        """Stats across included samples only."""
        idxs = self.get_included_indices()
        if not idxs:
            return {}
        vals = [self.samples[i]["computed"]["value"] for i in idxs]
        peaks = [self.samples[i]["computed"]["peak_force"] for i in idxs]
        return {
            **self._stats(vals, self.samples[0]["computed"]["name"],
                          self.samples[0]["computed"]["unit"]),
            "peak": self._stats(peaks, "Peak Force", "N"),
        }

    @staticmethod
    def _stats(vals, name, unit):
        n = len(vals)
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean)**2 for v in vals) / (n - 1)) if n > 1 else 0
        cov = (std / mean * 100) if mean != 0 else 0
        return {"name": name, "unit": unit, "n": n, "mean": round(mean, 2),
                "std": round(std, 2), "cov": round(cov, 2),
                "min": round(min(vals), 2), "max": round(max(vals), 2)}


# ---------------------------------------------------------------------------
#  FILE ARCHIVER
# ---------------------------------------------------------------------------

def sanitize_filename(name):
    return re.sub(r'[^\w\-.]', '_', name).strip('_')


def archive_sample(xml_bytes, parsed, output_dir, session_folder=""):
    save_dir = os.path.join(output_dir, session_folder) if session_folder else output_dir
    os.makedirs(save_dir, exist_ok=True)
    program = sanitize_filename(parsed["program_name"])
    sid = sanitize_filename(parsed["sample_id"]) or "unknown"
    sno = parsed["sample_no"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{program}_{sid}_{sno:>02s}_{ts}"
    xml_path = os.path.join(save_dir, f"{base}.xml")
    csv_path = os.path.join(save_dir, f"{base}.csv")
    with open(xml_path, "wb") as f:
        f.write(xml_bytes)
    with open(csv_path, "w", newline="") as f:
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


def export_session_summary(session, output_dir, session_folder=""):
    save_dir = os.path.join(output_dir, session_folder) if session_folder else output_dir
    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = sanitize_filename(session.project_name) or "session"
    path = os.path.join(save_dir, f"{name}_summary_{ts}.csv")
    stats = session.get_summary_stats()
    with open(path, "w", newline="") as f:
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
            for k in ["mean", "std", "cov", "min", "max"]:
                w.writerow([k.upper() if k != "cov" else "COV (%)", stats[k]])
            w.writerow([])
            ps = stats["peak"]
            w.writerow(["Peak Force (N)"])
            for k in ["mean", "std", "cov", "min", "max"]:
                w.writerow([k.upper() if k != "cov" else "COV (%)", ps[k]])
            w.writerow([])
        # Per-replicate
        param_col = {"ECT": "Length (mm)", "FCT": "Area (cm2)"}.get(
            session.test_type, "Param")
        header = ["#", "Included", "Sample ID", "Peak Force (N)",
                  param_col, f"{stats['name']} ({stats['unit']})" if stats else "Value"]
        for r in (session.samples[0]["results"] if session.samples else []):
            header.append(f"{r['property_name']} ({r['unit']})")
        w.writerow(header)
        for i, s in enumerate(session.samples):
            c = s["computed"]
            row = [i+1, "Y" if session.included[i] else "N",
                   s["sample_id"], c["peak_force"],
                   session.sample_params[i], c["value"]]
            for r in s["results"]:
                row.append(r["value"])
            w.writerow(row)
    return path


# ---------------------------------------------------------------------------
#  FTP MONITOR
# ---------------------------------------------------------------------------

class FTPMonitor:
    def __init__(self, host="192.168.0.3", port=21, user="lwuser",
                 password="lwapp", remote_dir="/results", poll_interval=5):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.remote_dir = remote_dir
        self.poll_interval = poll_interval
        self._ftp = None
        self._running = False
        self._thread = None
        self._file_sizes = {}
        self.on_status_change = None
        self.on_sample_changed = None
        self.on_summary_changed = None
        self.on_log = None

    def _log(self, m):
        if self.on_log: self.on_log(m)

    def _set_status(self, m, c):
        if self.on_status_change: self.on_status_change(m, c)

    def _connect(self):
        try:
            if self._ftp:
                try: self._ftp.quit()
                except: pass
            self._ftp = ftplib.FTP()
            self._ftp.connect(self.host, self.port, timeout=10)
            self._ftp.login(self.user, self.password)
            self._ftp.cwd(self.remote_dir)
            self._set_status(f"Connected to {self.host}", True)
            self._log(f"Connected to {self.host}:{self.port}")
            return True
        except Exception as e:
            self._ftp = None
            self._set_status(f"Connection failed: {e}", False)
            self._log(f"Connection failed: {e}")
            return False

    def _download(self, fn):
        try:
            buf = bytearray()
            self._ftp.retrbinary(f"RETR {fn}", buf.extend)
            return bytes(buf)
        except Exception as e:
            self._log(f"Download error ({fn}): {e}")
            return None

    def _file_size(self, fn):
        try: return self._ftp.size(fn)
        except: return None

    def _check(self, fn):
        sz = self._file_size(fn)
        if sz is None: return None
        prev = self._file_sizes.get(fn)
        if prev is None:
            self._file_sizes[fn] = sz
            self._log(f"Baseline: {fn} ({sz} bytes)")
            return self._download(fn)
        if sz != prev:
            self._file_sizes[fn] = sz
            self._log(f"Changed: {fn} ({prev} -> {sz} bytes)")
            return self._download(fn)
        return None

    def _loop(self):
        backoff = 1
        while self._running:
            if self._ftp is None:
                if not self._connect():
                    time.sleep(min(backoff, 30)); backoff = min(backoff*2, 30)
                    continue
                backoff = 1
            try:
                d = self._check("sample.xml")
                if d and self.on_sample_changed: self.on_sample_changed(d)
                d = self._check("summary.xml")
                if d and self.on_summary_changed: self.on_summary_changed(d)
                self._ftp.voidcmd("NOOP")
            except (ftplib.all_errors, OSError) as e:
                self._log(f"Connection lost: {e}")
                self._set_status("Disconnected — reconnecting...", False)
                self._ftp = None; backoff = 1; continue
            for _ in range(int(self.poll_interval * 10)):
                if not self._running: break
                time.sleep(0.1)

    def start(self):
        if self._running: return
        self._running = True; self._file_sizes.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ftp:
            try: self._ftp.quit()
            except: pass
            self._ftp = None
        self._set_status("Disconnected", False)

    def force_refresh(self):
        self._file_sizes.clear()
        self._log("Forced refresh")


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

class CrushReaderApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ABB Crush Tester Data Reader")
        self.root.geometry("1250x860")
        self.root.minsize(1050, 720)
        self.monitor = None
        self.output_dir = ""
        self.session = None
        self.threshold_var = tk.StringVar(value="10")
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
        fields = [("Host:", "host_var", "192.168.0.3"), ("Port:", "port_var", "21"),
                  ("Username:", "user_var", "lwuser"), ("Password:", "pass_var", "lwapp"),
                  ("Remote dir:", "dir_var", "/results"), ("Poll (sec):", "poll_var", "5")]
        for i, (label, vn, default) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=1)
            v = tk.StringVar(value=default); setattr(self, vn, v)
            e = ttk.Entry(frame, textvariable=v, width=20)
            if "Password" in label: e.config(show="*")
            e.grid(row=i, column=1, sticky=tk.EW, padx=(4,0), pady=1)
        frame.columnconfigure(1, weight=1)
        r = len(fields)
        df = ttk.Frame(frame)
        df.grid(row=r, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
        ttk.Label(df, text="Save to:").pack(side=tk.LEFT)
        self.dir_label = ttk.Label(df, text="(not set)", foreground="gray")
        self.dir_label.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
        ttk.Button(df, text="Browse...", command=self._pick_dir, width=8).pack(side=tk.RIGHT)
        bf = ttk.Frame(frame)
        bf.grid(row=r+1, column=0, columnspan=2, sticky=tk.EW, pady=(8,0))
        self.connect_btn = ttk.Button(bf, text="Connect & Monitor", command=self._toggle_mon)
        self.connect_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.refresh_btn = ttk.Button(bf, text="Refresh", width=7, command=self._force_refresh, state=tk.DISABLED)
        self.refresh_btn.pack(side=tk.RIGHT, padx=(4,0))
        sf = ttk.Frame(frame)
        sf.grid(row=r+2, column=0, columnspan=2, sticky=tk.EW, pady=(6,0))
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
        self.param_var = tk.StringVar(value="100.0")
        self._update_param_fields()
        # Zeroing threshold
        r3 = ttk.Frame(frame); r3.pack(fill=tk.X, pady=(6,0))
        ttk.Label(r3, text="Zeroing threshold:").pack(side=tk.LEFT)
        ttk.Entry(r3, textvariable=self.threshold_var, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(r3, text="N").pack(side=tk.LEFT)
        ttk.Button(r3, text="Apply", command=self._apply_threshold, width=6).pack(side=tk.RIGHT)

    def _update_param_fields(self):
        for w in self.param_container.winfo_children(): w.destroy()
        tt = self.test_type_var.get()
        if tt == "ECT":
            ttk.Label(self.param_container, text="Default length:").pack(side=tk.LEFT)
            self.param_var.set("100.0")
            ttk.Entry(self.param_container, textvariable=self.param_var, width=8).pack(side=tk.LEFT, padx=4)
            ttk.Label(self.param_container, text="mm").pack(side=tk.LEFT)
            ttk.Button(self.param_container, text="Apply to all", command=self._apply_param_all, width=10).pack(side=tk.RIGHT)
        elif tt == "FCT":
            ttk.Label(self.param_container, text="Default area:").pack(side=tk.LEFT)
            self.param_var.set("100.0")
            ttk.Entry(self.param_container, textvariable=self.param_var, width=8).pack(side=tk.LEFT, padx=4)
            ttk.Label(self.param_container, text="cm²").pack(side=tk.LEFT)
            ttk.Button(self.param_container, text="Apply to all", command=self._apply_param_all, width=10).pack(side=tk.RIGHT)
        else:
            ttk.Label(self.param_container, text="Reports peak load", foreground="gray").pack(side=tk.LEFT)

    def _on_test_type_changed(self, event=None):
        self._update_param_fields()
        # If session exists, update its test type
        if self.session:
            self._apply_param_all()

    def _apply_param_all(self):
        """Change test type + default param for all samples."""
        if not self.session: return
        tt = self.test_type_var.get()
        try: p = float(self.param_var.get())
        except ValueError: p = 100.0
        self.session.set_test_type(tt, p)
        self._refresh_table()
        self._update_summary()
        self._update_plot()
        self._log(f"Applied {tt} with param={p} to all {self.session.count} samples")

    def _apply_threshold(self):
        self._update_plot()
        self._log(f"Zeroing threshold set to {self.threshold_var.get()} N")

    def _get_threshold(self):
        try: return float(self.threshold_var.get())
        except ValueError: return 10.0

    def _build_log_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Log", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.log_text = scrolledtext.ScrolledText(frame, height=8, wrap=tk.WORD,
                                                   font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_replicates_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Replicates  (double-click param to edit, click Plot to toggle)", padding=4)
        frame.pack(fill=tk.X, padx=4, pady=(0, 2))
        self.meta_label = ttk.Label(frame, text="No active session", foreground="gray")
        self.meta_label.pack(anchor=tk.W)
        # Columns: Plot, #, Sample ID, Peak Force, Param, Computed, Unit
        cols = ("Plot", "#", "Sample ID", "Sample No", "Peak Force (N)",
                "Param", "Computed", "Unit")
        self.rep_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                     height=7, selectmode="browse")
        widths = [40, 30, 100, 60, 90, 80, 90, 50]
        for col, w in zip(cols, widths):
            self.rep_tree.heading(col, text=col)
            self.rep_tree.column(col, width=w, minwidth=w)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.rep_tree.yview)
        self.rep_tree.configure(yscrollcommand=sb.set)
        self.rep_tree.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=(4,0))
        sb.pack(side=tk.RIGHT, fill=tk.Y, pady=(4,0))
        # Bindings
        self.rep_tree.bind("<Double-1>", self._on_tree_double_click)
        self.rep_tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.rep_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

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
        region = self.rep_tree.identify_region(event.x, event.y)
        if region != "cell": return
        col = self.rep_tree.identify_column(event.x)
        if col != "#1": return  # #1 = first column = "Plot"
        iid = self.rep_tree.identify_row(event.y)
        if not iid: return
        idx = int(iid)
        if not self.session or idx >= self.session.count: return
        self.session.toggle_included(idx)
        self._refresh_table()
        self._update_summary()
        self._update_plot()

    def _on_tree_double_click(self, event):
        """Edit the Param cell on double-click."""
        region = self.rep_tree.identify_region(event.x, event.y)
        if region != "cell": return
        col = self.rep_tree.identify_column(event.x)
        if col != "#6": return  # #6 = "Param" column
        iid = self.rep_tree.identify_row(event.y)
        if not iid: return
        idx = int(iid)
        if not self.session or idx >= self.session.count: return
        if self.session.test_type == "Generic": return  # no param to edit

        # Get cell bounding box
        bbox = self.rep_tree.bbox(iid, column="Param")
        if not bbox: return
        x, y, w, h = bbox

        # Create entry overlay
        current_val = self.session.sample_params[idx]
        entry_var = tk.StringVar(value=str(current_val))
        entry = ttk.Entry(self.rep_tree, textvariable=entry_var, width=8)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, tk.END)

        def commit(event=None):
            try:
                new_val = float(entry_var.get())
                self.session.update_param(idx, new_val)
                self._refresh_table()
                self._update_summary()
                self._update_plot()
                self._log(f"Sample #{idx+1} param -> {new_val}")
            except ValueError:
                pass
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
            self.test_type_var.set(tv.get()); self._update_param_fields()
            tt = tv.get()
            try: p = float(self.param_var.get())
            except: p = 100.0
            self.session = TestSession(nv.get().strip(), tt, p)
            self._update_session_label(); self._clear_table()
            self._update_summary(); self._update_plot()
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
        if self.monitor and self.monitor._running: self._stop_mon()
        else: self._start_mon()

    def _start_mon(self):
        if not self.output_dir:
            messagebox.showwarning("No folder", "Choose an output folder first."); return
        try: port = int(self.port_var.get()); poll = float(self.poll_var.get())
        except: messagebox.showerror("Error", "Invalid port or poll."); return
        self.monitor = FTPMonitor(self.host_var.get(), port, self.user_var.get(),
                                  self.pass_var.get(), self.dir_var.get(), poll)
        self.monitor.on_status_change = self._update_status
        self.monitor.on_log = self._log
        self.monitor.on_sample_changed = self._on_ftp_sample
        self.monitor.on_summary_changed = self._on_ftp_summary
        self.monitor.start()
        self.connect_btn.config(text="Stop Monitoring")
        self.refresh_btn.config(state=tk.NORMAL)
        self._log("Monitoring started")

    def _stop_mon(self):
        if self.monitor: self.monitor.stop()
        self.connect_btn.config(text="Connect & Monitor")
        self.refresh_btn.config(state=tk.DISABLED)
        self._log("Stopped"); self._update_status("Disconnected", False)

    def _force_refresh(self):
        if self.monitor: self.monitor.force_refresh()

    # ========== FTP CALLBACKS ==========

    def _on_ftp_sample(self, xml_bytes):
        try:
            if not is_sample_xml(xml_bytes): return
            parsed = parse_sample_xml_bytes(xml_bytes)
            if not self.session:
                tt = self.test_type_var.get()
                try: p = float(self.param_var.get())
                except: p = 100.0
                name = parsed["program_name"]
                if parsed["sample_id"]: name += f"_{parsed['sample_id']}"
                self.session = TestSession(name, tt, p)
                self.root.after(0, lambda: self.export_btn.config(state=tk.NORMAL))
            if not self.session.project_name and parsed["program_name"]:
                self.session.project_name = (
                    f"{parsed['program_name']}_{parsed['sample_id']}"
                    if parsed["sample_id"] else parsed["program_name"])
            self.session.add_sample(parsed, xml_bytes)
            sf = sanitize_filename(self.session.project_name or "session")
            xo, co = archive_sample(xml_bytes, parsed, self.output_dir, sf)
            self._log(f"[#{self.session.count}] {os.path.basename(xo)}")
            self.root.after(0, self._update_session_label)
            self.root.after(0, self._refresh_table)
            self.root.after(0, self._update_summary)
            self.root.after(0, self._update_plot)
        except Exception as e:
            self._log(f"Error: {e}")

    def _on_ftp_summary(self, xml_bytes):
        try:
            parsed = parse_summary_xml_bytes(xml_bytes)
            if self.session: self.session.last_summary = parsed
            self._log(f"Machine summary: {parsed['program_name']}")
            self.root.after(0, self._show_machine_summary, parsed)
        except Exception as e:
            self._log(f"Summary error: {e}")

    # ========== BATCH IMPORT ==========

    def _import_batch(self):
        paths = filedialog.askopenfilenames(
            title="Select sample XML files",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")])
        if not paths: return

        if self.session and self.session.count > 0:
            ch = messagebox.askyesnocancel("Active Session",
                f"{self.session.count} replicates exist.\n\n"
                "Yes = Export & start fresh\nNo = Add to current\nCancel = Abort")
            if ch is None: return
            if ch:
                if self.output_dir: self._export_summary()
                self.session = None

        loaded, skipped, errors = 0, 0, []
        for path in sorted(paths):
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                if not is_sample_xml(raw):
                    skipped += 1
                    self._log(f"  Skipped (not sample): {os.path.basename(path)}")
                    continue
                self._load_sample_bytes(raw, os.path.basename(path))
                loaded += 1
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")

        self._log(f"Import: {loaded} loaded, {skipped} skipped, {len(errors)} errors")
        if loaded > 0:
            msg = f"Imported {loaded} sample{'s' if loaded!=1 else ''}"
            if skipped: msg += f"\n({skipped} non-sample files skipped)"
            if self.session:
                st = self.session.get_summary_stats()
                if st: msg += f"\n\n{st['name']}: Mean={st['mean']} {st['unit']}, COV={st['cov']}%"
            messagebox.showinfo("Import Complete", msg)

    def _load_sample_bytes(self, xml_bytes, filename=""):
        """Load sample bytes into session."""
        parsed = parse_sample_xml_bytes(xml_bytes)
        if not self.session:
            tt = self.test_type_var.get()
            try: p = float(self.param_var.get())
            except: p = 100.0
            name = parsed["program_name"]
            if parsed["sample_id"]: name += f"_{parsed['sample_id']}"
            self.session = TestSession(name, tt, p)
            self.export_btn.config(state=tk.NORMAL)
        if not self.session.project_name:
            self.session.project_name = parsed["program_name"]
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
            messagebox.showinfo("Nothing", "No replicates."); return
        if not self.output_dir:
            self._pick_dir()
            if not self.output_dir: return
        sf = sanitize_filename(self.session.project_name or "session")
        p = export_session_summary(self.session, self.output_dir, sf)
        self._log(f"Exported: {os.path.basename(p)}")
        messagebox.showinfo("Exported", f"Saved to:\n{p}")

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
            if messagebox.askyesno("Export?",
                f"Export {self.session.count} replicates before closing?"):
                self._export_summary()
        if self.monitor: self.monitor.stop()
        self.root.destroy()


if __name__ == "__main__":
    try:
        app = CrushReaderApp()
        app.run()
    except Exception as e:
        import traceback
        try: messagebox.showerror("Fatal Error", f"{e}\n\n{traceback.format_exc()}")
        except: pass
        raise
