import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
from band_check import parse_styles_table  # noqa: E402

TABLE = """| style | fleets | median | p10 | p90 | mean | out | ships (mean; count: fleets) |
|---|---|---|---|---|---|---|---|
| privateer | 2 | +157,000 | +147,367 | +166,632 | +157,000 | 0 | 1.00 (1: 2) |
| stock_trader | 2 | +130,199 | +116,532 | +143,866 | +130,199 | 0 | 1.00 (1: 2) |
| maker | 2 | -1,847 | -8,779 | +2,915 | +100,847 | 0 | 1.00 (1: 2) |
"""


class TestBandCheckParse(unittest.TestCase):
    def test_parses_medians_and_signs(self):
        got = parse_styles_table(TABLE)
        self.assertEqual(got['privateer'], (157000, 147367, 166632))
        self.assertEqual(got['stock_trader'][0], 130199)
        self.assertEqual(got['maker'], (-1847, -8779, 2915))
        self.assertNotIn('style', got)

    def test_empty_output(self):
        self.assertEqual(parse_styles_table('no table here'), {})


if __name__ == '__main__':
    unittest.main()
