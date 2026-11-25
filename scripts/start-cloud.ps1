$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# PowerShell launched through WSL can inherit PATHEXT without .EXE.
if (($env:PATHEXT -split ';') -notcontains '.EXE') {
    $env:PATHEXT += ';.EXE'
}
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$milvusPort = 19530
$attuPort = 8000
$webPort = 9900
$python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The Windows virtual environment is missing. Set up .venv before launching.'
}

$target = $env:ONCALL_MILVUS_SSH_TARGET
if (-not $target -and (Test-Path -LiteralPath '.env' -PathType Leaf)) {
    $setting = Select-String -LiteralPath '.env' -Pattern '^ONCALL_MILVUS_SSH_TARGET=(.+)$' | Select-Object -Last 1
    if ($setting) {
        $target = $setting.Matches[0].Groups[1].Value.Trim()
    }
}
if ($target -notmatch '\A[a-zA-Z0-9_.-]+@[a-zA-Z0-9][a-zA-Z0-9.-]*\z') {
    throw 'Set ONCALL_MILVUS_SSH_TARGET=user@host in .env.'
}

function Get-Listeners($Port) {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
}

if (Get-Listeners $webPort) {
    throw "Local port $webPort is already in use. Stop the existing server before starting another."
}

$milvusForward = "localhost:${milvusPort}:127.0.0.1:19530"
$attuForward = "localhost:${attuPort}:127.0.0.1:8000"
$ownedTunnel = $null
try {
    $listeners = @(Get-Listeners $milvusPort)
    if ($listeners) {
        $expectedForward = '(?:^|\s)"?' + [regex]::Escape($milvusForward) + '"?(?:\s|$)'
        $expectedTarget = '(?:^|\s)"?' + [regex]::Escape($target) + '"?(?:\s|$)'
        foreach ($owner in ($listeners.OwningProcess | Sort-Object -Unique)) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $owner"
            if ($process.Name -ine 'ssh.exe' -or $process.CommandLine -notmatch $expectedForward -or
                $process.CommandLine -notmatch $expectedTarget) {
                throw "Local port $milvusPort is occupied by a different process or SSH forward."
            }
        }
        Write-Host "Reusing the Milvus SSH tunnel on port $milvusPort."
    } else {
        if (Get-Listeners $attuPort) {
            throw "Local Attu port $attuPort is already in use."
        }
        $sshArguments = @(
            '-F', 'none', '-N', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-L', $milvusForward, '-L', $attuForward, $target
        )
        Write-Host "Opening the Milvus SSH tunnel to $target."
        $sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
        $ready = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $ownedTunnel = Start-Process -FilePath $sshExe -ArgumentList $sshArguments -WindowStyle Hidden -PassThru
            $deadline = (Get-Date).AddSeconds(15)
            while (-not $ownedTunnel.HasExited -and (Get-Date) -lt $deadline) {
                if (Get-Listeners $milvusPort | Where-Object OwningProcess -eq $ownedTunnel.Id) {
                    $ready = $true
                    break
                }
                Start-Sleep -Milliseconds 300
            }
            if ($ready -or -not $ownedTunnel.HasExited) {
                break
            }
        }
        if (-not $ready) {
            throw 'The SSH tunnel could not bind the local Milvus port.'
        }
    }

    $env:MILVUS_HOST = '127.0.0.1'
    $env:MILVUS_PORT = [string]$milvusPort
    & $python -X utf8 -c "from pymilvus import MilvusClient; MilvusClient('http://127.0.0.1:$milvusPort', timeout=5).list_collections(timeout=5)"
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not connect to Milvus through the SSH tunnel.'
    }

    & $python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port $webPort --reload
    exit $LASTEXITCODE
} finally {
    if ($ownedTunnel -and -not $ownedTunnel.HasExited) {
        Stop-Process -Id $ownedTunnel.Id -ErrorAction SilentlyContinue
    }
}
