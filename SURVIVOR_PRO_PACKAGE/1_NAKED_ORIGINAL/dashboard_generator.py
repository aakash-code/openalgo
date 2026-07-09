from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parent
PARENT_DIR = BASE_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from single_strategy_dashboard import build_single_dashboard


if __name__ == "__main__":
    output = build_single_dashboard(
        folder=BASE_DIR,
        strategy_name="Naked Original",
        output_file=BASE_DIR / "latest_report.html",
    )
    print(f"Dashboard created: {output}")
