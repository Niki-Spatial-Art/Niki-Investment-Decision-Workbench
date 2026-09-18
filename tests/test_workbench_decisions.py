import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.decision_state import BEIJING, account_check, local_decision_state, public_market_gate, route_check
from tools.signal_diagnostics import signal_backtest
from tools.notification_state import notification_decision
from tools.a_stock_radar_snapshot import load_workbench_codes
from tools.import_review_evidence import prepare_summary


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=BEIJING)


def scan(count=5000):
    return {"scanned_count": count, "min_rows_target": 5000,
            "breadth": {"advancers": 3780, "decliners": 675, "flat": 144}}


def bars(count=100):
    start = datetime(2025, 1, 1)
    return [{"date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
             "open": 100 * 1.01 ** i, "close": 100 * 1.01 ** i,
             "high": 101 * 1.01 ** i, "low": 99 * 1.01 ** i, "volume": 1000} for i in range(count)]


class DecisionChecks(unittest.TestCase):
    def setUp(self):
        self.snap = {"snapshot_time": NOW.isoformat(), "total_assets": 10000, "available_cash": 6000,
                     "securities_market_value": 4000, "fills_verified": True,
                     "positions_visible": [{"code": "510300", "shares": 1000, "available": 1000}]}
        self.route = {"generated_at": NOW.isoformat(), "requested_codes": ["510300"],
                      "quotes": {"510300": {"price": 4, "quote_time": NOW.strftime("%Y%m%d%H%M%S")}}}
        self.report = {"generated_at": NOW.isoformat(), "action_stack": {"new_entry_gate": {"blocked": False}}}

    def test_complete_is_review_only(self):
        state = local_decision_state(self.report, self.snap, self.route, 1, NOW)
        self.assertTrue(state["review_allowed"])
        self.assertTrue(state["holdings_review_allowed"])

    def test_missing_gate_is_not_permission(self):
        self.assertFalse(local_decision_state({}, self.snap, self.route, 1, NOW)["review_allowed"])

    def test_real_radar_metadata_time_and_age(self):
        self.report["metadata"] = {"generated_at": self.report.pop("generated_at")}
        self.assertTrue(local_decision_state(self.report, self.snap, self.route, 1, NOW)["review_allowed"])
        self.report["metadata"]["generated_at"] = (NOW - timedelta(hours=2)).isoformat()
        self.assertFalse(local_decision_state(self.report, self.snap, self.route, 1, NOW)["review_allowed"])

    def test_missing_candidate_blocks(self):
        self.assertFalse(local_decision_state(self.report, self.snap, self.route, 0, NOW)["review_allowed"])

    def test_account_requires_reconciliation(self):
        self.snap["fills_verified"] = False
        self.assertFalse(account_check(self.snap, NOW)["ok"])

    def test_mixed_time_and_unknown_cash(self):
        self.snap["cash_snapshot_time"] = "2026-09-18 09:00:00"
        self.snap["available_cash"] = None
        self.assertFalse(account_check(self.snap, NOW)["ok"])

    def test_zero_cash_is_valid(self):
        self.snap.update(available_cash=0, securities_market_value=10000)
        self.assertTrue(account_check(self.snap, NOW)["ok"])

    def test_future_account_not_fresh(self):
        self.snap["snapshot_time"] = (NOW + timedelta(hours=1)).isoformat()
        self.assertFalse(account_check(self.snap, NOW)["ok"])

    def test_unknown_available_not_zero(self):
        self.snap["positions_visible"][0]["available"] = None
        self.assertFalse(account_check(self.snap, NOW)["ok"])

    def test_stale_quote_not_revived_by_fresh_file(self):
        self.route["quotes"]["510300"]["quote_time"] = "20260917150000"
        self.assertFalse(route_check(self.route, ["510300"], NOW)["ok"])

    def test_missing_held_code(self):
        self.assertFalse(route_check(self.route, ["518880"], NOW)["ok"])

    def test_nan_price(self):
        self.route["quotes"]["510300"]["price"] = float("nan")
        self.assertFalse(route_check(self.route, ["510300"], NOW)["ok"])

    def test_profiles_keep_different_trend_criteria(self):
        for profile in ("intraday_two_of_three_ma20", "postclose_benchmark_ma20"):
            self.assertTrue(public_market_gate(scan(), True, True, profile)["open"])
            self.assertFalse(public_market_gate(scan(), False, True, profile)["open"])

    def test_partial_scan_and_unclassified_rows(self):
        result = public_market_gate(scan(4700), True, True, "intraday")
        self.assertFalse(result["open"])
        self.assertEqual(result["data"]["unclassified"], 101)
        self.assertTrue(result["market"]["ok"])

    def test_all_failure_reasons_preserved(self):
        result = public_market_gate(scan(4700), False, False, "intraday")
        self.assertEqual(len(result["reasons"]), 3)


class DiagnosticChecks(unittest.TestCase):
    def test_zero_drawdown_and_t_plus_one_nonoverlap(self):
        result = signal_backtest(bars(), {})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["horizons"]["5"]["max_drawdown"], 0)
        for row in result["horizons"].values():
            for trade in row["trades"]:
                self.assertLess(trade["signal_date"], trade["entry_date"])
                self.assertLess(trade["entry_date"], trade["exit_date"])
            for prior, current in zip(row["trades"], row["trades"][1:]):
                self.assertLess(prior["exit_date"], current["entry_date"])

    def test_next_open_gap_is_paid(self):
        data = bars()
        normal = signal_backtest(data, {})
        for row in data:
            row["open"] *= 1.1
        gapped = signal_backtest(data, {})
        self.assertLess(gapped["horizons"]["1"]["net_return"], normal["horizons"]["1"]["net_return"])
        self.assertFalse(gapped["eligible"])

    def test_invalid_and_short_history(self):
        for field, value in (("open", None), ("close", float("nan")), ("volume", -1)):
            data = bars()
            data[30][field] = value
            self.assertFalse(signal_backtest(data, {})["eligible"])
        self.assertEqual(signal_backtest(bars(20), {})["horizons"], {})

    def test_unsorted_dates_rejected(self):
        self.assertFalse(signal_backtest(list(reversed(bars())), {})["eligible"])

    def test_suspended_and_locked_bars_not_filled(self):
        data = bars()
        for row in data:
            row["high"] = row["low"] = row["close"]
        self.assertEqual(signal_backtest(data, {})["horizons"]["1"]["observations"], 0)


class NotificationChecks(unittest.TestCase):
    def test_duplicate_and_failed_delivery_retry(self):
        summary = {"gate_open": True, "actionable_candidates": [{"code": "002463"}]}
        send, _, state = notification_decision(summary, {}, "2026-09-18")
        self.assertTrue(send)
        self.assertTrue(notification_decision(summary, state, "2026-09-18")[0])
        state["smtp_accepted"] = True
        self.assertFalse(notification_decision(summary, state, "2026-09-18")[0])
        self.assertTrue(notification_decision(summary, state, "2026-09-21")[0])

    def test_risk_withdrawal_not_silenced(self):
        summary = {"gate_open": False}
        self.assertTrue(notification_decision(summary, {"day": "2026-09-18", "gate_open": True}, "2026-09-18")[0])
        self.assertFalse(notification_decision(summary, {}, "2026-09-18")[0])


class IntegrationChecks(unittest.TestCase):
    def test_sync_blocks_missing_dependency_not_unrelated_scratch(self):
        from tools.check_tracked_imports import missing_tracked_dependencies
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "caller.py").write_text("from helper import run\n", encoding="utf-8")
            (root / "helper.py").write_text("def run(): pass\n", encoding="utf-8")
            (root / "scratch.py").write_text("pass\n", encoding="utf-8")
            self.assertTrue(missing_tracked_dependencies(root, {"caller.py"}))
            self.assertFalse(missing_tracked_dependencies(root, {"caller.py", "helper.py"}))

    def test_unknown_available_does_not_invent_t_plus_one(self):
        from tools.local_dashboard import classify_holding_action
        result = classify_holding_action({"code": "518880", "shares": 100, "available": None})
        self.assertEqual(result[0], "核对")

    def test_missing_day_change_not_counted_as_flat(self):
        from tools.send_workbench_email_preview import index_stop_state, CORE_INDEX_CODES
        quotes = {code: {"price": 4, "ma20": 3} for code in CORE_INDEX_CODES}
        self.assertEqual(index_stop_state({"quotes": quotes}), (False, 0))

    def test_missing_portfolio_refreshes_benchmarks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(load_workbench_codes(root, root / "missing.json"), ["510300", "512100", "588000"])

    def test_holdings_and_only_three_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data/broker_account_snapshots.local.json").write_text(json.dumps({"snapshots": [{"snapshot_time": "2026-09-18", "positions_visible": [{"code": "512800", "shares": 10}]}]}))
            (root / "data/research_evidence.local.json").write_text(json.dumps({"cards": [{"code": x} for x in ("002463", "600183", "002916", "300476")]}))
            codes = load_workbench_codes(root, root / "missing.json")
            self.assertIn("512800", codes)
            self.assertNotIn("300476", codes)

    def test_incomplete_reconciliation_not_imported(self):
        with self.assertRaises(ValueError):
            prepare_summary({"checks": {}}, "hash")

    def test_clean_bars_preserves_execution_prices(self):
        from tools.theme_watch_report import clean_bars
        source = bars(1)[0]
        self.assertEqual(clean_bars({"history": {"x": {"bars": [source]}}}, "x")[0]["open"], source["open"])

    def test_postclose_passes_gate_into_modules(self):
        from tools import whole_market_watch_report as module
        seen = []
        def metrics(config, payload, *args):
            seen.append(payload.get("market_gate_ok"))
            return {"backtest_candidates": [{"code": "002463"}] if payload.get("market_gate_ok") else [], "a_share": []}
        config = {"deep_research_modules": [{"id": "test"}]}
        with patch.object(module, "run_broad_market_scan", return_value=scan()), patch.object(module, "load_digital_infra_watchlist", return_value={}), patch.object(module, "snapshot", return_value={}), patch.object(module, "benchmark_state", return_value={"above_ma20": True}), patch.object(module, "trading_day", return_value=True), patch.object(module, "module_metrics", side_effect=metrics):
            result = module.build_report(config)
        self.assertEqual(seen, [True])
        self.assertEqual(len(result["final_candidates"]), 1)

    def test_partial_postclose_does_not_release_candidates(self):
        from tools import whole_market_watch_report as module
        with patch.object(module, "run_broad_market_scan", return_value=scan(4700)), patch.object(module, "load_digital_infra_watchlist", return_value={}), patch.object(module, "snapshot", return_value={}), patch.object(module, "benchmark_state", return_value={"above_ma20": True}), patch.object(module, "trading_day", return_value=True):
            result = module.build_report({})
        self.assertFalse(result["market_gate"]["decision"]["open"])
        self.assertEqual(result["final_candidates"], [])


if __name__ == "__main__":
    unittest.main()
