Mino Scout installer (zip)

Frozen payload (GitHub Release): copy the onedir. No Python on the machine.
Source payload (dev pack without --binary): needs Python 3.12+ and a venv.

Daemon:

  macOS   root → LaunchDaemon /Library/LaunchDaemons/com.mino.scout.plist
          user → LaunchAgent  ~/Library/LaunchAgents/com.mino.scout.plist
  Windows admin → Scheduled Task "Mino Scout" AtStartup / SYSTEM
          user  → AtLogOn (Startup folder fallback)
  Linux   root → systemd mino-scout.service
          user → systemd --user mino-scout.service

Mino Studio writes config.json next to the install (nexus_url + token)
before opening this package. Nexus never hosts these binaries.

Control:

  mino-scout status
  mino-scout stop
  mino-scout probe

Sleep: Scout only inhibits system sleep while a run is in flight
(caffeinate / SetThreadExecutionState / systemd-inhibit). Display sleep is
left alone. Dedicated machines should also turn sleep off in OS settings.

Playwright Chromium is optional and not inside the freeze:
  playwright install chromium

Uninstall (macOS, user install):
  ~/Library/Application Support/MinoScout/bin/mino-scout stop
  launchctl unload ~/Library/LaunchAgents/com.mino.scout.plist
  rm ~/Library/LaunchAgents/com.mino.scout.plist
  rm -rf ~/Library/Application\ Support/MinoScout
