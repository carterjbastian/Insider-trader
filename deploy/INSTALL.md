# Install the daily timer (needs sudo — run once)
sudo cp deploy/insider-trader-daily.service deploy/insider-trader-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insider-trader-daily.timer
# verify:  systemctl list-timers insider-trader-daily.timer
# no-sudo alternative (cron):  (crontab -l 2>/dev/null; echo '0 11 * * * cd /home/carter/code/insider-trader && set -a && . /home/carter/.config/claude-channels/black-box.env && set +a && /home/carter/.local/bin/uv run python -m insider_trader.daily >> /home/carter/insider-daily.log 2>&1') | crontab -
