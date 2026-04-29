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

You need a Windows machine with Python 3.8+ installed. The lab computer does **not** need Python — it just runs the final `.exe`.

1. Clone or copy this folder to the machine that has Python.
2. Double-click `build_exe.bat` (or run it from a terminal).
3. Wait ~1–2 minutes. The script installs PyInstaller and matplotlib, then builds the executable.
4. Find the result in `dist\CrushReader.exe` (~40–60 MB).
5. Copy `CrushReader.exe` to the lab computer via USB drive or network.

### Notes

- The `.exe` is fully self-contained. No Python runtime needed on the target machine.
- Windows SmartScreen may show a warning the first time the `.exe` runs because it isn't digitally signed. Click **More info → Run anyway**.
- If antivirus flags the `.exe`, add an exception for it. PyInstaller executables are commonly flagged as false positives.

### Troubleshooting

- **"Python not found"** — make sure Python is on your `PATH`. Run `python --version` to verify. If it doesn't work, reinstall Python and check **Add to PATH**.
- **Build errors about missing modules** — make sure `pip` works. Try `pip install matplotlib`.
- **`.exe` crashes on the lab computer** — run it from a command prompt (`cmd` → `cd` to the folder → `CrushReader.exe`) to see the error message.

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

## User Guide

This section covers everyday use of the tool for lab technicians and students.

### 1. Starting the application

Double-click **CrushReader.exe** on the lab computer (or run `python crush_reader.py` if you have Python installed). The main window has four areas:

- **Left panel** — FTP connection settings, test parameters, and a log.
- **Top-right** — replicate table showing each sample collected.
- **Middle-right** — session summary with statistics.
- **Bottom-right** — force-displacement plot.

### 2. Connecting to the crush tester

Before connecting, make sure the lab computer is plugged into the crush tester via Ethernet cable.

1. **Choose an output folder.** Click **Browse...** under "Save to:" and pick a folder where archived test data will be saved. This is required before connecting.
2. **Check the connection settings.** The defaults (above) should work unless your machine has been reconfigured.
3. **Click "Connect & Monitor."** The status dot turns green when connected. If it fails, check that the Ethernet cable is plugged in and the tester is powered on.

Once connected, the tool polls the tester every 5 seconds. When you run a test on the machine, the tool will detect the new data within a few seconds.

### 3. Running a test session

A **session** groups multiple replicates of the same test so you can get summary statistics.

**Starting a new session.** Click **New Session** (top-left) to open the session dialog:

- **Project / Sample Name** — give the session a descriptive name (e.g., "C-flute ECT April 28"). If left blank, the tool auto-fills from the machine's program name.
- **Test Type** — choose ECT, FCT, or Generic depending on what you're testing.

Click **Start Session** to begin.

**Collecting replicates.** Run tests on the crush tester as normal. Each time you complete a test, the tool will:

1. Download the new `sample.xml` from the machine.
2. Archive a timestamped copy in your output folder.
3. Parse the force-displacement data.
4. Compute the test-specific value (ECT, FCT, or peak force).
5. Add the replicate to the table and update the plot.

**Ending a session.** Click **New Session** to start a fresh one (you'll be prompted to export), or click **Export Summary CSV** to save the session data. Closing the application will also prompt you to export.

### 4. Test types and parameters

**ECT (Edge Crush Test)** — measures edge crush resistance in **kN/m**. Default parameter: specimen length in mm (typically 100 mm).

**FCT (Flat Crush Test)** — measures flat crush resistance in **kPa**. Default parameter: specimen area in cm² (typically 100 cm² for a 10×10 cm specimen).

**Generic** — reports peak load in **N** with no additional calculation. Use this when you just need the raw peak force.

**Changing parameters after testing.** If specimen dimensions were entered incorrectly, you can fix them without re-running tests:

- **All samples** — update the value in the "Test Parameters" panel and click **Apply to all**.
- **One sample** — double-click the **Param** cell in the replicate table, type the new value, and press Enter.

The computed values and statistics update immediately.

### 5. Working with the replicate table

The table shows one row per sample with these columns:

| Column       | Description                                      |
|--------------|--------------------------------------------------|
| Plot         | ✓ = included in plot and statistics. Click to toggle. |
| #            | Sample number (order collected).                  |
| Sample ID    | ID from the machine.                              |
| Sample No    | Replicate number from the machine.                |
| Peak Force   | Maximum force recorded (N).                       |
| Param        | Specimen dimension (length or area). Double-click to edit. |
| Computed     | Calculated ECT, FCT, or peak force value.         |
| Unit         | Unit of the computed value.                       |

Click the **Plot** column for any sample to toggle it. Excluded samples are removed from the statistics and the graph — useful for discarding outliers without deleting data. Click any row to highlight that curve on the graph.

### 6. Reading the plot

The force-displacement plot shows all included samples overlaid with different colors.

- **X-axis:** displacement in mm (zeroed at the force threshold).
- **Y-axis:** force in Newtons.
- **Dots on curves:** peak force markers.
- **Legend:** sample numbers (up to 15 samples).

**Zeroing threshold.** The displacement axis is zeroed at the point where force first exceeds the **zeroing threshold** in the compressive (positive) direction (default: 10 N). This eliminates the initial flat / negative-noise region where the platen hasn't contacted the specimen yet. To change the threshold, type a new value and click **Apply**.

**Plot toolbar.** The matplotlib toolbar lets you pan, zoom (drag a rectangle), reset (home), and save the plot as PNG/PDF/SVG.

### 7. Importing saved files (batch import)

You don't need the machine connected to analyze data.

1. Click **Import Files...** (top bar) or use **File → Import Files...**.
2. Select one or more `.xml` files. Use Shift+Click or Ctrl+Click for multiples.
3. The tool loads all valid sample files and skips summary files automatically.

If a session is already active, you'll be asked whether to export it and start fresh, or add the imported files to the existing session.

Useful for re-analyzing old data with different parameters, combining sessions, or working on a computer that isn't connected to the tester.

### 8. Exporting data

**Session summary CSV.** Click **Export Summary CSV** to save a CSV with project name, test type, date, replicate count, summary statistics (mean, std, COV, min, max), and per-replicate data.

**Individual sample files.** When connected to the machine, each sample is automatically archived as both an XML file (exact copy from the machine) and a CSV file (metadata, properties, and zeroed force-displacement data), with timestamped filenames like `ECT_T839_01_20260428_143022.xml`.

### 9. Troubleshooting

- **"Connection failed" / status dot stays red** — check the Ethernet cable, tester power, IP address (default 192.168.0.3), and that your computer's adapter is on the same subnet (e.g., 192.168.0.x with mask 255.255.255.0).
- **Connection drops and reconnects** — normal if the tester is busy or the network is unstable. The tool reconnects with backoff automatically.
- **Peak force or computed values seem wrong** — check the Param column (double-click to fix), confirm the right test type is selected, and compare with the machine's own summary display.
- **Windows SmartScreen blocks the .exe** — click **More info → Run anyway**. The `.exe` is unsigned but built from this source.
- **Antivirus flags the .exe** — add an exception. PyInstaller executables are commonly flagged as false positives.
- **Import skips files** — only sample files (`<SAMPLE>` root tag) are imported; summary files (`<SAMPLESET>`) are skipped automatically.

### 10. Tips

- Always set the output folder *before* connecting, or early-arriving data won't be archived to disk.
- Name sessions descriptively — the name is used for the archive subfolder and export filenames.
- Export before closing.
- Use batch import for reporting and re-analysis.
- The zeroing threshold affects exported CSVs too; archived CSVs use the threshold set at archive time.

---

## Project Structure

```
crush_reader.py             Main application (single file)
build_exe.bat               Windows build script for PyInstaller
requirements.txt            Runtime dependencies
requirements-dev.txt        Test / dev dependencies
tests/test_crush_reader.py  Unit + integration tests
tests/fixtures/             Example XML data from the machine
```

---

## Requirements

**To run from source:** Python 3.8+, matplotlib

**To run the .exe:** Windows 10 or 11 (no other software needed)

**Hardware:** Ethernet connection to the ABB LW Crush Tester

---

## License

Internal tool — Virginia Tech, Department of Sustainable Biomaterials.
