# Install / update the daily timer (needs sudo — re-run after any unit change)
sudo cp deploy/insider-trader-daily.service deploy/insider-trader-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart insider-trader-daily.timer
# verify:  systemctl list-timers insider-trader-daily.timer   (NEXT should be 06:15 PT = 13:15 UTC in summer)
# Runs at 06:15 America/Los_Angeles — OnCalendar tracks DST automatically, so leave it as the TZ name.
# no-sudo alternative (cron — cron has no TZ: 13:15 UTC = 06:15 PDT summer / 14:15 UTC = 06:15 PST winter):
#   (crontab -l 2>/dev/null; echo '15 13 * * * cd /home/carter/code/insider-trader && set -a && . /home/carter/.config/claude-channels/black-box.env && set +a && /home/carter/.local/bin/uv run python -m insider_trader.daily >> /home/carter/insider-daily.log 2>&1') | crontab -
