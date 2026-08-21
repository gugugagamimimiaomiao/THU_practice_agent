"""口语里的时间说法要解析到该有的精度。

2026-08-21 实测发现的：_month_span 只认「八月」「8月」，一律返回整月。
后果是用户给的精确区间被撑大——

    「9月10号到9月20号有空」 → 2026-09-01 ~ 2026-09-30
    「9月上旬有空」          → 2026-09-01 ~ 2026-09-30

于是"实践日期与可用时间冲突"这条硬条件几乎筛不掉东西：一个月的窗口
跟什么都不冲突。表面上时间过滤在工作，实际上没有。
"""
import calendar
import unittest
from datetime import date

from chat_adapter import _month_span


class TimeSpanTests(unittest.TestCase):
    def setUp(self):
        self.today = date.today()

    def _year_for(self, month: int, day: int = 1) -> int:
        """说到的月日今年已经过完的话，指的是明年。"""
        last = calendar.monthrange(self.today.year, month)[1]
        target = date(self.today.year, month, min(day, last))
        return self.today.year if target >= self.today else self.today.year + 1

    def test_ten_day_spans(self):
        year = self._year_for(9)
        for text, expected in [
            ("9月上旬有空", (f"{year}-09-01", f"{year}-09-10")),
            ("9月中旬有空", (f"{year}-09-11", f"{year}-09-20")),
            ("9月下旬有空", (f"{year}-09-21", f"{year}-09-30")),
        ]:
            with self.subTest(text=text):
                self.assertEqual(_month_span(text), expected)

    def test_explicit_range_is_not_widened_to_the_whole_month(self):
        year = self._year_for(9, 10)
        for text in ["9月10号到9月20号有空", "9月10日至20日", "9月10日-9月20日"]:
            with self.subTest(text=text):
                self.assertEqual(_month_span(text), (f"{year}-09-10", f"{year}-09-20"))

    def test_range_across_two_months(self):
        year = self._year_for(9, 28)
        self.assertEqual(_month_span("9月28日到10月5日"), (f"{year}-09-28", f"{year}-10-05"))

    def test_range_across_the_new_year(self):
        start, end = _month_span("12月28号到1月5号")
        self.assertEqual(int(end[:4]), int(start[:4]) + 1, f"{start} → {end} 没有跨年")

    def test_single_day(self):
        year = self._year_for(9, 10)
        self.assertEqual(_month_span("9月10号那天"), (f"{year}-09-10", f"{year}-09-10"))

    def test_whole_month_still_works(self):
        year = self._year_for(9)
        self.assertEqual(_month_span("九月有空"), (f"{year}-09-01", f"{year}-09-30"))

    def test_short_february_clamps_to_the_real_last_day(self):
        start, end = _month_span("2月下旬")
        year, month = int(end[:4]), 2
        self.assertEqual(int(end[8:]), calendar.monthrange(year, month)[1])

    def test_no_time_mentioned(self):
        self.assertIsNone(_month_span("想找京津冀的实践"))

    def test_precision_actually_narrows_the_window(self):
        """精确区间必须比整月窄——这条用例存在的意义就是防止再退化回整月。"""
        exact = _month_span("9月10号到9月12号")
        whole = _month_span("九月有空")
        self.assertLess(
            (date.fromisoformat(exact[1]) - date.fromisoformat(exact[0])).days,
            (date.fromisoformat(whole[1]) - date.fromisoformat(whole[0])).days,
        )


if __name__ == "__main__":
    unittest.main()
