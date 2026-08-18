import tempfile
import unittest
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np

from chart_renderer import nearest_marker_in_pixels, render_backtest_chart


class ChartRendererTests(unittest.TestCase):
    def test_marker_hover_uses_rendered_sequential_coordinates(self):
        points = [
            {"x": 3.0, "y": 104.5, "text": "LONG OPEN"},
            {"x": 8.0, "y": 98.0, "text": "SHORT CLOSE"},
        ]

        hit = nearest_marker_in_pixels(lambda point: point, points, 3.5, 105.0)
        miss = nearest_marker_in_pixels(lambda point: point, points, 50.0, 50.0)

        self.assertEqual(hit["text"], "LONG OPEN")
        self.assertIsNone(miss)

    def test_headless_chart_export(self):
        count = 24
        close = np.linspace(100.0, 112.0, count)
        open_ = close - 0.25
        high = close + 1.0
        low = open_ - 1.0
        times = [f"2026-01-01 {hour:02d}:00:00" for hour in range(count)]
        chart_data = [[i, 1000 + i, 1000 + i * 1.1] for i in range(count)]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "chart.png"
            render_backtest_chart(
                chart_data=chart_data,
                close_prices=close,
                close_times=times,
                open_times=times,
                open_prices=open_,
                high_prices=high,
                low_prices=low,
                ema_16=close,
                ma_50=close - 1,
                ma_100=close - 2,
                ma_200=close - 3,
                rsi_values=np.linspace(30, 70, count),
                long_open_points=[(3, close[3])],
                long_close_points=[(8, close[8])],
                short_open_points=[],
                short_close_points=[],
                penalty_long_points=[],
                penalty_short_points=[],
                long_open_reasons={3: "entry"},
                long_close_reasons={8: "exit"},
                short_open_reasons={},
                short_close_reasons={},
                penalty_long_reasons={},
                penalty_short_reasons={},
                plot_end_offset=0,
                plot_max_candles=count,
                plot_step_candles=5,
                plot_min_zoom_candles=5,
                plot_max_render_candles=12,
                plot_zoom_in_factor=0.8,
                plot_zoom_out_factor=1.6,
                plot_window_width_scale=0.8,
                plot_window_height_scale=0.8,
                plot_drag_preview_factor=0.2,
                plot_drag_update_interval_ms=75,
                plot_yscale_drag_sensitivity=0.003,
                balance=1024,
                profits_lst=[24],
                t_profit_percent=2.4,
                count_closed_orders=1,
                total_wins=1,
                total_losses=0,
                max_drawdown=-1.0,
                lst_profit_percent_per_month=[2.4],
                chart_show=False,
                chart_save_path=output,
            )

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1_000)


if __name__ == "__main__":
    unittest.main()
