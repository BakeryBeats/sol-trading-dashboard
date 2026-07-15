# Sol Trading Dashboard

Live dashboard voor de SOL/perp trading bot. P&L, scanner-signalen en HL-account.

- **Lokaal:** `dashboard.html` (auto-refresh 30s) of `dashboard_preview.png`
- **Online:** GitHub Pages (private repo, alleen eigenaar ziet 't)

## Strategie
Wacht op bevestigde edge (vol-breakout + momentum + funding-tailwind), dan SOL/WIF/BONK perp 5x.
In/uit markt, -40% stop, +100% TP, kill-switch 50% drawdown. PAPER tot LIVE_PERPS=1.

## Update
De cron (elke 15 min) pusht `dashboard.html` + `state.json` + `dashboard_preview.png` hiernaartoe.
