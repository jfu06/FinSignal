"""Unit tests for the XBRL data layer (parsing + dedup + metric registry)."""

from __future__ import annotations

from app.xbrl import METRICS, dedupe_rows, parse_companyfacts

FIXTURE = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "NetIncomeLoss": {
                "label": "Net Income (Loss)",
                "units": {
                    "USD": [
                        # FY2007 as originally filed…
                        {"start": "2006-10-01", "end": "2007-09-29",
                         "val": 3496000000, "accn": "0001047469-07-009340",
                         "fy": 2007, "fp": "FY", "form": "10-K",
                         "filed": "2007-11-15"},
                        # …and restated in the 10-K/A with a DIFFERENT value (§5.3)
                        {"start": "2006-10-01", "end": "2007-09-29",
                         "val": 3495000000, "accn": "0001047469-08-000000",
                         "fy": 2007, "fp": "FY", "form": "10-K/A",
                         "filed": "2008-01-10"},
                    ]
                },
            },
            "Assets": {
                "label": "Assets",
                "units": {
                    "USD": [
                        # instant concept: no "start" (§5.5)
                        {"end": "2008-09-27", "val": 39572000000,
                         "accn": "0001193125-08-224958", "fy": 2008,
                         "fp": "FY", "form": "10-K", "filed": "2008-11-05",
                         "frame": "CY2008Q3I"},
                    ]
                },
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Shares Outstanding",
                "units": {
                    "shares": [
                        {"end": "2008-10-01", "val": 888325973,
                         "accn": "0001193125-08-224958", "fy": 2008,
                         "fp": "FY", "form": "10-K", "filed": "2008-11-05"},
                        # malformed point (no val) must be skipped, not crash
                        {"end": "2009-10-01", "accn": "x", "filed": "2009-11-05"},
                    ]
                },
            }
        },
    },
}


class TestParseCompanyfacts:
    def test_yields_all_valid_points(self):
        rows = list(parse_companyfacts(FIXTURE))
        assert len(rows) == 4  # 2 net income + 1 assets + 1 shares (bad skipped)

    def test_instant_concept_has_null_start(self):
        rows = list(parse_companyfacts(FIXTURE))
        assets = next(r for r in rows if r[2] == "Assets")
        assert assets[4] is None            # start_date
        assert assets[12] == "CY2008Q3I"    # frame preserved

    def test_values_are_raw_units(self):
        rows = list(parse_companyfacts(FIXTURE))
        ni = next(r for r in rows if r[2] == "NetIncomeLoss")
        assert ni[6] == 3496000000  # dollars, not millions

    def test_duplicate_period_kept_as_two_rows_for_dedup_view(self):
        # both the original and the /A restatement are stored; the SQL
        # facts_dedup view picks latest-filed — parsing must not collapse them
        rows = [r for r in parse_companyfacts(FIXTURE) if r[2] == "NetIncomeLoss"]
        assert len(rows) == 2
        assert {r[7] for r in rows} == {"0001047469-07-009340",
                                        "0001047469-08-000000"}


class TestDedupeRows:
    def test_exact_natural_key_dupes_dropped(self):
        rows = list(parse_companyfacts(FIXTURE))
        assert len(dedupe_rows(iter(rows + rows))) == len(rows)


class TestMetricRegistry:
    def test_revenue_priority_order_matches_data_dictionary(self):
        tags = [t for _, t in METRICS["revenue"]["tags"]]
        # §5.1: ASC-606 tag first, then generic, then the retired tag
        assert tags[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert "SalesRevenueNet" in tags

    def test_every_metric_declares_kind_and_unit(self):
        for name, spec in METRICS.items():
            assert spec["kind"] in ("duration", "instant"), name
            assert spec["unit"], name
            assert spec["tags"], name

    def test_instant_metrics_are_balance_sheet_concepts(self):
        # §5.5 sanity: the balance-sheet metrics must be instant
        for m in ("total_assets", "total_liabilities", "stockholders_equity",
                  "cash_and_equivalents", "shares_outstanding"):
            assert METRICS[m]["kind"] == "instant", m
