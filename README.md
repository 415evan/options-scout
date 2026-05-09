# Options Scout

Find this week's best call options — ranked by key levels, support &
resistance, volume, and live news. Includes TradingView watchlist import
and silent auto-updates.

## Run in development

```bash
# 1. Install Python deps (one time)
python3 -m pip install -r backend/requirements.txt

# 2. Install Node deps (one time)
npm install

# 3. Launch the app
npm start
```

## TradingView watchlist import

1. In TradingView, open the watchlist menu (⋮) → **Export Watchlist**
2. Save the `.txt` file
3. In Options Scout, click **Watchlist** in the header (or drag the
   file into the dropzone on the welcome screen)
4. Every ticker is scanned and ranked by best call-option score

## Build a downloadable `.dmg`

```bash
npm run dist
# → release/Options Scout-1.0.0-arm64.dmg  (Apple Silicon)
# → release/Options Scout-1.0.0.dmg        (Intel)
```

Open the `.dmg`, drag the app into Applications, done.

> **Note:** The app is unsigned. On first launch, macOS will block it.
> Right-click the app → **Open** → **Open** to allow it through Gatekeeper.

## Publish an update (auto-updates everyone)

The app checks GitHub Releases on launch and every 6 hours. To ship
an update to all installed copies:

```bash
# 1. Bump the version in package.json (e.g. 1.0.0 → 1.0.1)
# 2. Make sure GH_TOKEN is exported (a GitHub personal access token
#    with `repo` scope; get one at github.com/settings/tokens)
export GH_TOKEN=ghp_yourtoken_here

# 3. Build + publish
npm run release
```

This will:
- Build new DMGs
- Create a GitHub draft release at `github.com/415evan/options-scout`
- Upload the DMGs + a `latest-mac.yml` manifest

Then in GitHub, click **Publish release**. Within seconds, every
running copy of the app will see the update badge appear in the
header. Click it to restart and apply.

## First-time GitHub setup (do this once)

```bash
cd ~/options-scout
git init
git add .
git commit -m "initial commit"
gh repo create 415evan/options-scout --public --source=. --push
```

If `gh` isn't installed: `brew install gh && gh auth login`.

## Project layout

```
options-scout/
├── backend/         Flask + yfinance API
│   ├── app.py
│   └── requirements.txt
├── frontend/        HTML/CSS/JS UI
├── electron/        Electron shell + auto-updater
│   ├── main.js
│   └── preload.js
├── package.json     Build + publish config
└── README.md
```

## Requirements on the user's machine

- macOS 11+
- Python 3.9+ (the app uses system `python3`)
- The Python packages from `backend/requirements.txt`. The app prompts
  the user to install them on first launch if missing.

If you want a fully self-contained DMG that bundles its own Python,
swap the `startBackend()` logic in `electron/main.js` to spawn a
PyInstaller-built binary instead — that's a future enhancement.
