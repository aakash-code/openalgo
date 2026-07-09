# Survivor Strategy Comparison

This report compares the **Naked (Original)** version of the Survivor strategy against the **Hedged (Credit Spread)** version.

## Executive Summary (Oct 2024 - Apr 2026)

| Metric | Naked Version (Original) | Hedged Version (Pro) | Improvement |
| :--- | :--- | :--- | :--- |
| **Total Net Profit** | Rs 65.29 Lakhs | Rs 47.99 Lakhs | Hedged is -26% less profit |
| **Peak Margin Required** | Rs 2.36 Crore | Rs 0.87 Crore | **Hedged uses 63% LESS capital** |
| **ROI (on Peak Margin)** | **27.61%** | **55.16%** | **Hedged ROI is 2x Higher** |
| **Max Risk per Trade** | Unlimited (Short leg only) | **Fixed** (Spread Width) | Hedged is significantly safer |
| **Recovery Factor** | High | Ultra High | Hedged survives gaps better |

## Key Insights

1. **Capital Efficiency:** While the Naked version makes more absolute profit (65L vs 48L), it requires a massive **Rs 2.36 Crore** in margin. The Hedged version achieves a very healthy **48L profit** using only **Rs 87 Lakhs**.
2. **Return on Investment:** The Hedged version is twice as efficient. You get a **55% ROI** compared to **27%** in the naked version.
3. **Safety (The "Survivor" Goal):** The Naked version is vulnerable to "limit-up/limit-down" days where the short option price can jump instantly. The Hedged version's long leg acts as an insurance policy, capping your loss regardless of how fast the market moves.

## Recommendations

- **For Large Accounts:** The Hedged version allows you to trade more quantity with the same capital, likely yielding higher absolute profits than the Naked version if position sizes are normalized.
- **For Risk Management:** The Hedged version is the strictly superior choice for "surviving" black swan events.

---

### Folder Structure
- `1_NAKED_ORIGINAL/`: Contains the original naked-selling logic.
- `2_HEDGED_PRO/`: Contains the modified Credit Spread logic.
