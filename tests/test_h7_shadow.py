import unittest
from h7_shadow import parse_time, validate_signal


class ShadowValidationTests(unittest.TestCase):
    def setUp(self):
        self.signal = {
            "signal_id": "test-001",
            "created_at": "2099-01-01T00:00:00Z",
            "product_id": "BTC-USD",
            "side": "buy",
            "strategy_version": "H7-test",
            "parameter_hash": "frozen-hash",
            "stop_pct": 0.01,
            "target_pct": 0.02,
            "notional_quote": 20,
            "hold_minutes": 60,
        }

    def test_reject_future_timestamp(self):
        self.assertEqual(validate_signal(self.signal, 300),
                         "signal timestamp is in the future")

    def test_reject_unknown_side(self):
        self.signal["created_at"] = "2020-01-01T00:00:00Z"
        self.signal["side"] = "hold"
        self.assertEqual(validate_signal(self.signal, 300),
                         "side must be buy or sell")

    def test_reject_invalid_product(self):
        self.signal["created_at"] = "2020-01-01T00:00:00Z"
        self.signal["product_id"] = "BTC/USD"
        self.assertEqual(validate_signal(self.signal, 300),
                         "product_id must look like BTC-USD")

    def test_reject_fraction_over_one(self):
        self.signal["created_at"] = "2020-01-01T00:00:00Z"
        self.signal["stop_pct"] = 1
        self.assertEqual(validate_signal(self.signal, 300),
                         "stop_pct and target_pct must be fractions between 0 and 1")

    def test_parse_time_requires_timezone(self):
        with self.assertRaises(ValueError):
            parse_time("2026-10-13T12:00:00")


if __name__ == "__main__":
    unittest.main()
