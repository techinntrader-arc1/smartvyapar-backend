/**
 * SmartVyapar - Hostinger Node.js Hosting Bridge
 * Auto-creates .venv virtualenv, installs Python requirements, and runs FastAPI backend.
 */

const http = require('http');
const { spawn, execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const PORT = parseInt(process.env.PORT || '3000', 10);
const PYTHON_PORT = 8765;

const venvDir = path.join(__dirname, '.venv');
const venvBin = process.platform === 'win32' ? path.join(venvDir, 'Scripts') : path.join(venvDir, 'bin');
const venvPython = process.platform === 'win32' ? path.join(venvBin, 'python.exe') : path.join(venvBin, 'python3');
const venvPip = process.platform === 'win32' ? path.join(venvBin, 'pip.exe') : path.join(venvBin, 'pip');

let isShuttingDown = false;
let pythonProcess = null;
let lastPythonStderr = '';
let setupStatus = 'Initializing...';

console.log('[Hostinger Bridge] Public Port:', PORT, '| Internal Port:', PYTHON_PORT);

function ensurePythonPackages() {
  try {
    if (!fs.existsSync(venvPython)) {
      console.log('[Hostinger Bridge] Creating isolated Python virtualenv (.venv)...');
      setupStatus = 'Creating Python virtual environment...';
      execSync('python3 -m venv .venv || python -m venv .venv', { cwd: __dirname, stdio: 'inherit' });
    }

    console.log('[Hostinger Bridge] Installing/verifying requirements in .venv...');
    setupStatus = 'Installing requirements in .venv...';
    execSync(`"${venvPip}" install --no-cache-dir -r requirements.txt`, {
      cwd: __dirname,
      stdio: 'inherit',
      timeout: 300000
    });
    console.log('[Hostinger Bridge] Python packages ready in .venv.');
    setupStatus = 'Ready';
    return true;
  } catch (err) {
    console.warn('[Hostinger Bridge] Virtualenv setup notice:', err.message);
    try {
      console.log('[Hostinger Bridge] Trying direct system pip install fallback...');
      execSync('pip3 install --break-system-packages --no-cache-dir -r requirements.txt || pip install -r requirements.txt', {
        cwd: __dirname,
        stdio: 'inherit',
        timeout: 300000
      });
      setupStatus = 'Ready (System Pip)';
      return true;
    } catch (e2) {
      lastPythonStderr = 'Package installation failed: ' + err.message + ' | Fallback: ' + e2.message;
      return false;
    }
  }
}

// Ensure packages before starting
ensurePythonPackages();

function startPythonBackend() {
  if (isShuttingDown) return;

  const pythonToUse = fs.existsSync(venvPython) ? venvPython : (process.platform === 'win32' ? 'python' : 'python3');
  console.log(`[Hostinger Bridge] Starting FastAPI backend with: ${pythonToUse}`);

  const envPath = fs.existsSync(venvBin) ? `${venvBin}${path.delimiter}${process.env.PATH || ''}` : process.env.PATH;

  pythonProcess = spawn(
    pythonToUse,
    ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PYTHON_PORT)],
    {
      cwd: __dirname,
      env: {
        ...process.env,
        VIRTUAL_ENV: fs.existsSync(venvDir) ? venvDir : undefined,
        PATH: envPath,
        SV_PORT: String(PYTHON_PORT),
        SV_HOST: '127.0.0.1',
        PYTHONUNBUFFERED: '1'
      }
    }
  );

  pythonProcess.stdout.on('data', (data) => {
    process.stdout.write('[FastAPI] ' + data.toString());
  });

  pythonProcess.stderr.on('data', (data) => {
    const errText = data.toString();
    process.stderr.write('[FastAPI Log] ' + errText);
    lastPythonStderr += errText;
    if (lastPythonStderr.length > 4000) {
      lastPythonStderr = lastPythonStderr.slice(-4000);
    }
  });

  pythonProcess.on('exit', (code, signal) => {
    console.warn(`[Hostinger Bridge] Python process exited (code=${code}, signal=${signal})`);
    if (!isShuttingDown) {
      console.log('[Hostinger Bridge] Restarting Python in 4 seconds...');
      setTimeout(startPythonBackend, 4000);
    }
  });
}

// Start FastAPI Backend
startPythonBackend();

// Reverse Proxy
const server = http.createServer((req, res) => {
  const options = {
    hostname: '127.0.0.1',
    port: PYTHON_PORT,
    path: req.url,
    method: req.method,
    headers: req.headers
  };

  const proxyReq = http.request(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });
  });

  proxyReq.on('error', (err) => {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'starting',
      setup_step: setupStatus,
      message: 'FastAPI Backend is initializing. Please refresh in a few seconds.',
      detail: err.message,
      python_diagnostic: lastPythonStderr.trim() || 'Booting uvicorn server...'
    }, null, 2));
  });

  req.pipe(proxyReq, { end: true });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[Hostinger Bridge] Listening on public port ${PORT}`);
});

function gracefulShutdown() {
  isShuttingDown = true;
  if (pythonProcess) {
    try { pythonProcess.kill('SIGTERM'); } catch (e) {}
  }
  server.close(() => process.exit(0));
}

process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);
