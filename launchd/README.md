# launchd agents (Mac-side)

`com.shoonya.eodhousekeeping.plist` — runs `eod_housekeeping.sh` at 00:20 IST,
Tue–Sat, covering each Mon–Fri trading night (the job belongs to the trading day
that just ENDED, so it fires on the following calendar day).

Kept in the repo because the live copy lives in `~/Library/LaunchAgents/`, which
is untracked and machine-local. The whole reason `eod_housekeeping.sh` exists is
that the backup lost its only trigger and nobody noticed for days; an automation
whose definition survives only on one laptop is the same failure waiting to
happen again.

Install / reinstall:

    cp launchd/com.shoonya.eodhousekeeping.plist ~/Library/LaunchAgents/
    launchctl unload ~/Library/LaunchAgents/com.shoonya.eodhousekeeping.plist 2>/dev/null
    launchctl load   ~/Library/LaunchAgents/com.shoonya.eodhousekeeping.plist
    launchctl list | grep eodhousekeeping        # confirm registered

Run it by hand:

    launchctl kickstart -k gui/$(id -u)/com.shoonya.eodhousekeeping

Run outside the 00:00–06:00 window and a live trading session is the EXPECTED
state, so the job reports, exits 2 and sends NO alert. Inside the window a live
session means the 23:58 shutdown failed, and that does page. Alerting on an
expected condition is how a channel gets muted.

To exercise the alerting path itself without pushing to the operator's phone,
point the cred file somewhere empty:

    SHOONYA_CRED_FILE=/dev/null ./eod_housekeeping.sh

Exit codes: 0 all steps OK · 1 at least one step failed (alert sent) · 2
declined to run, nothing done (no alert).

Log: `logs/eod_housekeeping.log` (gitignored).
