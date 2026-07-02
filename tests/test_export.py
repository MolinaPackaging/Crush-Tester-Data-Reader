"""Tests for the one-click export bundle: all-replicates data CSV, the shared
plot renderer, and the SVG/PNG graph export."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crush_reader import (  # noqa: E402
    TestSession,
    export_all_curves,
    export_session_graph,
    parse_sample_xml,
    render_session_curves,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_XML_PATH = FIXTURES / "sample.xml"


def _session_with(n: int) -> TestSession:
    """A session populated with n replicates derived from the real fixture,
    each given a distinct SAMPLENO and a scaled curve."""
    s = TestSession("Export Test", "FCT", 100.0)
    base = parse_sample_xml(SAMPLE_XML_PATH)
    for k in range(n):
        rep = dict(base)
        rep["sample_no"] = str(k + 1)
        rep["y_values"] = [v * (1 + 0.1 * k) for v in base["y_values"]]
        rep["x_values"] = list(base["x_values"])
        rep["results"] = list(base["results"])
        s.ingest_sample(rep, b"<x/>")
    return s


class AllCurvesCsvTests(unittest.TestCase):
    def test_wide_layout_has_a_column_pair_per_sample(self):
        s = _session_with(3)
        with tempfile.TemporaryDirectory() as tmp:
            path = export_all_curves(s, tmp, "sess", threshold=10.0)
            rows = list(csv.reader(path.open(encoding="utf-8-sig")))
        self.assertEqual(len(rows[1]), 6)  # 3 samples x (Disp, Force)
        self.assertEqual(rows[1][:2], ["Displacement (mm)", "Force (N)"])
        self.assertIn("included", rows[0][0])
        # data rows present and start at zeroed displacement
        self.assertGreater(len(rows) - 2, 50)
        self.assertEqual(rows[2][0], "0.0000")

    def test_excluded_samples_are_still_exported(self):
        s = _session_with(2)
        s.toggle_included(1)  # exclude the 2nd
        with tempfile.TemporaryDirectory() as tmp:
            path = export_all_curves(s, tmp, "sess")
            rows = list(csv.reader(path.open(encoding="utf-8-sig")))
        self.assertEqual(len(rows[1]), 4)  # both samples still have columns
        self.assertIn("excluded", rows[0][2])


class GraphExportTests(unittest.TestCase):
    def test_writes_valid_svg_and_png(self):
        s = _session_with(2)
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_session_graph(s, tmp, "sess", threshold=10.0,
                                         formats=("svg", "png"))
            by_ext = {p.suffix: p for p in paths}
            self.assertIn(".svg", by_ext)
            self.assertIn(".png", by_ext)
            svg = by_ext[".svg"].read_bytes()
            png = by_ext[".png"].read_bytes()
        self.assertTrue(svg.lstrip().startswith(b"<?xml"))
        self.assertIn(b"<svg", svg[:600])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_render_returns_included_count(self):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        s = _session_with(3)
        s.toggle_included(0)
        fig = Figure()
        ax = fig.add_subplot(111)
        n = render_session_curves(ax, s, threshold=10.0)
        self.assertEqual(n, 2)  # 3 minus 1 excluded


if __name__ == "__main__":
    unittest.main()
