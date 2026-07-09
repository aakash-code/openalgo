import json
import os
from datetime import datetime

RESULTS_DIR = "survivor_ultimate_results"
TRADES_FILE = os.path.join(RESULTS_DIR, "trades.json")
EXPIRY_FILE = os.path.join(RESULTS_DIR, "expiry_stats.json")
REPORT_FILE = os.path.join(RESULTS_DIR, "Survivor_Ultimate_Report.html")

def load_json(filepath):
    with open(filepath, 'r') as f:
        return json.load(f)

def generate_html_report():
    if not os.path.exists(TRADES_FILE) or not os.path.exists(EXPIRY_FILE):
        print("Error: Backtest result files not found.")
        return

    trades_data = load_json(TRADES_FILE)
    expiry_data = load_json(EXPIRY_FILE)
    
    summary = trades_data.get("summary", {})
    config = trades_data.get("strategy_config", {})
    period = trades_data.get("backtest_period", "Unknown")

    # Generate Expiry Table Rows
    expiry_rows = ""
    for e in expiry_data:
        pnl_class = "text-success" if e["realised_pnl"] >= 0 else "text-danger"
        roi_class = "text-success" if e["roi_on_peak_margin"] >= 0 else "text-danger"
        
        expiry_rows += f"""
        <tr>
            <td>{e['expiry']}</td>
            <td>{e['trades']}</td>
            <td>₹ {e['total_credit']:,.2f}</td>
            <td class="{pnl_class}"><strong>₹ {e['realised_pnl']:,.2f}</strong></td>
            <td>₹ {e['peak_margin']:,.2f}</td>
            <td class="{roi_class}"><strong>{e['roi_on_peak_margin']:.2f}%</strong></td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Survivor Strategy Backtest Report</title>
        <style>
            :root {{
                --bg-color: #f8f9fa;
                --card-bg: #ffffff;
                --text-color: #333333;
                --border-color: #e9ecef;
                --primary: #4361ee;
                --success: #28a745;
                --danger: #dc3545;
            }}
            body {{
                font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--bg-color);
                color: var(--text-color);
                margin: 0;
                padding: 20px;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            .card {{
                background-color: var(--card-bg);
                border-radius: 8px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.05);
                padding: 20px;
                margin-bottom: 20px;
            }}
            .grid-3 {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
            }}
            .metric {{
                text-align: center;
                padding: 15px;
                border: 1px solid var(--border-color);
                border-radius: 6px;
            }}
            .metric h3 {{ margin: 0; font-size: 0.9rem; color: #6c757d; text-transform: uppercase; }}
            .metric p {{ margin: 10px 0 0; font-size: 1.8rem; font-weight: bold; color: var(--primary); }}
            
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 15px;
            }}
            th, td {{
                padding: 12px 15px;
                text-align: right;
                border-bottom: 1px solid var(--border-color);
            }}
            th:first-child, td:first-child {{ text-align: left; }}
            th {{
                background-color: var(--bg-color);
                font-weight: 600;
                color: #495057;
            }}
            tr:hover {{ background-color: rgba(0,0,0,0.015); }}
            
            .text-success {{ color: var(--success) !important; }}
            .text-danger {{ color: var(--danger) !important; }}
            .badge {{
                display: inline-block;
                padding: 4px 8px;
                border-radius: 4px;
                font-size: 0.8rem;
                font-weight: 600;
                background-color: var(--bg-color);
                border: 1px solid var(--border-color);
                margin-right: 5px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Survivor Strategy Backtest Report</h1>
                <p>Period: {period} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
            </div>

            <div class="card grid-3">
                <div class="metric">
                    <h3>Total Realised P&L</h3>
                    <p class="{'text-success' if summary.get('total_realised_pnl', 0) >= 0 else 'text-danger'}">
                        ₹ {summary.get('total_realised_pnl', 0):,.2f}
                    </p>
                </div>
                <div class="metric">
                    <h3>Peak Concurrent Margin</h3>
                    <p>₹ {summary.get('peak_concurrent_margin', 0):,.2f}</p>
                </div>
                <div class="metric">
                    <h3>Total Trades</h3>
                    <p>{summary.get('total_trades', 0)}</p>
                </div>
                <div class="metric">
                    <h3>Win Rate</h3>
                    <p class="text-success">{summary.get('win_rate_pct', 0):.1f}%</p>
                </div>
                <div class="metric">
                    <h3>Avg Win / Avg Loss</h3>
                    <p style="font-size: 1.2rem;">
                        <span class="text-success">₹ {summary.get('avg_win', 0):,.2f}</span> / 
                        <span class="text-danger">₹ {summary.get('avg_loss', 0):,.2f}</span>
                    </p>
                </div>
                <div class="metric">
                    <h3>Total Credit Collected</h3>
                    <p>₹ {summary.get('total_credit_collected', 0):,.2f}</p>
                </div>
            </div>

            <div class="card">
                <h2>Strategy Configuration</h2>
                <div>
                    <span class="badge">PE Gap: {config.get('pe_gap')}</span>
                    <span class="badge">CE Gap: {config.get('ce_gap')}</span>
                    <span class="badge">PE Dist: {config.get('pe_symbol_gap')}</span>
                    <span class="badge">CE Dist: {config.get('ce_symbol_gap')}</span>
                    <span class="badge">Lot Size: {config.get('lot_size')} (Fallback)</span>
                    <span class="badge">Min Premium: ₹ {config.get('min_price_to_sell')}</span>
                </div>
            </div>

            <div class="card">
                <h2>Expiry-by-Expiry Breakdown</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Expiry Date</th>
                            <th>Trades</th>
                            <th>Total Credit</th>
                            <th>Realised P&L</th>
                            <th>Peak Margin</th>
                            <th>ROI on Peak %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {expiry_rows}
                    </tbody>
                </table>
            </div>
            
            <div style="text-align: center; color: #888; margin-top: 20px; font-size: 0.9rem;">
                Generated by OpenAlgo Backtest Engine
            </div>
        </div>
    </body>
    </html>
    """

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    print(f"Successfully generated HTML report at: {REPORT_FILE}")

if __name__ == "__main__":
    generate_html_report()