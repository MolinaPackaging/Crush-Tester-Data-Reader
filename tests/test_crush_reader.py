"""Test suite for the ABB Crush Tester Data Reader.

These tests cover the pure data-handling layer: XML parsing, force-displacement
processing, session statistics, and CSV archiving. The Tk GUI is not exercised.

Run with::

    pytest tests/

or::

    python -m unittest discover tests
"""

from __future__ import annotations

import csv
import math
import sys
import unittest
from pathlib import Path

# Allow running these tests directly from the project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crush_reader import (  # noqa: E402
    DEFAULT_PARAM_VALUE,
    DEFAULT_THRESHOLD_N,
    TestSession,
    apply_threshold_zeroing,
    archive_sample,
    compute_value,
    export_session_summary,
    is_sample_xml,
    parse_comma_values,
    parse_sample_xml,
    parse_sample_xml_bytes,
    parse_summary_xml_bytes,
    sanitize_filename,
)

SAMPLE_XML_PATH = ROOT / "sample.xml"
SUMMARY_XML_PATH = ROOT / "summary.xml"


# ---------------------------------------------------------------------------
#  parse_comma_values
# ---------------------------------------------------------------------------


class CommaValuesTests(unittest.TestCase):
    def test_empty_inputs_return_empty(self):
        self.assertEqual(parse_comma_values(""), [])
        self.assertEqual(parse_comma_values("   "), [])

    def test_simple_decimals(self):
        self.assertEqual(
            parse_comma_values("1.5,2.5,3.5"),
            [1.5, 2.5, 3.5],
        )

    def test_thousand_separator_is_recombined(self):
        # The XML emits "1,033.50" as a single value with a thousand-separator
        # comma. Naïve CSV splitting would produce two values.
        self.assertEqual(
            parse_comma_values("996.97,1,033.50,1,068.08"),
            [996.97, 1033.50, 1068.08],
        )

    def test_million_continuation(self):
        self.assertEqual(parse_comma_values("1,234,567.89"), [1234567.89])

    def test_pure_integer_thousand(self):
        self.assertEqual(parse_comma_values("1,000"), [1000.0])

    def test_negative_values_not_treated_as_continuation(self):
        # A negative number like "-10.19" must never be merged into the
        # previous accumulator.
        self.assertEqual(
            parse_comma_values("-9.71,-10.19,5.83"),
            [-9.71, -10.19, 5.83],
        )

    def test_short_integer_neighbours(self):
        # A single-digit token can't be a thousand continuation (needs 3 digits).
        self.assertEqual(parse_comma_values("2,3,4"), [2.0, 3.0, 4.0])

    def test_skips_blank_tokens(self):
        self.assertEqual(parse_comma_values("1.0,,2.0"), [1.0, 2.0])


# ---------------------------------------------------------------------------
#  is_sample_xml / parse_sample_xml / parse_summary_xml_bytes
# ---------------------------------------------------------------------------


class SampleXmlTests(unittest.TestCase):
    def test_real_sample_xml_is_recognized(self):
        self.assertTrue(is_sample_xml(SAMPLE_XML_PATH.read_bytes()))

    def test_real_summary_xml_is_rejected(self):
        # SAMPLESET, not SAMPLE — must not be picked up by the FTP sample path.
        self.assertFalse(is_sample_xml(SUMMARY_XML_PATH.read_bytes()))

    def test_empty_input(self):
        self.assertFalse(is_sample_xml(b""))

    def test_garbage_input(self):
        self.assertFalse(is_sample_xml(b"not xml"))

    def test_sample_xml_parses_into_expected_shape(self):
        d = parse_sample_xml(SAMPLE_XML_PATH)
        self.assertEqual(d["sample_id"], "fctteas")
        self.assertEqual(d["program_name"], "FCT 100 cm²")
        self.assertEqual(d["sample_no"], "1")
        self.assertEqual(len(d["x_values"]), len(d["y_values"]))
        self.assertGreater(len(d["x_values"]), 100)
        # The peak force in this data set is ~2232 N (FCT ~223 kPa with 100 cm²).
        self.assertAlmostEqual(max(d["y_values"]), 2232.41, places=2)

    def test_results_block_is_parsed(self):
        d = parse_sample_xml_bytes(SAMPLE_XML_PATH.read_bytes())
        names = [r["property_name"] for r in d["results"]]
        self.assertIn("FCT", names)
        self.assertIn("Hardness", names)

    def test_summary_xml_parses(self):
        s = parse_summary_xml_bytes(SUMMARY_XML_PATH.read_bytes())
        self.assertEqual(s["program_name"], "ECT T839")
        self.assertTrue(any(it["property_name"] == "ECT" for it in s["items"]))


# ---------------------------------------------------------------------------
#  apply_threshold_zeroing
# ---------------------------------------------------------------------------


class ThresholdZeroingTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(apply_threshold_zeroing([], []), ([], []))

    def test_starts_at_first_positive_threshold_crossing(self):
        # y crosses +10 at index 2 (y=15). The negative excursion at index 1
        # is *not* a crossing — only positive force counts as engagement.
        x = [0.0, 1.0, 2.0, 3.0, 4.0]
        y = [0.5, -50.0, 15.0, 50.0, 100.0]
        xz, yz = apply_threshold_zeroing(x, y, threshold=10.0)
        self.assertEqual(yz, [15.0, 50.0, 100.0])
        self.assertEqual(xz[0], 0.0)
        self.assertEqual(xz, [0.0, 1.0, 2.0])

    def test_negative_noise_is_not_a_crossing(self):
        # The real crush tester idles around -10 N. With threshold=10 we
        # must skip the noise and start at the first genuine positive load.
        x = [0.0, 0.1, 0.2, 0.3, 0.4]
        y = [-10.19, -9.71, -10.68, 11.17, 200.0]
        xz, yz = apply_threshold_zeroing(x, y, threshold=10.0)
        self.assertEqual(yz, [11.17, 200.0])
        self.assertEqual(xz, [0.0, pytest_isclose(0.1)])

    def test_x_is_zeroed_to_first_kept_sample(self):
        # y crosses +10 at index 1 (y=100), so the first kept x is x[1]=9.
        # Function returns abs(x0 - x); since x is decreasing, xz is monotonic.
        x = [10.0, 9.0, 8.0]
        y = [0.0, 100.0, 200.0]
        xz, yz = apply_threshold_zeroing(x, y, threshold=10.0)
        self.assertEqual(yz, [100.0, 200.0])
        self.assertEqual(xz, [0.0, 1.0])

    def test_no_crossing_keeps_everything(self):
        # Below-threshold (and entirely negative) inputs: fall back to
        # keeping all samples zeroed at x[0]. This preserves the curve so
        # the user can still see what was recorded.
        xz, yz = apply_threshold_zeroing([1.0, 2.0], [-5.0, -1.0], threshold=10.0)
        self.assertEqual(yz, [-5.0, -1.0])
        self.assertEqual(xz[0], 0.0)

    def test_default_threshold_constant(self):
        # The default is exposed as a constant so the GUI and the function
        # cannot drift apart.
        x = [0.0, 1.0]
        y = [DEFAULT_THRESHOLD_N - 0.1, DEFAULT_THRESHOLD_N + 0.1]
        xz, yz = apply_threshold_zeroing(x, y)
        self.assertEqual(yz, [DEFAULT_THRESHOLD_N + 0.1])
        self.assertEqual(xz, [0.0])

    def test_real_sample_xml_trims_noise(self):
        # End-to-end: the real machine data has ~1033 points, mostly noise
        # at -10 N before the platen engages. With threshold=10 the trimmed
        # curve should be substantially shorter than the raw curve.
        d = parse_sample_xml(SAMPLE_XML_PATH)
        raw_n = len(d["y_values"])
        xz, yz = apply_threshold_zeroing(d["x_values"], d["y_values"])
        self.assertLess(len(yz), raw_n)
        self.assertGreaterEqual(yz[0], DEFAULT_THRESHOLD_N)
        # Peak survives the trim.
        self.assertAlmostEqual(max(yz), 2232.41, places=2)


def pytest_isclose(expected: float, rel: float = 1e-9, abs_: float = 1e-9):
    """Tiny helper: object that compares equal to a float within tolerance."""
    class _Close:
        def __eq__(self, other):
            return isinstance(other, float) and math.isclose(
                other, expected, rel_tol=rel, abs_tol=abs_)
        def __repr__(self):
            return f"≈{expected}"
    return _Close()


# ---------------------------------------------------------------------------
#  compute_value
# ---------------------------------------------------------------------------


class ComputeValueTests(unittest.TestCase):
    def _sample(self, peak: float) -> dict:
        return {"y_values": [0.0, peak, peak / 2]}

    def test_ect(self):
        v = compute_value(self._sample(2000.0), "ECT", 100.0)
        self.assertEqual(v["name"], "ECT")
        self.assertEqual(v["unit"], "kN/m")
        # 2000 N / 100 mm = 20 kN/m
        self.assertAlmostEqual(v["value"], 20.0)

    def test_fct(self):
        v = compute_value(self._sample(2000.0), "FCT", 100.0)
        self.assertEqual(v["name"], "FCT")
        self.assertEqual(v["unit"], "kPa")
        # 2000 N * 10 / 100 cm² = 200 kPa
        self.assertAlmostEqual(v["value"], 200.0)

    def test_generic_returns_peak(self):
        v = compute_value(self._sample(1234.5), "Generic", 0.0)
        self.assertEqual(v["name"], "Peak Force")
        self.assertEqual(v["unit"], "N")
        self.assertAlmostEqual(v["value"], 1234.5, places=1)

    def test_zero_param_falls_back_to_generic(self):
        # ECT with param=0 would divide by zero — function falls back to peak.
        v = compute_value(self._sample(1000.0), "ECT", 0.0)
        self.assertEqual(v["name"], "Peak Force")
        self.assertEqual(v["unit"], "N")

    def test_empty_curve(self):
        v = compute_value({"y_values": []}, "ECT", 100.0)
        self.assertEqual(v["peak_force"], 0.0)


# ---------------------------------------------------------------------------
#  TestSession
# ---------------------------------------------------------------------------


class TestSessionTests(unittest.TestCase):
    def _make_sample(self, peak: float, sample_id: str = "S") -> dict:
        return {
            "code_id": "1", "sample_id": sample_id, "operator_id": "",
            "program_name": "ECT test", "end_serie": "0", "sample_no": "1",
            "results": [], "x_values": [0.0, 1.0, 2.0], "y_values": [0.0, peak, peak / 2],
        }

    def test_add_sample_increments_count_and_includes_by_default(self):
        s = TestSession("p", "ECT", DEFAULT_PARAM_VALUE)
        s.add_sample(self._make_sample(1000.0), b"<x/>")
        self.assertEqual(s.count, 1)
        self.assertEqual(s.included, [True])
        self.assertEqual(s.sample_params, [DEFAULT_PARAM_VALUE])

    def test_summary_stats_n_minus_1_std(self):
        s = TestSession("p", "Generic", 0)
        for peak in (100.0, 200.0, 300.0):
            s.add_sample(self._make_sample(peak), b"<x/>")
        st = s.get_summary_stats()
        self.assertEqual(st["n"], 3)
        self.assertAlmostEqual(st["mean"], 200.0)
        # n=3 sample std of {100,200,300} == 100.0 exactly
        self.assertAlmostEqual(st["std"], 100.0)
        self.assertAlmostEqual(st["min"], 100.0)
        self.assertAlmostEqual(st["max"], 300.0)

    def test_excluded_samples_are_dropped_from_stats(self):
        s = TestSession("p", "Generic", 0)
        for peak in (100.0, 1_000_000.0, 200.0):
            s.add_sample(self._make_sample(peak), b"<x/>")
        s.toggle_included(1)  # exclude the outlier
        st = s.get_summary_stats()
        self.assertEqual(st["n"], 2)
        self.assertAlmostEqual(st["mean"], 150.0)

    def test_no_included_samples_returns_empty_stats(self):
        s = TestSession("p", "Generic", 0)
        s.add_sample(self._make_sample(100.0), b"<x/>")
        s.toggle_included(0)
        self.assertEqual(s.get_summary_stats(), {})

    def test_set_test_type_recomputes_all(self):
        s = TestSession("p", "Generic", 0)
        s.add_sample(self._make_sample(1000.0), b"<x/>")
        s.set_test_type("ECT", 100.0)
        self.assertEqual(s.test_type, "ECT")
        self.assertEqual(s.samples[0]["computed"]["unit"], "kN/m")
        self.assertAlmostEqual(s.samples[0]["computed"]["value"], 10.0)

    def test_update_param_only_affects_target_sample(self):
        s = TestSession("p", "ECT", 100.0)
        s.add_sample(self._make_sample(1000.0), b"<x/>")
        s.add_sample(self._make_sample(1000.0), b"<x/>")
        s.update_param(0, 50.0)
        self.assertEqual(s.sample_params, [50.0, 100.0])
        self.assertAlmostEqual(s.samples[0]["computed"]["value"], 20.0)
        self.assertAlmostEqual(s.samples[1]["computed"]["value"], 10.0)


# ---------------------------------------------------------------------------
#  Filename / archive helpers
# ---------------------------------------------------------------------------


class FilenameTests(unittest.TestCase):
    def test_replaces_path_and_shell_unsafe_chars(self):
        self.assertEqual(
            sanitize_filename("foo/bar:baz*qux?"), "foo_bar_baz_qux")

    def test_collapses_leading_and_trailing_separators(self):
        self.assertEqual(sanitize_filename("///foo bar///"), "foo_bar")

    def test_keeps_dots_and_hyphens(self):
        self.assertEqual(sanitize_filename("a.b-c"), "a.b-c")

    def test_keeps_unicode_word_characters(self):
        # ² and similar Unicode "word" chars are valid on every modern
        # filesystem we target — preserving them keeps the program name readable.
        self.assertEqual(sanitize_filename("FCT 100 cm²"), "FCT_100_cm²")


class ArchiveTests(unittest.TestCase):
    def test_archive_sample_writes_xml_and_csv_with_utf8_bom(self):
        import tempfile

        parsed = parse_sample_xml(SAMPLE_XML_PATH)
        parsed["computed"] = compute_value(parsed, "FCT", 100.0)
        with tempfile.TemporaryDirectory() as tmp:
            xml_path, csv_path = archive_sample(
                SAMPLE_XML_PATH.read_bytes(), parsed, tmp, "session_x")
            self.assertTrue(xml_path.exists())
            self.assertTrue(csv_path.exists())
            # CSV should be utf-8-sig (BOM) so Excel renders cm² / kPa.
            self.assertEqual(csv_path.read_bytes()[:3], b"\xef\xbb\xbf")
            # And it should round-trip through csv with utf-8-sig.
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertGreater(len(rows), 10)

    def test_export_session_summary_uses_utf8_sig(self):
        import tempfile

        s = TestSession("Project Foo", "FCT", 100.0)
        parsed = parse_sample_xml(SAMPLE_XML_PATH)
        s.add_sample(parsed, SAMPLE_XML_PATH.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            path = export_session_summary(s, tmp, "session_x")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("Project Foo", text)
            self.assertIn("Area (cm²)", text)


# ---------------------------------------------------------------------------
#  End-to-end: real sample.xml through the full pipeline
# ---------------------------------------------------------------------------


class EndToEndTests(unittest.TestCase):
    def test_real_sample_matches_machine_reported_fct(self):
        """Sanity check: parsing sample.xml and computing FCT with the
        program's stated 100 cm² area should reproduce the machine's own
        FCT figure (223 kPa)."""
        parsed = parse_sample_xml(SAMPLE_XML_PATH)
        v = compute_value(parsed, "FCT", 100.0)
        # Machine reports 223 kPa; we should be within ~1% (machine rounds).
        self.assertTrue(
            math.isclose(v["value"], 223.0, rel_tol=0.01),
            f"Expected ~223 kPa, got {v['value']}",
        )


if __name__ == "__main__":
    unittest.main()
