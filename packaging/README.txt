Mino Scout installer (zip)

Frozen payload (GitHub Release): layered. No Python on the machine.
Source payload (dev pack without --binary): needs Python 3.12+ and a venv.

Layers. This zip carries whatever layer directories are present next to
this file; layers it does not carry are left untouched on disk.

  runtime/   -> <prefix>/bin/mino-scout + _internal/   (deps + Python)
  app/       -> <prefix>/bin/app/mino_scout/           (Scout's own code)
  browser/   -> <prefix>/bin/ms-playwright/            (Chromium)

layers.txt at the root of this zip says which layers it carries and their
fingerprints. The installed state lives in <prefix>/bin/layers.txt.

A full install ships all three (about 439 MB). A code-only update ships
just the app layer (about 90 KB) and never rewrites the 780 MB browser
layer. An app-only zip is refused if its requires_runtime does not match
the runtime fingerprint already installed - use the combined zip then.

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

Playwright Chromium ships inside the browser layer; nothing to install
by hand. PLAYWRIGHT_BROWSERS_PATH points at <prefix>/bin/ms-playwright.

MINO_SCOUT_SKIP_SERVICE=1 installs the payload without registering a
daemon (CI and self-tests).

Uninstall (macOS, user install):
  ~/Library/Application Support/MinoScout/bin/mino-scout stop
  launchctl unload ~/Library/LaunchAgents/com.mino.scout.plist
  rm ~/Library/LaunchAgents/com.mino.scout.plist
  rm -rf ~/Library/Application\ Support/MinoScout
