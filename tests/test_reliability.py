"""Regression tests for the v3.4 reliability redesign.

Covers the completed-test identity / completeness model and the de-duplicating
ingest path that replaced raw-file-hash change detection:

  * sample_identity / sample_completeness / is_complete_sample
  * TestSession.ingest_sample: added / duplicate / updated / incomplete
  * SAMPLENO gap detection (missed replicates)
  * new-series detection (SAMPLENO restart / SAMPLEID change)
  * summary SAMPLENOS parsing (authoritative miss reconciliation)

These use inline parsed dicts / XML so they need no fixture files.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crush_reader import (  # noqa: E402
    TestSession,
    is_complete_sample,
    parse_summary_xml_bytes,
    sample_completeness,
    sample_identity,
)


def make_parsed(sample_no, sample_id="S-1", *, peak=1000.0, n_points=300,
                with_results=True):
    """Build a parsed-sample dict shaped like parse_sample_xml_bytes output."""
    ys = [0.0] * (n_points - 1) + [peak] if n_points else []
    xs = [float(i) for i in range(n_points)]
    results = (
        [{"property_id": "15070", "property_name": "ECT",
          "unit": "kN/m", "value": "14.12"}]
        if with_results else []
    )
    return {
        "code_id": "1", "sample_id": sample_id, "operator_id": "",
        "program_name": "ECT 25x100 mm", "end_serie": "0",
        "sample_no": str(sample_no), "results": results,
        "x_values": xs, "y_values": ys,
    }


class IdentityAndCompletenessTests(unittest.TestCase):
    def test_identity_parses_sample_no_as_int(self):
        self.assertEqual(sample_identity(make_parsed(3, "ABC")), ("ABC", 3))

    def test_identity_handles_bad_sample_no(self):
        p = make_parsed(1)
        p["sample_no"] = "not-a-number"
        self.assertEqual(sample_identity(p), ("S-1", 0))

    def test_completeness_orders_partial_below_finished(self):
        partial = make_parsed(1, with_results=False, n_points=50)
        finished = make_parsed(1, with_results=True, n_points=300)
        self.assertLess(sample_completeness(partial),
                        sample_completeness(finished))

    def test_is_complete_requires_results_and_curve(self):
        self.assertTrue(is_complete_sample(make_parsed(1)))
        self.assertFalse(is_complete_sample(make_parsed(1, with_results=False)))
        self.assertFalse(is_complete_sample(make_parsed(1, n_points=0)))


class IngestDedupTests(unittest.TestCase):
    def setUp(self):
        self.s = TestSession("p", "ECT", 100.0)

    def test_new_sample_is_added(self):
        r = self.s.ingest_sample(make_parsed(1), b"<x/>")
        self.assertEqual(r["status"], "added")
        self.assertEqual(self.s.count, 1)

    def test_repeated_sample_no_is_not_duplicated(self):
        self.s.ingest_sample(make_parsed(1, peak=1000.0), b"<x/>")
        r = self.s.ingest_sample(make_parsed(1, peak=1000.0), b"<x/>")
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(self.s.count, 1)

    def test_incomplete_sample_is_not_ingested(self):
        r = self.s.ingest_sample(make_parsed(1, with_results=False), b"<x/>")
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(self.s.count, 0)

    def test_partial_then_complete_upserts_in_place(self):
        # A mid-write read that still has RESULTS but a short curve, then the
        # finished read with the full curve for the SAME SAMPLENO: must replace,
        # not duplicate, and keep the more-complete peak.
        self.s.ingest_sample(make_parsed(1, peak=500.0, n_points=100), b"<a/>")
        r = self.s.ingest_sample(make_parsed(1, peak=1400.0, n_points=800), b"<b/>")
        self.assertEqual(r["status"], "updated")
        self.assertEqual(self.s.count, 1)
        self.assertAlmostEqual(self.s.samples[0]["computed"]["peak_force"], 1400.0)

    def test_distinct_sample_nos_accumulate(self):
        for n in (1, 2, 3):
            self.s.ingest_sample(make_parsed(n), b"<x/>")
        self.assertEqual(self.s.count, 3)

    def test_gap_is_detected(self):
        self.s.ingest_sample(make_parsed(1), b"<x/>")
        self.s.ingest_sample(make_parsed(2), b"<x/>")
        r = self.s.ingest_sample(make_parsed(5), b"<x/>")  # skipped 3, 4
        self.assertEqual(r["status"], "added")
        self.assertEqual(r["gap"], [3, 4])
        self.assertEqual(self.s.count, 3)

    def test_no_gap_flag_on_first_sample(self):
        r = self.s.ingest_sample(make_parsed(7), b"<x/>")  # first ever
        self.assertEqual(r["gap"], [])

    def test_upsert_preserves_include_flag_and_param(self):
        self.s.ingest_sample(make_parsed(1, peak=500.0, n_points=100), b"<a/>")
        self.s.update_param(0, 50.0)
        self.s.toggle_included(0)  # exclude it
        self.s.ingest_sample(make_parsed(1, peak=1400.0, n_points=800), b"<b/>")
        self.assertEqual(self.s.sample_params[0], 50.0)
        self.assertFalse(self.s.included[0])
        # recomputed with the preserved 50 mm param: 1400 / 50 = 28.0 kN/m
        self.assertAlmostEqual(self.s.samples[0]["computed"]["value"], 28.0)


class SeriesChangeTests(unittest.TestCase):
    def test_same_id_sample_one_reread_is_deduped(self):
        # Re-reading / re-importing SAMPLENO 1 under the SAME SAMPLEID is a
        # duplicate, not a new series — this is what protects re-imports. To
        # start a genuinely new run of the same specimen ID, use New Session.
        s = TestSession("p", "ECT", 100.0)
        s.ingest_sample(make_parsed(1, "A"), b"<x/>")
        s.ingest_sample(make_parsed(2, "A"), b"<x/>")
        r = s.ingest_sample(make_parsed(1, "A"), b"<x/>")  # identical re-read
        self.assertFalse(r["series_changed"])
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(s.count, 2)

    def test_sample_id_change_starts_new_series(self):
        s = TestSession("p", "ECT", 100.0)
        s.ingest_sample(make_parsed(1, "A"), b"<x/>")
        r = s.ingest_sample(make_parsed(1, "B"), b"<x/>")
        self.assertTrue(r["series_changed"])
        self.assertEqual(s.count, 2)


class SummarySampleNosTests(unittest.TestCase):
    SUMMARY = (
        b'<?xml version="1.0" encoding="UTF-8"?><SAMPLESET>'
        b'<SAMPLEID>S-1</SAMPLEID><PROGRAMNAME>ECT</PROGRAMNAME>'
        b'<SUMMARY><ITEM><PROPERTYNAME>ECT</PROPERTYNAME><UNIT>kN/m</UNIT>'
        b'<MEAN>14</MEAN><COV>1</COV><STD>0.1</STD><NOVALUES>4</NOVALUES>'
        b'<SAMPLENOS>1,2,3,5</SAMPLENOS></ITEM></SUMMARY></SAMPLESET>'
    )

    def test_summary_extracts_sample_nos(self):
        d = parse_summary_xml_bytes(self.SUMMARY)
        self.assertEqual(d["sample_id"], "S-1")
        self.assertEqual(d["sample_nos"], [1, 2, 3, 5])
        self.assertEqual(d["total_samples"], 4)


if __name__ == "__main__":
    unittest.main()
