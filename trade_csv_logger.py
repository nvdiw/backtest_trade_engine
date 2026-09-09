
from excel_charts import add_report_chart
import pandas as pd
import os
from collections import Counter


class TradeCSVLogger:
    """Lightweight CSV logger.
    - In normal mode it collects rows and writes a CSV on save_csv().
    - In optimize mode (optimize=True) it becomes a no-op to avoid disk I/O
      and reduce per-trade overhead (much faster for grid search).
    """
    COLUMNS = [
        "trade_id",
        "type",
        "side",
        "outcome",
        "open_time",
        "close_time",
        "entry_price",
        "close_price",
        "tactical_balance",
        "balance_before",
        "balance_after",
        "total_assets",
        "amount",
        "profit",
        "cumulative_net_profit",
        "side_cumulative_net_profit",
        "long_cumulative_net_profit",
        "short_cumulative_net_profit",
        "profit_percent",
        "pnl_percent",
        "fee_paid",
        "leverage",
        "trade_amount_percent",
        "duration_minutes_total",
        "duration_days",
        "duration_hours",
        "duration_minutes",
        "save_money",
        "profit_percent_per_month",
        "other_open_positions_at_close",
        "reason",
    ]

    def __init__(self, optimize: bool = False, write_excel: bool = True):
        self.optimize = bool(optimize)
        self.write_excel = bool(write_excel)
        if self.optimize:
            # keep only a tiny counter to preserve minimal bookkeeping
            self._count = 0
        else:
            self.rows = []
            self._cumulative_profit = 0.0
            self._side_cumulative_profit = {"LONG": 0.0, "SHORT": 0.0}

    def log_trade(
        self,
        trade_id,
        trade_type,
        open_time,
        close_time,
        entry_price,
        close_price,
        tactical_balance,
        total_assets,
        balance_before,
        balance_after,
        margin,
        leverage,
        trade_amount_percent,
        profit,
        profit_percent,
        pnl_percent,
        fee,
        days,
        hours,
        minutes,
        save_money,
        profit_percent_per_month,
        other_open_positions_at_close,
        reason
    ):
        if self.optimize:
            # no per-trade allocations during optimization
            self._count += 1
            return

        side = "LONG" if str(trade_type).upper().startswith("LONG") else "SHORT"
        outcome = "WIN" if profit > 0 else "LOSS" if profit < 0 else "BREAKEVEN"
        self._cumulative_profit += float(profit or 0.0)
        self._side_cumulative_profit[side] += float(profit or 0.0)
        self.rows.append({
            "trade_id": trade_id,
            "type": trade_type,
            "side": side,
            "outcome": outcome,
            "open_time": open_time,
            "close_time": close_time,
            "entry_price": entry_price,
            "close_price": close_price,
            "tactical_balance": tactical_balance,
            "total_assets": total_assets,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "amount": margin,
            "leverage": leverage,
            "trade_amount_percent": trade_amount_percent,
            "profit": profit,
            "cumulative_net_profit": self._cumulative_profit,
            "side_cumulative_net_profit": self._side_cumulative_profit[side],
            "long_cumulative_net_profit": self._side_cumulative_profit["LONG"],
            "short_cumulative_net_profit": self._side_cumulative_profit["SHORT"],
            "profit_percent": profit_percent,
            "pnl_percent": pnl_percent,
            "fee_paid": fee,
            "duration_minutes_total": days * 24 * 60 + hours * 60 + minutes,
            "duration_days": days,
            "duration_hours": hours,
            "duration_minutes": minutes,
            "save_money": save_money,
            "profit_percent_per_month": profit_percent_per_month,
            "other_open_positions_at_close": bool(other_open_positions_at_close),
            "reason": reason,
        })

    @staticmethod
    def _max_consecutive_losses(values):
        current = maximum = 0
        for value in values:
            if value <= 0:
                current += 1
                maximum = max(maximum, current)
            else:
                current = 0
        return maximum

    @classmethod
    def _summary_record(cls, scope, frame, first_balance):
        profits = pd.to_numeric(frame.get("profit", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        fees = pd.to_numeric(frame.get("fee_paid", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        durations = pd.to_numeric(
            frame.get("duration_minutes_total", pd.Series(dtype=float)), errors="coerce"
        ).dropna()
        wins = profits[profits > 0]
        negative_losses = profits[profits < 0]
        gross_profit = float(wins.sum())
        gross_loss = abs(float(negative_losses.sum()))
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else 5.0 if gross_profit > 0 else 0.0
        )
        average_win = float(wins.mean()) if len(wins) else 0.0
        average_loss = float(negative_losses.mean()) if len(negative_losses) else 0.0
        payoff_ratio = (
            average_win / abs(average_loss) if average_loss
            else 5.0 if average_win else 0.0
        )
        equity = peak = float(first_balance or 0.0)
        maximum_drawdown = 0.0
        for profit in profits:
            equity += float(profit)
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = min(maximum_drawdown, (equity / peak - 1.0) * 100.0)
        trades = len(frame)
        net_profit = float(profits.sum())
        liquidations = int(frame.get("type", pd.Series(dtype=str)).astype(str).str.contains("LIQUIDATED").sum())
        return {
            "scope": scope,
            "closed_trades": trades,
            "wins": int((profits > 0).sum()),
            "losses": int((profits <= 0).sum()),
            "breakeven_trades": int((profits == 0).sum()),
            "win_rate_percent": float((profits > 0).sum() * 100.0 / trades) if trades else 0.0,
            "net_profit": net_profit,
            "return_contribution_percent": net_profit * 100.0 / first_balance if first_balance else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "expectancy": net_profit / trades if trades else 0.0,
            "average_win": average_win,
            "average_loss": average_loss,
            "payoff_ratio": payoff_ratio,
            "best_trade": float(profits.max()) if trades else 0.0,
            "worst_trade": float(profits.min()) if trades else 0.0,
            "total_fees": float(fees.sum()),
            "average_duration_minutes": float(durations.mean()) if len(durations) else 0.0,
            "maximum_drawdown_percent": maximum_drawdown,
            "max_consecutive_losses": cls._max_consecutive_losses(profits.tolist()),
            "liquidations": liquidations,
        }

    @classmethod
    def _build_summary_table(cls, trades_df, first_balance):
        side_series = trades_df.get("side", pd.Series(index=trades_df.index, dtype=str)).astype(str).str.upper()
        inferred_sides = trades_df.get(
            "type", pd.Series(index=trades_df.index, dtype=str)
        ).astype(str).str.upper().str.extract(r"^(LONG|SHORT)", expand=False)
        side_series = side_series.where(side_series.isin(("LONG", "SHORT")), inferred_sides)
        records = [cls._summary_record("TOTAL", trades_df, first_balance)]
        records.extend(
            cls._summary_record(side, trades_df[side_series == side], first_balance)
            for side in ("LONG", "SHORT")
        )
        return pd.DataFrame(records)

    def save_csv(
        self,
        first_balance,
        final_balance,
        total_profit,
        total_profit_percent,
        total_fee,
        start_time,
        end_time,
        days,
        hours,
        minutes,
        overview_metrics=None,
        file_name: str = os.path.join("outputs", "trades", "data_orders.csv"),
        monthly_report=None,
        run_metadata=None,
    ):
        if self.optimize:
            # do not write any files during optimization
            return {"rows_logged": getattr(self, "_count", 0)}

        df = pd.DataFrame(self.rows, columns=self.COLUMNS)

        trades_only = df.copy()
        side_summary = self._build_summary_table(trades_only, first_balance)
        summary_row = {
            "trade_id": None,
            "type": "SUMMARY",
            "open_time": start_time,
            "close_time": end_time,
            "entry_price": None,
            "close_price": None,
            "total_assets": final_balance,
            "balance_before": first_balance,
            "balance_after": final_balance,
            "profit": total_profit,
            "profit_percent": total_profit_percent,
            "fee_paid": total_fee,
            "duration_days": days,
            "duration_hours": hours,
            "duration_minutes": minutes
        }
        summary_row_full = {col: summary_row.get(col, None) for col in self.COLUMNS}

        if df.empty:
            df = pd.DataFrame([summary_row_full], columns=self.COLUMNS)
        else:
            summary_frame = pd.DataFrame(
                [summary_row_full], columns=self.COLUMNS, dtype=object
            )
            df = pd.concat(
                [df.astype(object), summary_frame],
                ignore_index=True,
            )
        while True:
            try:
                output_dir = os.path.dirname(file_name)
                if output_dir:
                    os.makedirs(output_dir, exist_ok=True)
                for key, value in (run_metadata or {}).items():
                    df[key] = value
                if monthly_report is not None:
                    monthly_summary, monthly_rows = monthly_report
                    for key, value in monthly_summary.items():
                        df[key] = None
                        df.loc[df['type'] == 'SUMMARY', key] = value
                    stem = os.path.splitext(file_name)[0]
                    pd.DataFrame([monthly_summary]).to_csv(stem + '_monthly_summary.csv', index=False)
                    pd.DataFrame(monthly_rows).to_csv(stem + '_monthly_targets.csv', index=False)
                df.to_csv(file_name, index=False, encoding="utf-8")
                summary_file_name = os.path.splitext(file_name)[0] + "_summary.csv"
                side_summary.to_csv(summary_file_name, index=False, encoding="utf-8")
                if self.write_excel:
                    self._save_colored_excel(
                        df, file_name, overview_metrics, side_summary=side_summary,
                        monthly_report=monthly_report,
                    )
                break
            except PermissionError:
                answer = input(f"please close: {file_name} after close write ok: ")
                if answer == "ok":
                    print("thanks!")
        return {
            "trade_csv": file_name,
            "summary_csv": os.path.splitext(file_name)[0] + "_summary.csv",
            "excel": os.path.splitext(file_name)[0] + ".xlsx" if self.write_excel else None,
        }

    def _save_colored_excel(
        self, df: pd.DataFrame, csv_file_name: str, overview_metrics=None,
        side_summary=None,
        monthly_report=None,
    ):
        """Create a polished multi-sheet workbook alongside the raw CSV."""
        if df.empty:
            return

        try:
            from openpyxl import load_workbook
            from openpyxl.chart import BarChart, LineChart, Reference
            from openpyxl.chart.label import DataLabelList
            from openpyxl.chart.marker import DataPoint
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from openpyxl.utils import get_column_letter
            from openpyxl.worksheet.table import Table, TableStyleInfo
        except Exception:
            return

        excel_file_name = os.path.splitext(csv_file_name)[0] + ".xlsx"
        summary_df = df[df["type"] == "SUMMARY"].copy()
        trades_df = df[df["type"] != "SUMMARY"].copy()
        inferred_sides = trades_df["type"].astype(str).str.upper().str.extract(
            r"^(LONG|SHORT)", expand=False
        )
        normalized_sides = trades_df["side"].astype(str).str.upper()
        trades_df["side"] = normalized_sides.where(
            normalized_sides.isin(("LONG", "SHORT")), inferred_sides
        )
        numeric_profit = pd.to_numeric(trades_df["profit"], errors="coerce").fillna(0.0)
        inferred_outcomes = numeric_profit.map(
            lambda value: "WIN" if value > 0 else "LOSS" if value < 0 else "BREAKEVEN"
        )
        trades_df["outcome"] = trades_df["outcome"].where(
            trades_df["outcome"].notna(), inferred_outcomes
        )
        trade_ids = trades_df["trade_id"].fillna("").astype(str)
        rsi_df = trades_df[trade_ids.str.startswith("rsi_ma_strategy_")].copy()
        scale_df = trades_df[trade_ids.str.startswith("scale_ma_strategy_")].copy()
        main_df = trades_df[
            ~trade_ids.str.startswith(("rsi_ma_strategy_", "scale_ma_strategy_"))
        ].copy()
        long_df = trades_df[trades_df["side"].astype(str).str.upper() == "LONG"].copy()
        short_df = trades_df[trades_df["side"].astype(str).str.upper() == "SHORT"].copy()

        analysis_frame = trades_df.copy()
        analysis_frame["month"] = pd.to_datetime(
            analysis_frame.get("close_time"), errors="coerce"
        ).dt.to_period("M").astype(str)
        analysis_frame["is_win"] = pd.to_numeric(
            analysis_frame.get("profit"), errors="coerce"
        ).fillna(0.0) > 0
        monthly_rows = []
        for month, group in analysis_frame.groupby("month", dropna=True):
            row = {"month": month}
            for scope, scoped in (
                ("total", group),
                ("long", group[group["side"].astype(str).str.upper() == "LONG"]),
                ("short", group[group["side"].astype(str).str.upper() == "SHORT"]),
            ):
                profits = pd.to_numeric(scoped.get("profit"), errors="coerce").fillna(0.0)
                row[f"{scope}_trades"] = len(scoped)
                row[f"{scope}_wins"] = int((profits > 0).sum())
                row[f"{scope}_win_rate_percent"] = (
                    float((profits > 0).sum() * 100.0 / len(scoped)) if len(scoped) else 0.0
                )
                row[f"{scope}_net_profit"] = float(profits.sum())
                row[f"{scope}_fees"] = float(pd.to_numeric(
                    scoped.get("fee_paid"), errors="coerce"
                ).fillna(0.0).sum()) if len(scoped) else 0.0
            monthly_rows.append(row)
        monthly_df = pd.DataFrame(monthly_rows)

        reason_rows = []
        for (side, reason), group in analysis_frame.groupby(
            ["side", "reason"], dropna=False
        ):
            profits = pd.to_numeric(group.get("profit"), errors="coerce").fillna(0.0)
            reason_rows.append({
                "side": side,
                "reason": reason if pd.notna(reason) else "(not specified)",
                "trades": len(group),
                "wins": int((profits > 0).sum()),
                "win_rate_percent": float((profits > 0).sum() * 100.0 / len(group)),
                "net_profit": float(profits.sum()),
                "average_profit": float(profits.mean()),
                "fees": float(pd.to_numeric(group.get("fee_paid"), errors="coerce").fillna(0.0).sum()),
            })
        reasons_df = pd.DataFrame(reason_rows)

        summary_values = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
        if overview_metrics:
            overview_rows = [
                (section, metric, value)
                for section, metrics in overview_metrics.items()
                for metric, value in metrics.items()
            ]
        else:
            overview_rows = [
                ("Run", "Start time", summary_values.get("open_time")),
                ("Run", "End time", summary_values.get("close_time")),
                ("Capital", "Starting balance", summary_values.get("balance_before")),
                ("Capital", "Final balance", summary_values.get("balance_after")),
                ("Capital", "Total profit", summary_values.get("profit")),
                ("Capital", "Total profit %", summary_values.get("profit_percent")),
                ("Capital", "Total fees", summary_values.get("fee_paid")),
                ("Trades", "Closed trades", len(trades_df)),
                ("Trades", "Main trades", len(main_df)),
                ("RSI", "RSI trades", len(rsi_df)),
                ("Scale", "Scale trades", len(scale_df)),
            ]
        overview = pd.DataFrame(overview_rows, columns=["Section", "Metric", "Value"])

        if side_summary is None:
            first_balance = summary_values.get("balance_before") or 0.0
            side_summary = self._build_summary_table(trades_df, first_balance)
        summary_by_scope = side_summary.set_index("scope")
        dashboard_metric_map = [
            ("Net profit", "net_profit"),
            ("Return contribution %", "return_contribution_percent"),
            ("Closed trades", "closed_trades"),
            ("Wins", "wins"),
            ("Losses", "losses"),
            ("Win rate %", "win_rate_percent"),
            ("Profit factor", "profit_factor"),
            ("Expectancy / trade", "expectancy"),
            ("Average win", "average_win"),
            ("Average loss", "average_loss"),
            ("Payoff ratio", "payoff_ratio"),
            ("Best trade", "best_trade"),
            ("Worst trade", "worst_trade"),
            ("Total fees", "total_fees"),
            ("Maximum drawdown %", "maximum_drawdown_percent"),
            ("Max consecutive losses", "max_consecutive_losses"),
            ("Liquidations", "liquidations"),
            ("Average duration minutes", "average_duration_minutes"),
        ]
        dashboard = pd.DataFrame([
            {
                "Metric": label,
                "TOTAL": summary_by_scope.at["TOTAL", key],
                "LONG": summary_by_scope.at["LONG", key],
                "SHORT": summary_by_scope.at["SHORT", key],
            }
            for label, key in dashboard_metric_map
        ])

        sheets = {
            "Dashboard": dashboard,
            "Side Comparison": side_summary,
            "Overview": overview,
            "Monthly Analysis": monthly_df,
            "Exit Reasons": reasons_df,
            "All Trades": trades_df,
            "Long Trades": long_df,
            "Short Trades": short_df,
            "Main Strategy": main_df,
            "RSI Strategy": rsi_df,
            "Scale Strategy": scale_df,
        }
        if monthly_report is not None:
            monthly_summary, monthly_rows = monthly_report
            sheets = {
                'Monthly Summary': pd.DataFrame(list(monthly_summary.items()), columns=['Metric', 'Value']),
                'Monthly Goals': pd.DataFrame(monthly_rows),
                **sheets,
            }
        with pd.ExcelWriter(excel_file_name, engine="openpyxl") as writer:
            for sheet_name, sheet_df in sheets.items():
                sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

        close_times = [str(x) for x in trades_df.get("close_time", [])]
        types = [str(x) for x in trades_df.get("type", [])]
        valid_close_times = [
            ct for ct, t in zip(close_times, types)
            if ct and ct.lower() != "none" and t != "SUMMARY"
        ]
        close_counts = Counter(valid_close_times)
        multi_close_times = {ct for ct, c in close_counts.items() if c >= 2}
        wb = load_workbook(excel_file_name)
        header_fill = PatternFill("solid", fgColor="17365D")
        header_font = Font(color="FFFFFF", bold=True)
        profit_fill = PatternFill("solid", fgColor="E2F0D9")
        loss_fill = PatternFill("solid", fgColor="FCE4D6")
        grouped_fill = PatternFill("solid", fgColor="DDEBF7")
        section_fills = {
            "Run": PatternFill("solid", fgColor="E2F0D9"),
            "Capital": PatternFill("solid", fgColor="D9EAF7"),
            "Performance": PatternFill("solid", fgColor="FFF2CC"),
            "Trades": PatternFill("solid", fgColor="E4DFEC"),
            "RSI": PatternFill("solid", fgColor="FCE4D6"),
            "Scale": PatternFill("solid", fgColor="DDEBF7"),
            "Long": PatternFill("solid", fgColor="E2F0D9"),
            "Short": PatternFill("solid", fgColor="FCE4D6"),
        }
        money_columns = {
            "entry_price", "close_price", "tactical_balance", "balance_before",
            "balance_after", "total_assets", "amount", "profit", "fee_paid",
            "save_money",
            "cumulative_net_profit", "side_cumulative_net_profit",
            "long_cumulative_net_profit", "short_cumulative_net_profit",
            "net_profit", "gross_profit", "gross_loss", "expectancy",
            "average_win", "average_loss", "best_trade", "worst_trade",
            "total_fees", "average_profit", "fees",
        }
        percent_columns = {
            "profit_percent", "pnl_percent", "trade_amount_percent",
            "profit_percent_per_month",
            "win_rate_percent", "return_contribution_percent",
            "maximum_drawdown_percent", "total_win_rate_percent",
            "long_win_rate_percent", "short_win_rate_percent",
        }

        for sheet_index, ws in enumerate(wb.worksheets, start=1):
            ws.freeze_panes = "A2" if "Trades" not in ws.title else "E2"
            ws.auto_filter.ref = ws.dimensions
            ws.sheet_view.showGridLines = False
            ws.row_dimensions[1].height = 24
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
            headers = {cell.value: cell.column for cell in ws[1]}
            for column_index, cells in enumerate(ws.columns, start=1):
                values = [str(cell.value or "") for cell in cells[:250]]
                width = min(55, max(11, max(map(len, values), default=10) + 2))
                ws.column_dimensions[get_column_letter(column_index)].width = width
            for name in money_columns:
                column_index = headers.get(name)
                if column_index:
                    for row_index in range(2, ws.max_row + 1):
                        ws.cell(row_index, column_index).number_format = '$#,##0.00;[Red]-$#,##0.00'
            for name in percent_columns:
                column_index = headers.get(name)
                if column_index:
                    for row_index in range(2, ws.max_row + 1):
                        ws.cell(row_index, column_index).number_format = '0.00"%";[Red]-0.00"%"'
            for name, column_index in headers.items():
                header_text = str(name or "").lower()
                if header_text.endswith("_profit") or header_text.endswith("_fees"):
                    for row_index in range(2, ws.max_row + 1):
                        ws.cell(row_index, column_index).number_format = '$#,##0.00;[Red]-$#,##0.00'
                elif "win_rate_percent" in header_text or "drawdown_percent" in header_text:
                    for row_index in range(2, ws.max_row + 1):
                        ws.cell(row_index, column_index).number_format = '0.00"%";[Red]-0.00"%"'
            if ws.title == "Overview":
                section_column = headers.get("Section")
                metric_column = headers.get("Metric")
                value_column = headers.get("Value")
                for row_index in range(2, ws.max_row + 1):
                    section = str(ws.cell(row_index, section_column).value or "")
                    metric = str(ws.cell(row_index, metric_column).value or "")
                    ws.cell(row_index, section_column).font = Font(bold=True, color="1F1F1F")
                    ws.cell(row_index, section_column).fill = section_fills.get(
                        section, PatternFill("solid", fgColor="F2F2F2")
                    )
                    value_cell = ws.cell(row_index, value_column)
                    metric_lower = metric.lower()
                    if any(token in metric_lower for token in (
                        "balance", "profit", "fees", "saved money",
                    )) and "%" not in metric:
                        value_cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                    elif "%" in metric or "win rate" in metric_lower or "drawdown" in metric_lower:
                        value_cell.number_format = '0.00"%";[Red]-0.00"%"'
                    elif any(token in metric_lower for token in (
                        "trades", "wins", "losses", "positions", "liquidations", "months",
                    )):
                        value_cell.number_format = '#,##0'
            if ws.max_row >= 2 and all(cell.value is not None for cell in ws[1]):
                table = Table(
                    displayName=f"TradeReportTable{sheet_index}", ref=ws.dimensions
                )
                table.tableStyleInfo = TableStyleInfo(
                    name="TableStyleMedium2", showRowStripes=True,
                    showFirstColumn=False, showLastColumn=False,
                    showColumnStripes=False,
                )
                ws.add_table(table)

            profit_column = headers.get("profit")
            close_time_column = headers.get("close_time")
            if profit_column:
                for row_index in range(2, ws.max_row + 1):
                    profit = ws.cell(row_index, profit_column).value
                    row_fill = profit_fill if isinstance(profit, (int, float)) and profit >= 0 else loss_fill
                    if close_time_column:
                        close_time = str(ws.cell(row_index, close_time_column).value)
                        if close_time in multi_close_times:
                            row_fill = grouped_fill
                    for column_index in range(1, ws.max_column + 1):
                        ws.cell(row_index, column_index).fill = row_fill

            if ws.title == "Dashboard":
                ws.sheet_properties.tabColor = "4472C4"
                ws.column_dimensions["A"].width = 32
                for column in ("B", "C", "D"):
                    ws.column_dimensions[column].width = 18
                for row_index in range(2, ws.max_row + 1):
                    ws.cell(row_index, 1).font = Font(bold=True, color="17365D")
                    ws.cell(row_index, 3).fill = PatternFill("solid", fgColor="E2F0D9")
                    ws.cell(row_index, 4).fill = PatternFill("solid", fgColor="FCE4D6")
                for row_index in range(2, ws.max_row + 1):
                    metric = str(ws.cell(row_index, 1).value or "").lower()
                    if any(token in metric for token in (
                        "profit", "expectancy", "average win", "average loss",
                        "best trade", "worst trade", "fees",
                    )) and "%" not in metric and "factor" not in metric:
                        for column_index in (2, 3, 4):
                            ws.cell(row_index, column_index).number_format = '$#,##0.00;[Red]-$#,##0.00'
                    elif "%" in metric or "drawdown" in metric:
                        for column_index in (2, 3, 4):
                            ws.cell(row_index, column_index).number_format = '0.00"%";[Red]-0.00"%"'
            elif ws.title == "Side Comparison":
                ws.sheet_properties.tabColor = "70AD47"
                scope_column = headers.get("scope")
                if scope_column:
                    fills = {
                        "TOTAL": PatternFill("solid", fgColor="D9EAF7"),
                        "LONG": PatternFill("solid", fgColor="E2F0D9"),
                        "SHORT": PatternFill("solid", fgColor="FCE4D6"),
                    }
                    for row_index in range(2, ws.max_row + 1):
                        fill = fills.get(str(ws.cell(row_index, scope_column).value))
                        if fill:
                            for column_index in range(1, ws.max_column + 1):
                                ws.cell(row_index, column_index).fill = fill
            elif ws.title == "Long Trades":
                ws.sheet_properties.tabColor = "70AD47"
            elif ws.title == "Short Trades":
                ws.sheet_properties.tabColor = "C0504D"

        dashboard_ws = wb["Dashboard"]
        side_ws = wb["Side Comparison"]
        side_headers = {cell.value: cell.column for cell in side_ws[1]}
        scope_column = side_headers.get("scope")

        chart_panel_border = Side(style="thin", color="D9E1F2")

        def add_chart_detail_table(start_row, start_column, title, headers, rows):
            end_column = start_column + len(headers) - 1
            dashboard_ws.merge_cells(
                start_row=start_row, start_column=start_column,
                end_row=start_row, end_column=end_column,
            )
            title_cell = dashboard_ws.cell(start_row, start_column, title)
            title_cell.fill = PatternFill("solid", fgColor="17365D")
            title_cell.font = Font(color="FFFFFF", bold=True)
            title_cell.alignment = Alignment(horizontal="left", vertical="center")
            for offset, header in enumerate(headers):
                cell = dashboard_ws.cell(start_row + 1, start_column + offset, header)
                cell.fill = PatternFill("solid", fgColor="D9EAF7")
                cell.font = Font(color="17365D", bold=True)
                cell.alignment = Alignment(horizontal="center")
            for row_offset, row_values in enumerate(rows, start=2):
                for column_offset, value in enumerate(row_values):
                    cell = dashboard_ws.cell(
                        start_row + row_offset, start_column + column_offset, value
                    )
                    cell.border = Border(bottom=chart_panel_border)
                    cell.alignment = Alignment(
                        horizontal="right" if isinstance(value, (int, float)) else "left"
                    )
                    header = headers[column_offset]
                    if "Profit" in header or "Ending" in header:
                        cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                    elif "Win rate" in header:
                        cell.number_format = '0.00"%";[Red]-0.00"%"'
                    scope = str(row_values[0]).upper() if row_values else ""
                    if scope == "LONG":
                        cell.fill = PatternFill("solid", fgColor="E2F0D9")
                    elif scope == "SHORT":
                        cell.fill = PatternFill("solid", fgColor="FCE4D6")
                    elif scope == "TOTAL":
                        cell.fill = PatternFill("solid", fgColor="D9EAF7")
            for offset, header in enumerate(headers):
                letter = get_column_letter(start_column + offset)
                dashboard_ws.column_dimensions[letter].width = max(12, len(header) + 3)

        if scope_column:
            side_exact_rows = []
            for row_index in range(2, min(side_ws.max_row, 4) + 1):
                side_exact_rows.append({
                    "scope": side_ws.cell(row_index, scope_column).value,
                    "net_profit": side_ws.cell(
                        row_index, side_headers.get("net_profit")
                    ).value if side_headers.get("net_profit") else None,
                    "win_rate": side_ws.cell(
                        row_index, side_headers.get("win_rate_percent")
                    ).value if side_headers.get("win_rate_percent") else None,
                })
            add_chart_detail_table(
                2, 14, "Exact values - net profit",
                ["Scope", "Net Profit"],
                [[row["scope"], row["net_profit"]] for row in side_exact_rows],
            )
            add_chart_detail_table(
                18, 14, "Exact values - win rate",
                ["Scope", "Win rate %"],
                [[row["scope"], row["win_rate"]] for row in side_exact_rows],
            )

        for metric, title, anchor in (
            ("net_profit", "Long vs Short net profit", "F2"),
            ("win_rate_percent", "Long vs Short win rate (%)", "F18"),
        ):
            metric_column = side_headers.get(metric)
            if scope_column and metric_column:
                chart = BarChart()
                chart.type = "col"
                chart.style = 10
                chart.title = title
                chart.height = 7
                chart.width = 12
                chart.add_data(
                    Reference(side_ws, min_col=metric_column, min_row=1, max_row=4),
                    titles_from_data=True,
                )
                chart.set_categories(
                    Reference(side_ws, min_col=scope_column, min_row=2, max_row=4)
                )
                chart.legend = None
                chart.y_axis.numFmt = (
                    '$#,##0' if metric == "net_profit" else '0.00"%"'
                )
                chart.dLbls = DataLabelList()
                chart.dLbls.showVal = True
                chart.dLbls.numFmt = chart.y_axis.numFmt
                chart.series[0].dPt = []
                for point_index, color in enumerate(("4472C4", "70AD47", "C0504D")):
                    point = DataPoint(idx=point_index)
                    point.graphicalProperties.solidFill = color
                    point.graphicalProperties.line.solidFill = color
                    chart.series[0].dPt.append(point)
                add_report_chart(wb, chart)

        if not monthly_df.empty:
            monthly_ws = wb["Monthly Analysis"]
            monthly_headers = {cell.value: cell.column for cell in monthly_ws[1]}
            month_column = monthly_headers.get("month")
            profit_columns = [
                monthly_headers.get(name)
                for name in ("total_net_profit", "long_net_profit", "short_net_profit")
            ]
            profit_columns = [column for column in profit_columns if column]
            if month_column and profit_columns:
                chart = BarChart()
                chart.type = "col"
                chart.style = 10
                chart.title = "Monthly net profit: Total vs Long vs Short"
                chart.y_axis.title = "Net profit"
                chart.height = 8
                chart.width = 18
                for column in profit_columns:
                    chart.add_data(
                        Reference(
                            monthly_ws, min_col=column, min_row=1,
                            max_row=monthly_ws.max_row,
                        ),
                        titles_from_data=True,
                    )
                chart.y_axis.numFmt = '$#,##0'
                for series, color in zip(
                    chart.series, ("4472C4", "70AD47", "C0504D")
                ):
                    series.graphicalProperties.solidFill = color
                    series.graphicalProperties.line.solidFill = color
                chart.set_categories(
                    Reference(
                        monthly_ws, min_col=month_column, min_row=2,
                        max_row=monthly_ws.max_row,
                    )
                )
                if monthly_ws.max_row <= 13:
                    chart.dLbls = DataLabelList()
                    chart.dLbls.showVal = True
                    chart.dLbls.numFmt = '$#,##0'
                add_report_chart(wb, chart)

                monthly_exact_rows = []
                first_row = max(2, monthly_ws.max_row - 11)
                for row_index in range(first_row, monthly_ws.max_row + 1):
                    monthly_exact_rows.append([
                        monthly_ws.cell(row_index, month_column).value,
                        monthly_ws.cell(row_index, monthly_headers.get("total_net_profit")).value,
                        monthly_ws.cell(row_index, monthly_headers.get("long_net_profit")).value,
                        monthly_ws.cell(row_index, monthly_headers.get("short_net_profit")).value,
                    ])
                add_chart_detail_table(
                    2, 38, "Latest 12 months - exact net profit",
                    ["Month", "Total Profit", "Long Profit", "Short Profit"],
                    monthly_exact_rows,
                )

        all_ws = wb["All Trades"]
        all_headers = {cell.value: cell.column for cell in all_ws[1]}
        if all_ws.max_row >= 2:
            cumulative_columns = [
                all_headers.get(name) for name in (
                    "cumulative_net_profit", "long_cumulative_net_profit",
                    "short_cumulative_net_profit",
                )
            ]
            cumulative_columns = [column for column in cumulative_columns if column]
            close_column = all_headers.get("close_time")
            if cumulative_columns and close_column:
                chart = LineChart()
                chart.style = 13
                chart.title = "Cumulative net profit by direction"
                chart.y_axis.title = "Net profit"
                chart.height = 8
                chart.width = 18
                for column in cumulative_columns:
                    chart.add_data(
                        Reference(all_ws, min_col=column, min_row=1, max_row=all_ws.max_row),
                        titles_from_data=True,
                    )
                chart.y_axis.numFmt = '$#,##0'
                for series, color in zip(
                    chart.series, ("4472C4", "70AD47", "C0504D")
                ):
                    series.graphicalProperties.line.solidFill = color
                    series.graphicalProperties.line.width = 26000
                chart.set_categories(
                    Reference(all_ws, min_col=close_column, min_row=2, max_row=all_ws.max_row)
                )
                add_report_chart(wb, chart)

                ending_rows = []
                for label, column_name in (
                    ("TOTAL", "cumulative_net_profit"),
                    ("LONG", "long_cumulative_net_profit"),
                    ("SHORT", "short_cumulative_net_profit"),
                ):
                    column = all_headers.get(column_name)
                    if column:
                        ending_rows.append([
                            label, all_ws.cell(all_ws.max_row, column).value
                        ])
                add_chart_detail_table(
                    20, 38, "Ending cumulative profit - exact values",
                    ["Scope", "Ending Profit"], ending_rows,
                )

        wb.save(excel_file_name)
