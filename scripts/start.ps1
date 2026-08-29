param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$botRoot = Split-Path -Parent $PSScriptRoot
$botEntry = Join-Path $botRoot "bot.py"
$requirements = Join-Path $botRoot "requirements.txt"
$projectVenv = Join-Path $botRoot ".venv"
$projectPython = Join-Path $botRoot ".venv\Scripts\python.exe"
$legacyPython = Join-Path $botRoot "venv\Scripts\python.exe"
$napCatRoot = Join-Path (Split-Path -Parent $botRoot) "NapCat.Shell"
$napCatLauncher = Join-Path $napCatRoot "launcher-user.bat"

function Test-Python {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }

    try {
        $version = & $Path --version 2>&1
        return $LASTEXITCODE -eq 0 -and "$version" -match "^Python 3\."
    }
    catch {
        return $false
    }
}

function Test-BotDependencies {
    param([string]$PythonPath)

    try {
        & $PythonPath -c "import nonebot; import nonebot.adapters.onebot.v11" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-DotEnvValue {
    param([string]$Name)

    $pattern = "^\s*$([regex]::Escape($Name))\s*=(.*)$"
    foreach ($line in Get-Content -LiteralPath (Join-Path $botRoot ".env")) {
        if ($line -notmatch $pattern) {
            continue
        }

        $value = $matches[1].Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
    }

    return $null
}

function Confirm-OneBotConfiguration {
    $portText = Get-DotEnvValue "PORT"
    $botPort = 8080
    if ($portText -and -not [int]::TryParse($portText, [ref]$botPort)) {
        throw "Invalid PORT value in .env: $portText"
    }

    $matchingClient = $null
    $configDirectory = Join-Path $napCatRoot "config"
    foreach ($configFile in Get-ChildItem -LiteralPath $configDirectory -Filter "onebot11_*.json" -File -ErrorAction SilentlyContinue) {
        try {
            $config = Get-Content -Raw -LiteralPath $configFile.FullName | ConvertFrom-Json
            foreach ($client in $config.network.websocketClients) {
                if (-not $client.enable) {
                    continue
                }

                $uri = [Uri]$client.url
                if ($uri.Port -eq $botPort) {
                    $matchingClient = $client
                    break
                }
            }
        }
        catch {
            throw "Invalid NapCat OneBot configuration: $($configFile.FullName)"
        }

        if ($matchingClient) {
            break
        }
    }

    if (-not $matchingClient) {
        throw "No enabled NapCat reverse WebSocket client targets bot port $botPort."
    }

    $botToken = Get-DotEnvValue "ONEBOT_ACCESS_TOKEN"
    $napCatToken = "$($matchingClient.token)"

    if ($botToken -and $napCatToken -and $botToken -ne $napCatToken) {
        throw "ONEBOT_ACCESS_TOKEN does not match the enabled NapCat reverse WebSocket client."
    }

    if (-not $botToken -and $napCatToken) {
        # Environment variables override .env and are inherited by the bot process.
        # This avoids duplicating the NapCat secret into another file.
        $env:ONEBOT_ACCESS_TOKEN = $napCatToken
        Write-Host "[OK] OneBot access token inherited from NapCat." -ForegroundColor Green
    }
    elseif ($botToken -and -not $napCatToken) {
        throw "NapCat has no token, but ONEBOT_ACCESS_TOKEN is set in .env."
    }
    elseif (-not $botToken -and -not $napCatToken) {
        Write-Host "[WARNING] OneBot authentication is disabled on both sides." -ForegroundColor Yellow
    }
    else {
        Write-Host "[OK] OneBot access tokens match." -ForegroundColor Green
    }

    Write-Host "[OK] OneBot reverse WebSocket port: $botPort" -ForegroundColor Green
}

function Get-PythonCandidates {
    $candidates = [System.Collections.Generic.List[string]]::new()

    $candidates.Add($projectPython)
    $candidates.Add($legacyPython)

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $launchedPython = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $launchedPython) {
                $candidates.Add("$launchedPython".Trim())
            }
        }
        catch {
            # Continue checking the remaining known locations.
        }
    }

    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe" -ErrorAction SilentlyContinue |
        ForEach-Object { $candidates.Add($_.FullName) }

    # Codex Desktop includes Python on development machines. This is a last-resort
    # bootstrap source; the bot still runs from its own project virtual environment.
    $codexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $candidates.Add($codexPython)

    return $candidates | Select-Object -Unique
}

function Resolve-BotPython {
    $basePython = $null

    foreach ($candidate in Get-PythonCandidates) {
        if (-not (Test-Python $candidate)) {
            continue
        }

        if (-not $basePython) {
            $basePython = $candidate
        }

        if (Test-BotDependencies $candidate) {
            return $candidate
        }
    }

    if (-not $basePython) {
        throw "Python 3 was not found. Install Python 3.10 or newer and enable Add Python to PATH."
    }

    if ($Check) {
        Write-Host "[PENDING] The first start will create .venv and install dependencies." -ForegroundColor Yellow
        return $basePython
    }

    if ([IO.Path]::GetFullPath($basePython) -ne [IO.Path]::GetFullPath($projectPython)) {
        Write-Host "[SETUP] Creating the project virtual environment..." -ForegroundColor Cyan
        & $basePython -m venv --clear $projectVenv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Python $projectPython)) {
            throw "Failed to create the project virtual environment."
        }
    }

    Write-Host "[SETUP] Installing Python dependencies..." -ForegroundColor Cyan
    & $projectPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0 -or -not (Test-BotDependencies $projectPython)) {
        throw "Failed to install Python dependencies. Check the network and try again."
    }

    return $projectPython
}

try {
    Write-Host "=== AI QQ Bot Launcher ===" -ForegroundColor Cyan

    if (-not (Test-Path -LiteralPath $botEntry -PathType Leaf)) {
        throw "Bot entry point not found: $botEntry"
    }
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "Requirements file not found: $requirements"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $botRoot ".env") -PathType Leaf)) {
        throw "Configuration file not found: $(Join-Path $botRoot '.env')"
    }
    if (-not (Test-Path -LiteralPath $napCatLauncher -PathType Leaf)) {
        throw "NapCat launcher not found: $napCatLauncher"
    }

    Confirm-OneBotConfiguration
    $botPython = Resolve-BotPython

    Write-Host "[OK] Python: $botPython" -ForegroundColor Green
    Write-Host "[OK] NapCat: $napCatLauncher" -ForegroundColor Green

    if ($Check) {
        Write-Host "Check complete. No processes were started." -ForegroundColor Green
        exit 0
    }

    Write-Host "Starting the bot..."
    Start-Process -FilePath $botPython -ArgumentList @($botEntry) -WorkingDirectory $botRoot

    Start-Sleep -Seconds 2

    Write-Host "Starting NapCat..."
    Start-Process -FilePath $napCatLauncher -WorkingDirectory $napCatRoot

    Write-Host "Both start commands were sent. Keep the bot and NapCat windows open." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
