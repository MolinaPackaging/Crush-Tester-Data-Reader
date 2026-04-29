# ABB Crush Tester Data Reader

A standalone desktop tool for extracting, reading, and processing test data from an **ABB LW Crush Tester**. Designed for corrugated packaging labs that need an alternative to the proprietary ABB software.

The tool connects to the crush tester over FTP, monitors for new test results in real time, and provides session-based analysis with force-displacement plotting, summary statistics, and CSV export.

---

## Features

- **Live FTP monitoring** — connects to the crush tester via Ethernet, detects new tests automatically, and archives each sample before the machine overwrites it.
- **Session management** — collect multiple replicates into a session with running statistics (mean, std, COV, min/max).
- **Test-specific processing**:
  - **ECT** (Edge Crush Test) — computes ECT in kN/m from peak force and specimen length.
  - **FCT** (Flat Crush Test) — computes FCT in kPa from peak force and specimen area.
  - **Generic** — reports peak load in Newtons.
- **Per-sample editing** — double-click to change specimen dimensions for individual replicates; click to include/exclude samples from analysis.
- **Force-displacement plotting** — overlaid curves with peak markers, adjustable zeroing threshold, and matplotlib toolbar for zoom/pan/save.
- **Batch import** — load previously saved XML files for post-hoc analysis without needing the machine connected.
- **CSV export** — per-sample raw data and session summaries with all computed values.
- **Single-file .exe** — runs on any Windows 10/11 PC with no Python installation required.

---

## Quick Start

### Option A: Run the pre-built executable (lab computers)

1. Download or copy `CrushReader.exe` to the lab computer.
2. Double-click to run. Windows SmartScreen may warn you the first time — click **More info → Run anyway**.
3. That's it. No installation needed.

### Option B: Run from Python (development)

Requires Python 3.8+ and matplotlib.

```bash
pip install matplotlib
python crush_reader.py
```

---

## Building the Executable

You need a Windows machine with Python 3.8+ installed. The lab computer does **not** need Python.

1. Clone or copy this folder to the machine with Python.
2. Double-click `build_exe.bat` (or run it from a terminal).
3. Wait ~1–2 minutes. The script installs PyInstaller and matplotlib, then builds the .exe.
4. Find the result in `dist\CrushReader.exe` (~40–60 MB).
5. Copy `CrushReader.exe` to the lab computer via USB or network.

See `HOW_TO_BUILD.txt` for troubleshooting.

---

## Connection Setup

The ABB LW Crush Tester exposes test data via a built-in FTP server. Default connection settings:

| Setting     | Default         |
|-------------|-----------------|
| Host        | `192.168.0.3`   |
| Port        | `21`            |
| Username    | `lwuser`        |
| Password    | `lwapp`         |
| Remote dir  | `/results`      |

The computer must be connected to the tester via Ethernet (direct cable or through a switch on the same subnet). No internet connection is needed.

---

## How It Works

The crush tester writes two files to its FTP server after each test:

- **`sample.xml`** — raw force-displacement data for the last test. This file is **overwritten** on every new test.
- **`summary.xml`** — accumulated session statistics from the machine.

The tool polls the FTP server every few seconds. When `sample.xml` changes, it downloads the new data, parses it, archives a timestamped copy, and adds the replicate to the current session.

### Thousand-Separator Handling

The machine's XML uses commas as both value delimiters *and* thousand separators (e.g., `996.97,1,033.50,1,068.08`). The parser disambiguates by checking whether the accumulator already contains a decimal point — if it does, the number is complete and the next token starts a new value.

---

## Formulas

**ECT (Edge Crush Test)**
```
ECT (kN/m) = Peak Force (N) / Specimen Length (mm)
```
Since 1 N/mm = 1 kN/m, no conversion factor is needed.

**FCT (Flat Crush Test)**
```
FCT (kPa) = Peak Force (N) × 10 / Specimen Area (cm²)
```
Equivalently: FCT = Peak Force / Area(m²) / 1000.

---

## Project Structure

```
crush_reader.py     Main application (single file, ~1050 lines)
build_exe.bat       Windows build script for PyInstaller
HOW_TO_BUILD.txt    Step-by-step build instructions
USER_GUIDE.md       Guide for lab technicians and students
sample.xml          Example FCT test data from the machine
summary.xml         Example summary data from the machine
```

---

## Requirements

**To run from source:** Python 3.8+, matplotlib

**To run the .exe:** Windows 10 or 11 (no other software needed)

**Hardware:** Ethernet connection to the ABB LW Crush Tester

---

## License

Internal tool — Virginia Tech, Department of Sustainable Biomaterials.
