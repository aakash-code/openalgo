#!/usr/bin/env python3
"""
NIFTY Options Selling - Professional Trade Journal Report
===========================================================
Generates a comprehensive HTML report + ZIP package with:
  1. Executive Summary & Performance Dashboard
  2. Month-wise trade journal (entry, exit, strikes, P&L)
  3. Day-wise trade log with all details
  4. NIFTY candlestick chart with trade entry/exit markers
  5. Individual option charts per trade showing leg entry/exit
  6. Drawdown chart, equity curve, monthly heatmap
  7. All data exported as Excel + CSVs

Output: ZIP file ready for client/bank presentation
"""

import sys
import os
import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
import zipfile
import json
import shutil

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")
INITIAL_CAPITAL = 400_000

# Which config to generate report for
REPORT_CONFIG = "Best_Sharpe"  # Best_P&L, Best_Sharpe, Best_15m_Sharpe, Conservative


def load_trade_data(config_name):
    csv_path = _script_dir / f"intraday_trades_{config_name}.csv"
    if not csv_path.exists():
        print(f"ERROR: Trade log not found: {csv_path}")
        sys.exit(1)
    df = pd.read_csv(csv_path, parse_dates=['entry_ts', 'exit_ts'])
    return df


def load_nifty_1m(conn, start_date, end_date):
    start_ts = int(datetime.combine(start_date, datetime.min.time()).timestamp())
    end_ts = int(datetime.combine(end_date, datetime.max.time()).timestamp())
    df = conn.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp
    """, [start_ts, end_ts]).df()
    df['dt'] = (pd.to_datetime(df['timestamp'], unit='s', utc=True)
                  .dt.tz_convert('Asia/Kolkata').dt.tz_localize(None))
    df = df.set_index('dt').drop(columns=['timestamp'])
    return df


def load_option_1m(conn, symbol, start_date, end_date):
    start_ts = int(datetime.combine(start_date - timedelta(days=2), datetime.min.time()).timestamp())
    end_ts = int(datetime.combine(end_date + timedelta(days=2), datetime.max.time()).timestamp())
    df = conn.execute("""
        SELECT timestamp, open, high, low, close
        FROM market_data
        WHERE symbol=? AND exchange='NFO' AND interval='1m'
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp
    """, [symbol, start_ts, end_ts]).df()
    if df.empty:
        return df
    df['dt'] = (pd.to_datetime(df['timestamp'], unit='s', utc=True)
                  .dt.tz_convert('Asia/Kolkata').dt.tz_localize(None))
    df = df.set_index('dt').drop(columns=['timestamp'])
    return df


def resample_nifty(df1m, tf='10min'):
    tf_map = {'3min': '15:29', '5min': '15:24', '10min': '15:19', '15min': '15:14'}
    upper = tf_map.get(tf, '15:19')
    df_tf = (df1m.between_time('09:15', '15:29')
                 .resample(tf, closed='left', label='left')
                 .agg(open=('open','first'), high=('high','max'),
                      low=('low','min'), close=('close','last'),
                      volume=('volume','sum'))
                 .dropna().between_time('09:15', upper))
    return df_tf


# ============================================================================
# CHART GENERATORS
# ============================================================================

def make_equity_drawdown_chart(df_t):
    """Full equity curve + drawdown chart."""
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        subplot_titles=["Portfolio Equity Curve", "Drawdown (%)"],
        row_heights=[0.65, 0.35], vertical_spacing=0.08)

    # Equity
    fig.add_trace(go.Scatter(
        x=df_t['exit_ts'], y=df_t['equity'],
        mode='lines', name='Equity',
        line=dict(color='#00d4aa', width=2.5),
        fill='tozeroy', fillcolor='rgba(0,212,170,0.1)'), row=1, col=1)

    fig.add_hline(y=INITIAL_CAPITAL, line_dash='dash', line_color='#888',
                  annotation_text=f'Capital: ₹{INITIAL_CAPITAL:,}', row=1, col=1)

    # Peak equity
    peak = df_t['equity'].cummax()
    fig.add_trace(go.Scatter(
        x=df_t['exit_ts'], y=peak,
        mode='lines', name='Peak Equity',
        line=dict(color='#ffd93d', width=1, dash='dot')), row=1, col=1)

    # Drawdown
    dd = (df_t['equity'] - peak) / peak * 100
    colors = ['#ff4444' if d < -10 else '#ff8800' if d < -5 else '#44aa44' for d in dd]
    fig.add_trace(go.Bar(
        x=df_t['exit_ts'], y=dd, name='Drawdown',
        marker_color=colors, opacity=0.7), row=2, col=1)

    fig.update_layout(
        template='plotly_dark', height=700, width=1400,
        title='Portfolio Performance',
        showlegend=True,
        legend=dict(x=0.01, y=0.99))
    fig.update_yaxes(title_text='Equity (₹)', tickformat=',.0f', row=1, col=1)
    fig.update_yaxes(title_text='Drawdown %', row=2, col=1)
    return fig


def make_monthly_heatmap(df_t):
    """Monthly P&L heatmap + bar chart."""
    df_t['year'] = df_t['exit_ts'].dt.year
    df_t['month_num'] = df_t['exit_ts'].dt.month
    df_t['month_name'] = df_t['exit_ts'].dt.strftime('%b')

    monthly = df_t.groupby(['year', 'month_num']).agg(
        pnl=('pnl', 'sum'),
        trades=('pnl', 'count'),
        wins=('pnl', lambda x: (x > 0).sum()),
    ).reset_index()
    monthly['wr'] = monthly['wins'] / monthly['trades'] * 100
    monthly['month_label'] = monthly.apply(
        lambda r: f"{int(r['year'])}-{int(r['month_num']):02d}", axis=1)

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=["Monthly P&L (₹)", "Monthly Win Rate (%)"],
        row_heights=[0.6, 0.4], vertical_spacing=0.12)

    colors = ['#22c55e' if p > 0 else '#ef4444' for p in monthly['pnl']]
    fig.add_trace(go.Bar(
        x=monthly['month_label'], y=monthly['pnl'],
        name='Monthly P&L', marker_color=colors,
        text=[f"₹{p:,.0f}" for p in monthly['pnl']],
        textposition='outside', textfont_size=10), row=1, col=1)

    fig.add_trace(go.Bar(
        x=monthly['month_label'], y=monthly['wr'],
        name='Win Rate', marker_color='#60a5fa',
        text=[f"{w:.0f}%" for w in monthly['wr']],
        textposition='outside', textfont_size=10), row=2, col=1)

    fig.add_hline(y=0, line_color='#666', row=1, col=1)
    fig.add_hline(y=50, line_dash='dash', line_color='#666', row=2, col=1)

    fig.update_layout(
        template='plotly_dark', height=600, width=1400,
        title='Monthly Performance Breakdown',
        showlegend=False)
    fig.update_yaxes(title_text='P&L (₹)', tickformat=',.0f', row=1, col=1)
    fig.update_yaxes(title_text='Win Rate %', row=2, col=1)
    return fig


def make_nifty_chart_with_trades(nifty_tf, df_t, month_label, month_trades):
    """NIFTY candlestick chart for a specific month with trade markers."""
    if month_trades.empty:
        return None

    start = month_trades['entry_ts'].min() - timedelta(days=1)
    end = month_trades['exit_ts'].max() + timedelta(days=1)
    candles = nifty_tf.loc[start:end]

    if candles.empty:
        return None

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=candles.index, open=candles['open'], high=candles['high'],
        low=candles['low'], close=candles['close'],
        name='NIFTY', increasing_line_color='#22c55e',
        decreasing_line_color='#ef4444'))

    # Trade markers
    for _, trade in month_trades.iterrows():
        color = '#22c55e' if trade['pnl'] > 0 else '#ef4444'
        marker_sym = 'triangle-up' if trade['type'] == 'BULL_PUT' else 'triangle-down'

        # Find NIFTY price at entry/exit for marker placement
        entry_idx = candles.index.searchsorted(trade['entry_ts'], side='right') - 1
        exit_idx = candles.index.searchsorted(trade['exit_ts'], side='right') - 1
        entry_price = float(candles['close'].iloc[max(0, entry_idx)]) if entry_idx >= 0 else None
        exit_price = float(candles['close'].iloc[max(0, exit_idx)]) if exit_idx >= 0 else None

        if entry_price:
            fig.add_trace(go.Scatter(
                x=[trade['entry_ts']], y=[entry_price],
                mode='markers+text',
                marker=dict(symbol=marker_sym, size=14, color=color, line=dict(width=1, color='white')),
                text=[f"ENTRY<br>{trade['type']}<br>₹{trade['entry_credit']:.1f}"],
                textposition='top center', textfont=dict(size=8, color=color),
                name=f"T{_+1} Entry", showlegend=False))

        if exit_price:
            exit_sym = 'x' if trade['pnl'] < 0 else 'star'
            fig.add_trace(go.Scatter(
                x=[trade['exit_ts']], y=[exit_price],
                mode='markers+text',
                marker=dict(symbol=exit_sym, size=12, color=color, line=dict(width=1, color='white')),
                text=[f"EXIT<br>{trade['exit_reason']}<br>P&L: ₹{trade['pnl']:,.0f}"],
                textposition='bottom center', textfont=dict(size=8, color=color),
                name=f"T{_+1} Exit", showlegend=False))

        # Connect entry to exit with line
        if entry_price and exit_price:
            fig.add_trace(go.Scatter(
                x=[trade['entry_ts'], trade['exit_ts']],
                y=[entry_price, exit_price],
                mode='lines', line=dict(color=color, width=1, dash='dot'),
                showlegend=False))

    month_pnl = month_trades['pnl'].sum()
    month_wr = (month_trades['pnl'] > 0).mean() * 100
    fig.update_layout(
        template='plotly_dark', height=600, width=1400,
        title=f"NIFTY 10min Chart — {month_label} | "
              f"{len(month_trades)} Trades | P&L: ₹{month_pnl:,.0f} | WR: {month_wr:.0f}%",
        xaxis_rangeslider_visible=False,
        yaxis_title='NIFTY Spot')
    return fig


def make_option_trade_chart(conn, trade, trade_num):
    """Individual option leg chart for a single trade."""
    sell_df = load_option_1m(conn, trade['sell_sym'],
                             trade['entry_ts'].date(), trade['exit_ts'].date())
    buy_df = load_option_1m(conn, trade['buy_sym'],
                            trade['entry_ts'].date(), trade['exit_ts'].date())

    if sell_df.empty and buy_df.empty:
        return None

    fig = make_subplots(rows=1, cols=1)

    # Sell leg
    if not sell_df.empty:
        fig.add_trace(go.Scatter(
            x=sell_df.index, y=sell_df['close'],
            mode='lines', name=f'SELL: {trade["sell_sym"]}',
            line=dict(color='#ef4444', width=2)))
        # Entry marker
        fig.add_trace(go.Scatter(
            x=[trade['entry_ts']], y=[trade['sell_entry']],
            mode='markers', marker=dict(symbol='triangle-down', size=14, color='#ef4444',
                                         line=dict(width=2, color='white')),
            name=f'Sell Entry ₹{trade["sell_entry"]:.1f}', showlegend=True))
        # Exit marker
        fig.add_trace(go.Scatter(
            x=[trade['exit_ts']], y=[trade['sell_exit']],
            mode='markers', marker=dict(symbol='square', size=12, color='#ef4444',
                                         line=dict(width=2, color='white')),
            name=f'Sell Exit ₹{trade["sell_exit"]:.1f}', showlegend=True))

    # Buy leg
    if not buy_df.empty:
        fig.add_trace(go.Scatter(
            x=buy_df.index, y=buy_df['close'],
            mode='lines', name=f'BUY: {trade["buy_sym"]}',
            line=dict(color='#22c55e', width=2)))
        fig.add_trace(go.Scatter(
            x=[trade['entry_ts']], y=[trade['buy_entry']],
            mode='markers', marker=dict(symbol='triangle-up', size=14, color='#22c55e',
                                         line=dict(width=2, color='white')),
            name=f'Buy Entry ₹{trade["buy_entry"]:.1f}', showlegend=True))
        fig.add_trace(go.Scatter(
            x=[trade['exit_ts']], y=[trade['buy_exit']],
            mode='markers', marker=dict(symbol='square', size=12, color='#22c55e',
                                         line=dict(width=2, color='white')),
            name=f'Buy Exit ₹{trade["buy_exit"]:.1f}', showlegend=True))

    # Shade trade period
    fig.add_vrect(x0=trade['entry_ts'], x1=trade['exit_ts'],
                  fillcolor='rgba(100,100,255,0.1)', line_width=0)

    pnl_color = '#22c55e' if trade['pnl'] > 0 else '#ef4444'
    result = 'WIN' if trade['pnl'] > 0 else 'LOSS'
    fig.update_layout(
        template='plotly_dark', height=400, width=1200,
        title=f"Trade #{trade_num} | {trade['type']} | {result} | "
              f"Credit: ₹{trade['entry_credit']:.1f} → Spread: ₹{trade['exit_spread']:.1f} | "
              f"P&L: <span style='color:{pnl_color}'>₹{trade['pnl']:,.0f}</span> | "
              f"{trade['exit_reason']}",
        yaxis_title='Option Price (₹)',
        legend=dict(x=0.01, y=0.99, bgcolor='rgba(0,0,0,0.6)'))
    return fig


# ============================================================================
# HTML REPORT GENERATOR
# ============================================================================

def generate_html_report(df_t, config_name, charts_dir):
    """Generate professional multi-page HTML report."""

    total_pnl = df_t['pnl'].sum()
    roi = total_pnl / INITIAL_CAPITAL * 100
    wins = df_t[df_t['pnl'] > 0]
    losses = df_t[df_t['pnl'] <= 0]
    wr = len(wins) / len(df_t) * 100
    pf = wins['pnl'].sum() / abs(losses['pnl'].sum()) if len(losses) > 0 else float('inf')
    max_eq = df_t['equity'].cummax()
    max_dd = ((df_t['equity'] - max_eq) / max_eq * 100).min()
    sharpe = df_t['pnl'].mean() / df_t['pnl'].std() * np.sqrt(252) if df_t['pnl'].std() > 0 else 0

    start_date = df_t['entry_ts'].min().strftime('%d %b %Y')
    end_date = df_t['exit_ts'].max().strftime('%d %b %Y')
    n_primary = df_t[~df_t['is_continuation']].shape[0]
    n_cont = df_t[df_t['is_continuation']].shape[0]

    # Monthly summary
    df_t['month'] = df_t['exit_ts'].dt.to_period('M')
    monthly = df_t.groupby('month').agg(
        trades=('pnl', 'count'),
        wins_count=('pnl', lambda x: (x > 0).sum()),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        max_win=('pnl', 'max'),
        max_loss=('pnl', 'min'),
        charges=('charges', 'sum'),
    ).reset_index()
    monthly['wr'] = monthly['wins_count'] / monthly['trades'] * 100
    monthly['cum_pnl'] = monthly['total_pnl'].cumsum()

    config_display = config_name.replace('_', ' ').replace('&', '&amp;')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NIFTY Options Strategy - Trade Journal Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  .header {{ background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1px solid #334155; border-radius: 12px; padding: 30px; margin-bottom: 24px; text-align: center; }}
  .header h1 {{ font-size: 28px; color: #f1f5f9; margin-bottom: 8px; }}
  .header .subtitle {{ color: #94a3b8; font-size: 14px; }}
  .header .period {{ color: #60a5fa; font-size: 16px; margin-top: 8px; }}
  .section {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
  .section h2 {{ font-size: 20px; color: #f1f5f9; margin-bottom: 16px; border-bottom: 2px solid #334155; padding-bottom: 8px; }}
  .section h3 {{ font-size: 16px; color: #cbd5e1; margin: 16px 0 8px; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .metric {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; text-align: center; }}
  .metric .value {{ font-size: 28px; font-weight: 700; }}
  .metric .label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
  .positive {{ color: #22c55e; }}
  .negative {{ color: #ef4444; }}
  .neutral {{ color: #60a5fa; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #0f172a; color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; padding: 10px 8px; text-align: left; border-bottom: 2px solid #334155; position: sticky; top: 0; }}
  td {{ padding: 8px; border-bottom: 1px solid #1e293b; }}
  tr:hover {{ background: rgba(59, 130, 246, 0.05); }}
  tr.win {{ background: rgba(34, 197, 94, 0.05); }}
  tr.loss {{ background: rgba(239, 68, 68, 0.05); }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .tag-bull {{ background: rgba(34, 197, 94, 0.15); color: #22c55e; }}
  .tag-bear {{ background: rgba(239, 68, 68, 0.15); color: #ef4444; }}
  .tag-target {{ background: rgba(96, 165, 250, 0.15); color: #60a5fa; }}
  .tag-reversal {{ background: rgba(251, 191, 36, 0.15); color: #fbbf24; }}
  .tag-eod {{ background: rgba(148, 163, 184, 0.15); color: #94a3b8; }}
  .tag-maxloss {{ background: rgba(239, 68, 68, 0.3); color: #fca5a5; }}
  .chart-container {{ margin: 16px 0; text-align: center; }}
  .chart-container iframe {{ border: none; border-radius: 8px; }}
  .month-section {{ margin-bottom: 32px; }}
  .month-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding: 12px 16px; background: #0f172a; border-radius: 8px; border-left: 4px solid; }}
  .month-header.profit {{ border-color: #22c55e; }}
  .month-header.loss {{ border-color: #ef4444; }}
  .month-header h3 {{ margin: 0; font-size: 18px; }}
  .month-stats {{ display: flex; gap: 24px; font-size: 14px; }}
  .month-stats span {{ color: #94a3b8; }}
  .month-stats .value {{ font-weight: 600; }}
  .disclaimer {{ text-align: center; color: #64748b; font-size: 11px; margin-top: 40px; padding: 20px; border-top: 1px solid #334155; }}
  .page-break {{ page-break-before: always; }}
  @media print {{
    body {{ background: white; color: #1e293b; }}
    .section {{ border-color: #e2e8f0; }}
    th {{ background: #f1f5f9; color: #475569; }}
  }}
</style>
</head>
<body>
<div class="container">

<!-- HEADER -->
<div class="header">
  <h1>NIFTY Options Credit Spread Strategy</h1>
  <div class="subtitle">Intraday Trading Journal & Performance Report</div>
  <div class="subtitle" style="margin-top:4px;">Strategy: {config_display} | SuperTrend Signal-Based Credit Spreads</div>
  <div class="period">{start_date} — {end_date}</div>
</div>

<!-- EXECUTIVE SUMMARY -->
<div class="section">
  <h2>Executive Summary</h2>
  <div class="metrics-grid">
    <div class="metric">
      <div class="value {'positive' if total_pnl > 0 else 'negative'}">₹{total_pnl:,.0f}</div>
      <div class="label">Total Net P&L</div>
    </div>
    <div class="metric">
      <div class="value {'positive' if roi > 0 else 'negative'}">{roi:.1f}%</div>
      <div class="label">Return on Capital</div>
    </div>
    <div class="metric">
      <div class="value neutral">₹{INITIAL_CAPITAL:,}</div>
      <div class="label">Initial Capital</div>
    </div>
    <div class="metric">
      <div class="value neutral">₹{INITIAL_CAPITAL + total_pnl:,.0f}</div>
      <div class="label">Final Equity</div>
    </div>
    <div class="metric">
      <div class="value {'positive' if wr >= 50 else 'negative'}">{wr:.1f}%</div>
      <div class="label">Win Rate</div>
    </div>
    <div class="metric">
      <div class="value {'positive' if pf > 1 else 'negative'}">{pf:.2f}</div>
      <div class="label">Profit Factor</div>
    </div>
    <div class="metric">
      <div class="value negative">{max_dd:.1f}%</div>
      <div class="label">Max Drawdown</div>
    </div>
    <div class="metric">
      <div class="value {'positive' if sharpe > 1 else 'neutral'}">{sharpe:.2f}</div>
      <div class="label">Sharpe Ratio (Ann.)</div>
    </div>
    <div class="metric">
      <div class="value neutral">{len(df_t)}</div>
      <div class="label">Total Trades</div>
    </div>
    <div class="metric">
      <div class="value neutral">{n_primary} / {n_cont}</div>
      <div class="label">Primary / Continuation</div>
    </div>
    <div class="metric">
      <div class="value neutral">₹{df_t['pnl'].mean():,.0f}</div>
      <div class="label">Avg P&L per Trade</div>
    </div>
    <div class="metric">
      <div class="value neutral">₹{df_t['charges'].sum():,.0f}</div>
      <div class="label">Total Charges</div>
    </div>
  </div>

  <h3>Strategy Details</h3>
  <table>
    <tr><td style="width:200px;color:#94a3b8;">Instrument</td><td>NIFTY 50 Index Options (NSE/NFO)</td></tr>
    <tr><td style="color:#94a3b8;">Strategy Type</td><td>Credit Spread (Bull Put / Bear Call)</td></tr>
    <tr><td style="color:#94a3b8;">Signal</td><td>SuperTrend Indicator on 10-min NIFTY Chart</td></tr>
    <tr><td style="color:#94a3b8;">Position Sizing</td><td>5 Lots per trade</td></tr>
    <tr><td style="color:#94a3b8;">Trade Mode</td><td>Intraday Only (all positions squared off by 15:20)</td></tr>
    <tr><td style="color:#94a3b8;">Entry</td><td>Next bar open after SuperTrend flip signal (no lookahead)</td></tr>
    <tr><td style="color:#94a3b8;">Exit</td><td>Target (premium decay) / Reversal signal / EOD 15:20 / Max Loss</td></tr>
    <tr><td style="color:#94a3b8;">Avg Win / Avg Loss</td><td>₹{wins['pnl'].mean():,.0f} / ₹{losses['pnl'].mean():,.0f}</td></tr>
    <tr><td style="color:#94a3b8;">Risk:Reward</td><td>1:{abs(wins['pnl'].mean()/losses['pnl'].mean()):.2f}</td></tr>
    <tr><td style="color:#94a3b8;">Max Win / Max Loss</td><td>₹{df_t['pnl'].max():,.0f} / ₹{df_t['pnl'].min():,.0f}</td></tr>
  </table>
</div>

<!-- EQUITY CURVE CHART -->
<div class="section">
  <h2>Equity Curve & Drawdown</h2>
  <div class="chart-container">
    <iframe src="charts/equity_drawdown.html" width="100%" height="720" loading="lazy"></iframe>
  </div>
</div>

<!-- MONTHLY P&L CHART -->
<div class="section">
  <h2>Monthly Performance</h2>
  <div class="chart-container">
    <iframe src="charts/monthly_pnl.html" width="100%" height="620" loading="lazy"></iframe>
  </div>
</div>

<!-- MONTHLY SUMMARY TABLE -->
<div class="section">
  <h2>Monthly Summary</h2>
  <table>
    <thead>
      <tr>
        <th>Month</th><th>Trades</th><th>Wins</th><th>Win Rate</th>
        <th>Gross P&L</th><th>Charges</th><th>Net P&L</th>
        <th>Cumulative</th><th>Max Win</th><th>Max Loss</th>
      </tr>
    </thead>
    <tbody>
"""
    for _, row in monthly.iterrows():
        pnl_class = 'positive' if row['total_pnl'] > 0 else 'negative'
        html += f"""      <tr class="{'win' if row['total_pnl'] > 0 else 'loss'}">
        <td><strong>{row['month']}</strong></td>
        <td>{int(row['trades'])}</td>
        <td>{int(row['wins_count'])}</td>
        <td>{row['wr']:.0f}%</td>
        <td class="{pnl_class}">₹{row['total_pnl'] + row['charges']:,.0f}</td>
        <td>₹{row['charges']:,.0f}</td>
        <td class="{pnl_class}"><strong>₹{row['total_pnl']:,.0f}</strong></td>
        <td>₹{row['cum_pnl']:,.0f}</td>
        <td class="positive">₹{row['max_win']:,.0f}</td>
        <td class="negative">₹{row['max_loss']:,.0f}</td>
      </tr>\n"""

    win_months = (monthly['total_pnl'] > 0).sum()
    loss_months = (monthly['total_pnl'] <= 0).sum()
    html += f"""    </tbody>
  </table>
  <p style="margin-top:12px;color:#94a3b8;">
    Profitable Months: <strong class="positive">{win_months}</strong> |
    Losing Months: <strong class="negative">{loss_months}</strong> |
    Monthly Win Rate: <strong>{win_months/(win_months+loss_months)*100:.0f}%</strong> |
    Avg Monthly P&L: <strong>₹{monthly['total_pnl'].mean():,.0f}</strong>
  </p>
</div>
"""

    # -- MONTH-WISE TRADE JOURNAL WITH NIFTY CHARTS ---------------------------
    html += """
<div class="section page-break">
  <h2>Month-wise Trade Journal with NIFTY Charts</h2>
"""
    for period in df_t['month'].unique():
        month_trades = df_t[df_t['month'] == period].reset_index(drop=True)
        month_label = str(period)
        month_pnl = month_trades['pnl'].sum()
        month_wr = (month_trades['pnl'] > 0).mean() * 100
        pnl_class = 'profit' if month_pnl > 0 else 'loss'

        chart_file = f"charts/nifty_{month_label}.html"
        html += f"""
  <div class="month-section">
    <div class="month-header {pnl_class}">
      <h3>{month_label}</h3>
      <div class="month-stats">
        <span>Trades: <span class="value">{len(month_trades)}</span></span>
        <span>Win Rate: <span class="value">{month_wr:.0f}%</span></span>
        <span>P&L: <span class="value {'positive' if month_pnl > 0 else 'negative'}">₹{month_pnl:,.0f}</span></span>
      </div>
    </div>

    <div class="chart-container">
      <iframe src="{chart_file}" width="100%" height="620" loading="lazy"></iframe>
    </div>

    <table>
      <thead>
        <tr>
          <th>#</th><th>Date</th><th>Type</th><th>Sell Strike</th><th>Buy Strike</th>
          <th>Entry Time</th><th>Exit Time</th>
          <th>Credit</th><th>Exit Spread</th>
          <th>Qty</th><th>Gross P&L</th><th>Charges</th><th>Net P&L</th>
          <th>Exit Reason</th><th>Options Chart</th>
        </tr>
      </thead>
      <tbody>
"""
        for idx, trade in month_trades.iterrows():
            pnl_class = 'positive' if trade['pnl'] > 0 else 'negative'
            row_class = 'win' if trade['pnl'] > 0 else 'loss'
            type_class = 'tag-bull' if trade['type'] == 'BULL_PUT' else 'tag-bear'
            type_label = 'Bull Put' if trade['type'] == 'BULL_PUT' else 'Bear Call'

            exit_r = trade['exit_reason']
            if 'Target' in str(exit_r):
                exit_tag = 'tag-target'
            elif 'Reversal' in str(exit_r):
                exit_tag = 'tag-reversal'
            elif 'EOD' in str(exit_r):
                exit_tag = 'tag-eod'
            elif 'MaxLoss' in str(exit_r):
                exit_tag = 'tag-maxloss'
            else:
                exit_tag = 'tag-eod'

            cont_mark = ' *' if trade['is_continuation'] else ''
            trade_num = idx + 1
            opt_chart = f"charts/trade_{month_label}_{trade_num}.html"

            html += f"""        <tr class="{row_class}">
          <td>{trade_num}{cont_mark}</td>
          <td>{trade['entry_ts'].strftime('%d %b')}</td>
          <td><span class="tag {type_class}">{type_label}</span></td>
          <td>{trade['sell_sym']}<br><small>₹{trade['sell_entry']:.1f} → ₹{trade['sell_exit']:.1f}</small></td>
          <td>{trade['buy_sym']}<br><small>₹{trade['buy_entry']:.1f} → ₹{trade['buy_exit']:.1f}</small></td>
          <td>{trade['entry_ts'].strftime('%H:%M')}</td>
          <td>{trade['exit_ts'].strftime('%H:%M')}</td>
          <td>₹{trade['entry_credit']:.1f}</td>
          <td>₹{trade['exit_spread']:.1f}</td>
          <td>{int(trade['qty'])}</td>
          <td>₹{trade['gross_pnl']:,.0f}</td>
          <td>₹{trade['charges']:,.0f}</td>
          <td class="{pnl_class}"><strong>₹{trade['pnl']:,.0f}</strong></td>
          <td><span class="tag {exit_tag}">{exit_r}</span></td>
          <td><a href="{opt_chart}" target="_blank" style="color:#60a5fa;">View</a></td>
        </tr>\n"""

        html += """      </tbody>
    </table>
    <p style="font-size:11px;color:#64748b;margin-top:4px;">* = continuation trade after target hit</p>
  </div>
"""

    html += "</div>\n"

    # -- DAY-WISE FULL TRADE LOG -----------------------------------------------
    html += """
<div class="section page-break">
  <h2>Complete Day-wise Trade Log</h2>
  <div style="overflow-x:auto;">
  <table>
    <thead>
      <tr>
        <th>#</th><th>Entry Date</th><th>Entry Time</th><th>Exit Date</th><th>Exit Time</th>
        <th>Type</th><th>Sell Symbol</th><th>Buy Symbol</th>
        <th>Sell Entry</th><th>Buy Entry</th><th>Sell Exit</th><th>Buy Exit</th>
        <th>Credit</th><th>Exit Spread</th><th>Qty</th><th>Spread Width</th>
        <th>Gross P&L</th><th>Charges</th><th>Net P&L</th><th>Cum P&L</th>
        <th>Equity</th><th>Exit Reason</th><th>Cont?</th>
      </tr>
    </thead>
    <tbody>
"""
    for i, trade in df_t.iterrows():
        pnl_class = 'positive' if trade['pnl'] > 0 else 'negative'
        row_class = 'win' if trade['pnl'] > 0 else 'loss'
        html += f"""      <tr class="{row_class}">
        <td>{i+1}</td>
        <td>{trade['entry_ts'].strftime('%d %b %Y')}</td>
        <td>{trade['entry_ts'].strftime('%H:%M')}</td>
        <td>{trade['exit_ts'].strftime('%d %b %Y')}</td>
        <td>{trade['exit_ts'].strftime('%H:%M')}</td>
        <td>{'Bull Put' if trade['type']=='BULL_PUT' else 'Bear Call'}</td>
        <td>{trade['sell_sym']}</td>
        <td>{trade['buy_sym']}</td>
        <td>₹{trade['sell_entry']:.2f}</td>
        <td>₹{trade['buy_entry']:.2f}</td>
        <td>₹{trade['sell_exit']:.2f}</td>
        <td>₹{trade['buy_exit']:.2f}</td>
        <td>₹{trade['entry_credit']:.2f}</td>
        <td>₹{trade['exit_spread']:.2f}</td>
        <td>{int(trade['qty'])}</td>
        <td>{int(trade['spread_width'])}pt</td>
        <td>₹{trade['gross_pnl']:,.0f}</td>
        <td>₹{trade['charges']:,.0f}</td>
        <td class="{pnl_class}"><strong>₹{trade['pnl']:,.0f}</strong></td>
        <td>₹{trade['cum_pnl']:,.0f}</td>
        <td>₹{trade['equity']:,.0f}</td>
        <td>{trade['exit_reason']}</td>
        <td>{'Yes' if trade['is_continuation'] else ''}</td>
      </tr>\n"""

    html += """    </tbody>
  </table>
  </div>
</div>
"""

    # -- FOOTER ----------------------------------------------------------------
    html += f"""
<div class="disclaimer">
  <p>This report is generated from historical backtesting data and is for informational purposes only.</p>
  <p>Past performance does not guarantee future results. Options trading involves significant risk of loss.</p>
  <p>Generated: {datetime.now().strftime('%d %B %Y %H:%M')} | Data Period: {start_date} to {end_date}</p>
  <p>Strategy Engine: SuperTrend Credit Spread (No Lookahead Bias, Actual Option Prices from Historical DB)</p>
</div>

</div>
</body>
</html>"""

    return html


# ============================================================================
# MAIN
# ============================================================================
def main():
    config_name = REPORT_CONFIG
    print("=" * 80)
    print(f"TRADE JOURNAL REPORT GENERATOR — {config_name}")
    print("=" * 80)

    # Load trades
    df_t = load_trade_data(config_name)
    print(f"  Loaded {len(df_t)} trades from {config_name}")

    # Create output directory
    out_dir = _script_dir / f"report_{config_name}"
    charts_dir = out_dir / "charts"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    charts_dir.mkdir(parents=True)
    print(f"  Output: {out_dir}")

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # Load NIFTY data
    print("\n[1] Loading NIFTY data...")
    start_date = df_t['entry_ts'].min().date()
    end_date = df_t['exit_ts'].max().date()
    nifty_1m = load_nifty_1m(conn, start_date, end_date)
    nifty_tf = resample_nifty(nifty_1m, '10min')
    print(f"  NIFTY 10m bars: {len(nifty_tf):,}")

    # Generate equity + drawdown chart
    print("\n[2] Generating performance charts...")
    eq_fig = make_equity_drawdown_chart(df_t)
    eq_fig.write_html(str(charts_dir / "equity_drawdown.html"), include_plotlyjs='cdn')

    monthly_fig = make_monthly_heatmap(df_t)
    monthly_fig.write_html(str(charts_dir / "monthly_pnl.html"), include_plotlyjs='cdn')
    print("  Equity + Monthly charts done")

    # Generate NIFTY charts per month
    print("\n[3] Generating monthly NIFTY charts with trades...")
    df_t['month'] = df_t['exit_ts'].dt.to_period('M')
    months = df_t['month'].unique()
    for period in months:
        month_trades = df_t[df_t['month'] == period].reset_index(drop=True)
        month_label = str(period)
        fig = make_nifty_chart_with_trades(nifty_tf, df_t, month_label, month_trades)
        if fig:
            fig.write_html(str(charts_dir / f"nifty_{month_label}.html"), include_plotlyjs='cdn')
            print(f"  {month_label}: {len(month_trades)} trades")

    # Generate individual option charts per trade per month
    print("\n[4] Generating individual option trade charts...")
    total_opt_charts = 0
    for period in months:
        month_trades = df_t[df_t['month'] == period].reset_index(drop=True)
        month_label = str(period)
        for idx, trade in month_trades.iterrows():
            trade_num = idx + 1
            fig = make_option_trade_chart(conn, trade, trade_num)
            if fig:
                fig.write_html(
                    str(charts_dir / f"trade_{month_label}_{trade_num}.html"),
                    include_plotlyjs='cdn')
                total_opt_charts += 1
        print(f"  {month_label}: {len(month_trades)} option charts")

    print(f"  Total option charts: {total_opt_charts}")

    conn.close()

    # Generate HTML report
    print("\n[5] Generating HTML report...")
    html = generate_html_report(df_t, config_name, charts_dir)
    report_path = out_dir / "index.html"
    with open(report_path, 'w') as f:
        f.write(html)
    print(f"  Report: {report_path}")

    # Save Excel with multiple sheets
    print("\n[6] Generating Excel workbook...")
    excel_path = out_dir / "trade_journal.xlsx"
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        # Full trade log
        df_export = df_t.copy()
        df_export['month'] = df_export['month'].astype(str)
        df_export.to_excel(writer, sheet_name='All Trades', index=False)

        # Monthly summary
        monthly_export = df_t.groupby(df_t['exit_ts'].dt.to_period('M')).agg(
            Trades=('pnl', 'count'),
            Wins=('pnl', lambda x: (x > 0).sum()),
            Losses=('pnl', lambda x: (x <= 0).sum()),
            WinRate=('pnl', lambda x: (x > 0).mean() * 100),
            GrossPnL=('gross_pnl', 'sum'),
            Charges=('charges', 'sum'),
            NetPnL=('pnl', 'sum'),
            AvgPnL=('pnl', 'mean'),
            MaxWin=('pnl', 'max'),
            MaxLoss=('pnl', 'min'),
        ).reset_index()
        monthly_export.columns = ['Month', 'Trades', 'Wins', 'Losses', 'Win Rate %',
                                   'Gross P&L', 'Charges', 'Net P&L', 'Avg P&L',
                                   'Max Win', 'Max Loss']
        monthly_export['Month'] = monthly_export['Month'].astype(str)
        monthly_export['Cumulative'] = monthly_export['Net P&L'].cumsum()
        monthly_export.to_excel(writer, sheet_name='Monthly Summary', index=False)

        # Day of week
        dow = df_t.groupby(df_t['entry_ts'].dt.day_name()).agg(
            Trades=('pnl', 'count'),
            WinRate=('pnl', lambda x: (x > 0).mean() * 100),
            PnL=('pnl', 'sum'),
            AvgPnL=('pnl', 'mean'),
        ).reindex(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'])
        dow.to_excel(writer, sheet_name='Day of Week')

        # Exit reasons
        exits = df_t.groupby('exit_reason').agg(
            Trades=('pnl', 'count'),
            WinRate=('pnl', lambda x: (x > 0).mean() * 100),
            PnL=('pnl', 'sum'),
        ).sort_values('PnL', ascending=False)
        exits.to_excel(writer, sheet_name='Exit Analysis')

    print(f"  Excel: {excel_path}")

    # Create ZIP
    print("\n[7] Creating ZIP package...")
    zip_path = _script_dir / f"NIFTY_TradeJournal_{config_name}.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(out_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(out_dir)
                zf.write(file_path, arcname)

    zip_size = zip_path.stat().st_size / (1024 * 1024)
    print(f"  ZIP: {zip_path} ({zip_size:.1f} MB)")

    print(f"\n{'='*80}")
    print("REPORT COMPLETE")
    print(f"{'='*80}")
    print(f"  HTML Report:  {report_path}")
    print(f"  Excel:        {excel_path}")
    print(f"  ZIP Package:  {zip_path}")
    print(f"  Charts:       {total_opt_charts + len(months) + 2} files")
    print(f"\n  Open in browser: file://{report_path}")


if __name__ == "__main__":
    main()
