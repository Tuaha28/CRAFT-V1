# Start the CRAFT V3 planner on this computer: API (8001), worker and web page (5173).
#   powershell -ExecutionPolicy Bypass -File web_v1\start_planner.ps1        (fast production build)
#   powershell -ExecutionPolicy Bypass -File web_v1\start_planner.ps1 -Dev   (development: reloads on code changes)
# First run: creates a local Django secret (web_v1\backend\.planner_secret, git-ignored)
# and applies the database migrations. Create a user once with:
#   web_v1\backend\.venv\Scripts\python.exe web_v1\backend\manage.py planning_setup --user NAME --role manager --password-file web_v1\backend\.planner_users.local
param([switch]$Dev)
$ErrorActionPreference = 'Stop'
$web = $PSScriptRoot
$backend = Join-Path $web 'backend'
$repo = Split-Path $web -Parent
$python = Join-Path $backend '.venv\Scripts\python.exe'
$scheduler = Join-Path $repo 'venv\Scripts\python.exe'
foreach ($path in @($python, $scheduler)) {
    if (-not (Test-Path $path)) { throw "Missing $path. See web_v1\README.md, section 'CRAFT V3 planner'." }
}
$secretFile = Join-Path $backend '.planner_secret'
if (-not (Test-Path $secretFile)) {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -NoNewline -Encoding ascii $secretFile
}
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
$env:DJANGO_SECRET_KEY = (Get-Content -Raw $secretFile).Trim()
$env:CRAFT_WORKER_PYTHON = $scheduler
& $python (Join-Path $backend 'manage.py') migrate --noinput
& $python (Join-Path $backend 'manage.py') planning_setup

$setup = "`$env:DJANGO_SETTINGS_MODULE='config.settings_integration'; `$env:DJANGO_SECRET_KEY=(Get-Content -Raw '$secretFile').Trim(); `$env:CRAFT_WORKER_PYTHON='$scheduler';"
Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command', "$setup & '$python' manage.py runserver 127.0.0.1:8001"
Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command', "$setup & '$python' -u manage.py planning_worker"
# The web page: a production build is much faster (bundled, minified); -Dev keeps the reloading dev server.
$page = if ($Dev) { 'npm run dev' } else { 'npx vite build; if ($?) { npx vite preview }' }
Start-Process powershell -WorkingDirectory (Join-Path $web 'frontend') -ArgumentList '-NoExit', '-Command', $page
# The customer portal's assistant (web_v1\agent), when its environment exists (see README, Customer portal).
$agentPython = Join-Path $web 'agent\.venv\Scripts\python.exe'
if (Test-Path $agentPython) {
    & $python (Join-Path $backend 'manage.py') run_agent --init
    Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command', "$setup & '$python' manage.py run_agent --backend http://127.0.0.1:8001"
}
Write-Host 'CRAFT planner starting: open http://127.0.0.1:5173 in a minute.'
