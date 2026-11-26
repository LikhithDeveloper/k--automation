$ErrorActionPreference = 'Stop'

function Get-Servers($Pattern) {
    Get-ChildItem Env: |
        Where-Object { $_.Name -cmatch $Pattern } |
        Sort-Object { [int]($_.Name -replace '^\D+', '0') } |
        ForEach-Object { $_.Value.Trim() }
}

function Invoke-Sudo($Server, $Command, [switch]$AllowDisconnect) {
    Write-Host "[$Server] $Command"
    $output = & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 `
        -o ServerAliveInterval=10 -o ServerAliveCountMax=2 -l $env:SSH_USER -- $Server "sudo -n -u root -- $Command"

    if ($AllowDisconnect -and $LASTEXITCODE -eq 255) { return }
    if ($LASTEXITCODE -ne 0) { throw "[$Server] $Command failed with exit code $LASTEXITCODE" }
    $output
}

$proxies = @(Get-Servers '^PROXY_?[1-9][0-9]*$')
$webApps = @(Get-Servers '^WEB_APP_[1-9][0-9]*$')
$databases = @(Get-Servers '^DB(?:_?[1-9][0-9]*)?$')
$shutdownOrder = $proxies + $webApps + $databases
$startupOrder = $databases + $webApps + $proxies

if (!$proxies.Count -or !$webApps.Count -or !$databases.Count) {
    throw 'Set at least one proxy, web app, and database server variable.'
}
if ($env:SSH_USER -notmatch '^[a-zA-Z_][a-zA-Z0-9_.-]*$') {
    throw 'Set SSH_USER to the SSH account with passwordless sudo access.'
}
if (@($shutdownOrder -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.:-]*$').Count -or
    @($shutdownOrder | Sort-Object -Unique).Count -ne $shutdownOrder.Count) {
    throw 'Use unique server hostnames or IP addresses.'
}

$expected = @{}
foreach ($server in $shutdownOrder) {
    Write-Host "[$server] Stopping Kasm and rebooting"
    $expected[$server] = @(Invoke-Sudo $server 'docker ps -a --format "{{.Names}}"')
    if (!$expected[$server].Count) { throw "[$server] No Docker containers found" }

    Invoke-Sudo $server '/opt/kasm/bin/stop' | Write-Host
    while (Invoke-Sudo $server 'docker ps -a --format "{{.State}}"' |
            Where-Object { $_ -notin @('created', 'exited', 'dead') }) {
        Start-Sleep -Seconds 20
    }

    # Keep Docker off across reboot until this server's startup turn.
    Invoke-Sudo $server 'systemctl mask docker.service docker.socket' | Write-Host
    $oldBoot = Invoke-Sudo $server 'cat /proc/sys/kernel/random/boot_id'
    Invoke-Sudo $server 'systemctl --no-block reboot' -AllowDisconnect | Write-Host
    $deadline = (Get-Date).AddMinutes(10)
    do {
        if ((Get-Date) -ge $deadline) { throw "[$server] Reboot did not complete within 10 minutes" }
        Start-Sleep -Seconds 10
        $newBoot = Invoke-Sudo $server 'cat /proc/sys/kernel/random/boot_id' -AllowDisconnect
    } until ($newBoot -match '^[0-9a-f-]{36}$' -and $newBoot -ne $oldBoot)
}

foreach ($server in $startupOrder) {
    Write-Host "[$server] Starting Docker and Kasm"
    Invoke-Sudo $server 'systemctl unmask docker.service docker.socket' | Write-Host
    Invoke-Sudo $server 'systemctl start docker' | Write-Host
    Invoke-Sudo $server '/opt/kasm/bin/start' | Write-Host

    while ($true) {
        $names = @()
        $ready = $true
        foreach ($line in Invoke-Sudo $server 'docker ps -a --format "{{.Names}}|{{.State}}|{{.Status}}"') {
            $name, $state, $status = $line -split '\|', 3
            $names += $name
            Write-Host "[$server] $name $status"
            if ($state -ne 'running' -or $status -match '\((unhealthy|health: [^)]+)\)') {
                $ready = $false
            }
        }
        $missing = $expected[$server] | Where-Object { $_ -notin $names }
        if ($ready -and !$missing) { break }
        Start-Sleep -Seconds 30
    }
}
