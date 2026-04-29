# CrushReader — User Guide

This guide covers everyday use of the ABB Crush Tester Data Reader for lab technicians and students.

---

## 1. Starting the Application

Double-click **CrushReader.exe** on the lab computer (or run `python crush_reader.py` if you have Python installed). The main window has four areas:

- **Left panel** — FTP connection settings, test parameters, and a log.
- **Top-right** — replicate table showing each sample collected.
- **Middle-right** — session summary with statistics.
- **Bottom-right** — force-displacement plot.

---

## 2. Connecting to the Crush Tester

Before connecting, make sure the lab computer is plugged into the crush tester via Ethernet cable.

1. **Choose an output folder.** Click **Browse...** under "Save to:" and pick a folder where archived test data will be saved. This is required before connecting.
2. **Check the connection settings.** The defaults should work unless your machine has been reconfigured:
   - Host: `192.168.0.3`
   - Port: `21`
   - Username: `lwuser`
   - Password: `lwapp`
   - Remote dir: `/results`
3. **Click "Connect & Monitor."** The status dot will turn green when connected. If it fails, check that the Ethernet cable is plugged in and the tester is powered on.

Once connected, the tool polls the tester every 5 seconds. When you run a test on the machine, the tool will automatically detect the new data within a few seconds.

---

## 3. Running a Test Session

A **session** groups multiple replicates of the same test so you can get summary statistics.

### Starting a new session

Click **New Session** (top-left) to open the session dialog:

- **Project / Sample Name** — give the session a descriptive name (e.g., "C-flute ECT April 28"). If left blank, the tool auto-fills from the machine's program name.
- **Test Type** — choose ECT, FCT, or Generic depending on what you're testing.

Click **Start Session** to begin.

### Collecting replicates

Run tests on the crush tester as normal. Each time you complete a test, the tool will:

1. Download the new `sample.xml` from the machine.
2. Archive a timestamped copy in your output folder.
3. Parse the force-displacement data.
4. Compute the test-specific value (ECT, FCT, or peak force).
5. Add the replicate to the table and update the plot.

You'll see each sample appear in the replicate table and on the graph automatically.

### Ending a session

When you're done testing, either click **New Session** to start a fresh one (you'll be prompted to export) or click **Export Summary CSV** to save the session data. You can also just close the application — it will ask if you want to export first.

---

## 4. Test Types and Parameters

### ECT (Edge Crush Test)

Measures edge crush resistance in **kN/m**.

- **Formula:** ECT = Peak Force (N) / Specimen Length (mm)
- **Default parameter:** Specimen length in mm (typically 100 mm for standard tests).
- Set the default length in the "Test Parameters" panel on the left.

### FCT (Flat Crush Test)

Measures flat crush resistance in **kPa**.

- **Formula:** FCT = Peak Force (N) × 10 / Specimen Area (cm²)
- **Default parameter:** Specimen area in cm² (typically 100 cm² for a 10×10 cm specimen).

### Generic

Reports peak load in **N** with no additional calculation. Use this when you just need the raw peak force.

### Changing parameters after testing

If you realize the specimen dimensions were entered incorrectly, you can fix them without re-running the tests:

- **Change for all samples:** Update the value in the "Test Parameters" panel and click **Apply to all**.
- **Change for one sample:** Double-click the **Param** cell in the replicate table for that sample. Type the new value and press Enter.

The computed values and statistics will update immediately.

---

## 5. Working with the Replicate Table

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
| Unit         | Unit of the computed value.                        |

### Including / excluding samples

Click the **Plot** column (✓) for any sample to toggle it. Excluded samples are removed from the statistics and the graph. This is useful for discarding outliers or bad tests without deleting the data.

### Selecting a sample

Click any row to highlight that curve on the graph. The selected curve becomes thicker and other curves fade, making it easy to identify which physical sample corresponds to which curve.

---

## 6. Reading the Plot

The force-displacement plot shows all included samples overlaid with different colors.

- **X-axis:** Displacement in mm (zeroed at the force threshold).
- **Y-axis:** Force in Newtons.
- **Dots on curves:** Peak force markers showing where the maximum load occurred.
- **Legend:** Shows sample numbers (up to 15 samples).

### Zeroing threshold

The displacement axis is zeroed at the point where force first exceeds the **zeroing threshold** (default: 10 N). This eliminates the initial flat region where the platen hasn't contacted the specimen yet.

To change the threshold, type a new value in the "Zeroing threshold" field and click **Apply**. A lower threshold keeps more of the initial curve; a higher threshold trims more.

### Plot toolbar

The matplotlib toolbar below the plot lets you:

- **Pan** — click and drag to move around the plot.
- **Zoom** — draw a rectangle to zoom into an area.
- **Home** — reset to the original view.
- **Save** — export the plot as a PNG, PDF, or SVG image.

---

## 7. Importing Saved Files (Batch Import)

You don't need the machine connected to analyze data. If you have previously saved XML files (from the archive folder or from USB), you can import them.

1. Click **Import Files...** (top bar) or use **File → Import Files...**.
2. Select one or more `.xml` files. You can select multiple files at once using Shift+Click or Ctrl+Click.
3. The tool will load all valid sample files, automatically skipping any summary files.

If there's already an active session, you'll be asked whether to export the current session and start fresh, or add the imported files to the existing session.

This is useful for:

- Re-analyzing old test data with different parameters.
- Combining data from multiple testing sessions.
- Working on a computer that isn't connected to the tester.

---

## 8. Exporting Data

### Session summary CSV

Click **Export Summary CSV** to save a CSV file with:

- Project name, test type, date, and replicate count.
- Summary statistics (mean, std, COV, min, max) for both the computed value and peak force.
- Per-replicate data including include/exclude status, parameters, and all machine-reported properties.

The file is saved in your output folder under a subfolder named after the session.

### Individual sample files

When connected to the machine, each sample is automatically archived as both:

- An **XML** file (exact copy from the machine).
- A **CSV** file with metadata, properties, and zeroed force-displacement data.

These are saved in your output folder with timestamped filenames like:
```
ECT_T839_01_20260428_143022.xml
ECT_T839_01_20260428_143022.csv
```

---

## 9. Troubleshooting

**"Connection failed" / status dot stays red**
- Check that the Ethernet cable is plugged into both the computer and the tester.
- Make sure the tester is powered on.
- Verify the IP address matches your tester (default: 192.168.0.3).
- Check that your computer's Ethernet adapter is on the same subnet (e.g., 192.168.0.x with mask 255.255.255.0).

**Connection drops and reconnects frequently**
- This is normal if the tester is busy or the network is unstable. The tool will automatically reconnect with backoff and resume monitoring.

**Peak force or computed values seem wrong**
- Check the specimen dimensions (Param column). Double-click to correct if needed.
- Make sure the correct test type is selected (ECT vs FCT vs Generic).
- Compare with the machine's own summary display.

**Windows SmartScreen blocks the .exe**
- Click **More info → Run anyway**. This happens because the .exe isn't digitally signed. It's safe — it was built from this source code with PyInstaller.

**Antivirus flags the .exe**
- Add an exception for CrushReader.exe. PyInstaller executables are commonly flagged as false positives.

**Import skips my files**
- Only `sample.xml`-type files (with `<SAMPLE>` as the root XML tag) are imported. Summary files (`<SAMPLESET>`) are automatically skipped. Make sure you're selecting the right files.

---

## 10. Tips

- **Always set the output folder** before connecting. If the machine runs a test before you've chosen a folder, the data will still be captured in the session but won't be archived to disk.
- **Name your sessions** descriptively. The session name is used for the archive subfolder and export filenames.
- **Export before closing.** The tool will remind you, but it's good practice to export your session summary before shutting down.
- **Use batch import for reporting.** If you need to re-analyze data with different parameters or compare across sessions, import the archived XML files into a new session.
- **The zeroing threshold affects exported CSVs too.** The displacement values in archived CSV files use the threshold that was set at the time of archiving.
