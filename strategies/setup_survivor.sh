#!/bin/bash

###############################################################################
# Survivor Strategy Setup Script for OpenAlgo
# This script helps you set up and configure the Survivor strategy
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../log/strategies"

echo "============================================================"
echo "  Survivor Strategy Setup for OpenAlgo"
echo "============================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Python 3 found${NC}"

# Check if OpenAlgo package is installed
if ! python3 -c "import openalgo" 2>/dev/null; then
    echo -e "${YELLOW}! OpenAlgo package not found${NC}"
    echo "  Installing OpenAlgo Python package..."
    pip install openalgo || {
        echo -e "${RED}Error: Failed to install OpenAlgo package${NC}"
        exit 1
    }
fi

echo -e "${GREEN}✓ OpenAlgo package installed${NC}"

# Create log directory
mkdir -p "$LOG_DIR"
echo -e "${GREEN}✓ Log directory created: $LOG_DIR${NC}"

# Check if config file exists
CONFIG_FILE="$SCRIPT_DIR/survivor_config.env"
if [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    echo -e "${YELLOW}! Configuration file not found${NC}"
    echo "  Creating from example..."
    cp "$SCRIPT_DIR/survivor_config.env.example" "$CONFIG_FILE"

    echo ""
    echo "============================================================"
    echo "  Configuration Setup"
    echo "============================================================"
    echo ""

    # Interactive configuration
    read -p "Enter your OpenAlgo API Key: " api_key
    sed -i "s/your-openalgo-api-key-here/$api_key/" "$CONFIG_FILE"

    read -p "Enter OpenAlgo Host [http://127.0.0.1:5000]: " host
    host=${host:-http://127.0.0.1:5000}
    sed -i "s|http://127.0.0.1:5000|$host|" "$CONFIG_FILE"

    read -p "Enter Symbol Initials (e.g., NIFTY25JAN30): " symbol
    sed -i "s/NIFTY25JAN30/$symbol/" "$CONFIG_FILE"

    read -p "Enter PE Gap [25]: " pe_gap
    pe_gap=${pe_gap:-25}
    sed -i "s/PE_GAP=25/PE_GAP=$pe_gap/" "$CONFIG_FILE"

    read -p "Enter CE Gap [25]: " ce_gap
    ce_gap=${ce_gap:-25}
    sed -i "s/CE_GAP=25/CE_GAP=$ce_gap/" "$CONFIG_FILE"

    read -p "Enter PE Quantity [50]: " pe_qty
    pe_qty=${pe_qty:-50}
    sed -i "s/PE_QUANTITY=50/PE_QUANTITY=$pe_qty/" "$CONFIG_FILE"

    read -p "Enter CE Quantity [50]: " ce_qty
    ce_qty=${ce_qty:-50}
    sed -i "s/CE_QUANTITY=50/CE_QUANTITY=$ce_qty/" "$CONFIG_FILE"

    echo -e "${GREEN}✓ Configuration saved to $CONFIG_FILE${NC}"
else
    echo -e "${GREEN}✓ Configuration file exists: $CONFIG_FILE${NC}"
fi

echo ""
echo "============================================================"
echo "  Setup Complete!"
echo "============================================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Review your configuration:"
echo "   cat $CONFIG_FILE"
echo ""
echo "2. Test in Analyzer Mode (simulated orders):"
echo "   - Open OpenAlgo UI and enable Analyzer Mode"
echo "   - Run: ./run_survivor.sh"
echo ""
echo "3. For live trading:"
echo "   - Disable Analyzer Mode in OpenAlgo UI"
echo "   - Run: ./run_survivor.sh"
echo ""
echo "4. View logs:"
echo "   tail -f $LOG_DIR/survivor_*.log"
echo ""
echo "5. For WebSocket version (faster):"
echo "   ./run_survivor_ws.sh"
echo ""
echo "============================================================"
echo ""

# Create run scripts
cat > "$SCRIPT_DIR/run_survivor.sh" <<'EOF'
#!/bin/bash
# Load configuration and run Survivor Strategy (REST version)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/survivor_config.env"

python3 "$SCRIPT_DIR/survivor_strategy.py" \
    --api-key "$OPENALGO_API_KEY" \
    --host "$OPENALGO_HOST" \
    --ws-url "$OPENALGO_WS_URL" \
    --symbol-initials "$SYMBOL_INITIALS" \
    --pe-gap "$PE_GAP" \
    --ce-gap "$CE_GAP" \
    --pe-quantity "$PE_QUANTITY" \
    --ce-quantity "$CE_QUANTITY" \
    --min-price-to-sell "$MIN_PRICE_TO_SELL" \
    --max-loss "$MAX_LOSS_PER_LOT" \
    --target-profit "$TARGET_PROFIT_PER_LOT"
EOF

chmod +x "$SCRIPT_DIR/run_survivor.sh"
echo -e "${GREEN}✓ Created run_survivor.sh${NC}"

cat > "$SCRIPT_DIR/run_survivor_ws.sh" <<'EOF'
#!/bin/bash
# Load configuration and run Survivor Strategy (WebSocket version)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/survivor_config.env"

python3 "$SCRIPT_DIR/survivor_strategy_ws.py" \
    --api-key "$OPENALGO_API_KEY" \
    --host "$OPENALGO_HOST" \
    --ws-url "$OPENALGO_WS_URL" \
    --symbol-initials "$SYMBOL_INITIALS" \
    --pe-gap "$PE_GAP" \
    --ce-gap "$CE_GAP" \
    --pe-quantity "$PE_QUANTITY" \
    --ce-quantity "$CE_QUANTITY" \
    --min-price-to-sell "$MIN_PRICE_TO_SELL" \
    --max-loss "$MAX_LOSS_PER_LOT" \
    --target-profit "$TARGET_PROFIT_PER_LOT"
EOF

chmod +x "$SCRIPT_DIR/run_survivor_ws.sh"
echo -e "${GREEN}✓ Created run_survivor_ws.sh${NC}"

echo ""
echo "Quick start:"
echo "  ./run_survivor.sh       # Run REST version"
echo "  ./run_survivor_ws.sh    # Run WebSocket version (recommended)"
echo ""
