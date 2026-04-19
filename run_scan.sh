#!/bin/bash
# Daily Kalshi v2 scan runner
# Cron: 0 13 * * *  (UTC = 6AM PT during PDT)
# Logs to /var/log/kalshi-scanner-v2.log
# .env loaded from /root/kalshi-scanner-v2/.env

set -e

cd /root/kalshi-scanner-v2

# Load env
set -a
source .env
set +a

# Run scan with default config (50 markets, 14 days max)
PYTHONPATH=. python3 -c "
import logging, json
logging.basicConfig(level=logging.INFO, format='%(message)s')
from scanner.score import run_scan
result = run_scan()
print(json.dumps({
    'scan_id': result.scan_id,
    'considered': result.candidates_considered,
    'selected': result.candidates_selected,
    'preds_by_model': result.predictions_by_model,
    'failures_by_model': result.failures_by_model,
    'alert_counts': result.alert_counts,
    'telegram': result.telegram_result,
    'elapsed_s': round(result.elapsed_seconds, 1),
    'predictions_path': result.predictions_path,
    'alerts_path': result.alerts_path,
}))
"
