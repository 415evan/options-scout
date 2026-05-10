const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const log = require('electron-log');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const fs = require('fs');

// ── Logging ──────────────────────────────────────────────────────────────────
log.transports.file.level = 'info';
autoUpdater.logger = log;
autoUpdater.autoDownload = true;
autoUpdater.autoInstallOnAppQuit = true;

let mainWindow = null;
let pythonProc = null;
let backendPort = 0;

// ── Find a free port ─────────────────────────────────────────────────────────
function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

// ── Wait for backend to respond ──────────────────────────────────────────────
function waitForBackend(port, timeoutMs = 15000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/health', timeout: 800 }, res => {
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) reject(new Error('Backend did not start in time'));
      else setTimeout(tick, 300);
    };
    tick();
  });
}

// ── Start Python backend ─────────────────────────────────────────────────────
async function startBackend() {
  backendPort = await findFreePort();

  const isPackaged = app.isPackaged;
  let cmd, args, cwd;

  if (isPackaged) {
    // Production: use bundled standalone binary (Python embedded inside)
    const binDir = path.join(process.resourcesPath, 'backend-bin');
    const exeName = process.platform === 'win32' ? 'optionsscout-backend.exe' : 'optionsscout-backend';
    cmd = path.join(binDir, exeName);
    args = [];
    cwd = binDir;
  } else {
    // Dev: use system python3
    const backendDir = path.join(__dirname, '..', 'backend');
    const candidates = ['/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3', 'python3'];
    cmd = candidates.find(p => { try { return fs.existsSync(p); } catch { return false; } }) || 'python3';
    args = [path.join(backendDir, 'app.py')];
    cwd = backendDir;
  }

  log.info(`Starting backend: ${cmd} ${args.join(' ')} on port ${backendPort}`);

  pythonProc = spawn(cmd, args, {
    env: { ...process.env, PORT: String(backendPort), PYTHONUNBUFFERED: '1' },
    cwd,
  });
  pythonProc.stdout.on('data', d => log.info('[py]', d.toString().trim()));
  pythonProc.stderr.on('data', d => log.info('[py-err]', d.toString().trim()));
  pythonProc.on('exit', code => { log.warn('Python exited:', code); pythonProc = null; });

  await waitForBackend(backendPort);
  log.info('Backend healthy on port', backendPort);
}

// ── Window creation ──────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 860,
    minWidth: 860,
    minHeight: 640,
    backgroundColor: '#0d1117',
    titleBarStyle: 'hiddenInset',
    title: 'Options Scout',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ── Auto-updater wiring ──────────────────────────────────────────────────────
function setupAutoUpdater() {
  if (!app.isPackaged) {
    log.info('Auto-updates disabled in dev mode');
    return;
  }

  autoUpdater.on('update-available', info => {
    log.info('Update available:', info.version);
    if (mainWindow) mainWindow.webContents.send('update-available', { version: info.version });
  });
  autoUpdater.on('update-downloaded', info => {
    log.info('Update downloaded:', info.version);
    if (mainWindow) mainWindow.webContents.send('update-downloaded', { version: info.version });
  });
  autoUpdater.on('error', err => log.error('Updater error:', err));

  // Initial check, then every 6 hours
  autoUpdater.checkForUpdatesAndNotify().catch(e => log.warn('Initial update check failed:', e.message));
  setInterval(() => {
    autoUpdater.checkForUpdatesAndNotify().catch(e => log.warn('Periodic update check failed:', e.message));
  }, 6 * 60 * 60 * 1000);
}

// ── IPC ──────────────────────────────────────────────────────────────────────
ipcMain.handle('app:get-version', () => app.getVersion());
ipcMain.handle('updater:install', () => {
  log.info('User triggered install');
  if (pythonProc) {
    try { pythonProc.kill('SIGTERM'); } catch {}
    pythonProc = null;
  }
  try {
    autoUpdater.quitAndInstall(true, true);
  } catch (e) {
    log.error('quitAndInstall failed, opening releases page:', e);
    shell.openExternal('https://github.com/415evan/options-scout/releases/latest');
  }
});

ipcMain.handle('app:open-releases', () => {
  shell.openExternal('https://github.com/415evan/options-scout/releases/latest');
});

// ── App lifecycle ────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  try {
    await startBackend();
  } catch (e) {
    log.error('Backend failed to start:', e);
    dialog.showErrorBox(
      'Options Scout — Backend Error',
      'Could not start the Python backend. Make sure Python 3 is installed:\n\nbrew install python3\n\nThen install dependencies:\n\npython3 -m pip install flask yfinance pandas numpy\n\nDetails: ' + e.message
    );
    app.quit();
    return;
  }
  createWindow();
  setupAutoUpdater();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (pythonProc) {
    log.info('Killing python process');
    try { pythonProc.kill('SIGTERM'); } catch {}
  }
});
