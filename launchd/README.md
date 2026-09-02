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

Run it by hand (fires the real thing, including the ntfy alert on failure):

    launchctl kickstart -k gui/$(id -u)/com.shoonya.eodhousekeeping

Dry test without alerting the operator's phone:

    SHOONYA_CRED_FILE=/dev/null ./eod_housekeeping.sh

Log: `logs/eod_housekeeping.log` (gitignored).
