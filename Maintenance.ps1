# Shared commands for the three PowerShell maintenance entry points.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$KasmBin = '/opt/kasm/bin'
$DockerUnits = @('docker.service', 'docker.socket')
$StateDir = '/var/lib/kasm-env0-maintenance'
$StateFile = "$StateDir/state.json"
$BootIdPattern = '^[0-9a-f-]{36}$'
$HostPatterns = [ordered]@{
    proxy  = '^PROXY_?([1-9][0-9]*)$'
    webapp = '^WEB_APP_([1-9][0-9]*)$'
    db     = '^DB(?:_?([1-9][0-9]*))?$'
}
$Secrets = @(Get-ChildItem Env: |
    Where-Object { $_.Name -cmatch '(_PASSWORD|_KEY_PASSPHRASE)$' -and $_.Value } |
    Sort-Object { $_.Value.Length } -Descending |
    ForEach-Object Value)
$WriteState = @'
set -eu
umask 077
temporary=$(mktemp "$2/state.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' "$1" > "$temporary"
mv -f -- "$temporary" "$3"
'@

function Write-MaintenanceLog($Context, $Message, $Level = 'INFO') {
    foreach ($secret in $Secrets) {
        $Message = $Message.Replace($secret, '[redacted]')
        $Context = $Context.Replace($secret, '[redacted]')
    }
    Write-Host "[$([DateTimeOffset]::UtcNow.ToString('o'))] $Level [$Context] $Message"
}

function Get-MaintenanceInventory {
    $groups = @{}
    $seen = @{}
    foreach ($role in $HostPatterns.Keys) {
        $servers = @{}
        foreach ($variable in Get-ChildItem Env:) {
            if ($variable.Name -cnotmatch $HostPatterns[$role]) { continue }
            $index = if ($Matches.ContainsKey(1)) { [long]$Matches[1] } else { 0L }
            $name = $variable.Name
            $hostname = $variable.Value.Trim()
            $port = 22
            if ($hostname -match '^([^:]+):([0-9]+)$') {
                $hostname = $Matches[1]
                $port = [int]$Matches[2]
            }
            $user = [Environment]::GetEnvironmentVariable("${name}_USER") ?? $env:SSH_USER
            if ($hostname -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.:-]*$' -or
                $user -notmatch '^[a-zA-Z_][a-zA-Z0-9_.-]*$' -or $port -lt 1 -or $port -gt 65535) {
                throw "Check the hostname, port and SSH user for $name"
            }
            $identity = $hostname.TrimEnd('.')
            if ($servers.ContainsKey($index) -or $seen.ContainsKey($identity)) {
                throw "Duplicate host or index in $name"
            }
            $password = [Environment]::GetEnvironmentVariable("${name}_PASSWORD")
            $sudoPassword = [Environment]::GetEnvironmentVariable("${name}_SUDO_PASSWORD") ?? $password
            if ($sudoPassword -match '[\r\n]' -or [Text.Encoding]::UTF8.GetByteCount([string]$sudoPassword) -gt 1024) {
                throw "Invalid sudo password length or newline for $name"
            }
            $servers[$index] = @{
                Hostname = $hostname
                Port = $port
                User = $user
                Password = $password
                SudoPassword = $sudoPassword
                KeyFile = [Environment]::GetEnvironmentVariable("${name}_KEY_FILE") ?? $env:SSH_KEY_FILE
                KeyPassphrase = [Environment]::GetEnvironmentVariable("${name}_KEY_PASSPHRASE") ?? $env:SSH_KEY_PASSPHRASE
            }
            $seen[$identity] = $true
        }
        if (!$servers.Count) { throw "Configure at least one $role server" }
        $groups[$role] = @($servers.Keys | Sort-Object | ForEach-Object { $servers[$_] })
    }
    return $groups
}

function Get-TimeoutSeconds($Name, $Default) {
    $value = [Environment]::GetEnvironmentVariable($Name) ?? $Default
    $seconds = 0
    if (![int]::TryParse($value, [ref]$seconds) -or $seconds -le 0) {
        throw "$Name must be a positive integer"
    }
    return $seconds
}

function Test-RebootDisconnect($Exception) {
    for ($cause = $Exception; $null -ne $cause; $cause = $cause.InnerException) {
        if ($cause -is [Net.Sockets.SocketException] -or
            $cause -is [Renci.SshNet.Common.SshOperationTimeoutException]) { return $true }
        if ($cause -is [Renci.SshNet.Common.SshConnectionException] -and
            $cause.DisconnectReason -in @('ConnectionLost', 'ByApplication')) { return $true }
    }
    return $false
}

function Invoke-RootCommand {
    param(
        $Server, [string]$Label, [string[]]$Command,
        [switch]$Capture, [switch]$Reconnect, [int[]]$AllowedExit = @()
    )

    $hostname = $Server.Hostname
    Write-MaintenanceLog $hostname $Label
    $secret = [Security.SecureString]::new()
    if ($Server.Password) { $secret = ConvertTo-SecureString $Server.Password -AsPlainText -Force }
    $options = @{
        ComputerName = $hostname
        Port = $Server.Port
        Credential = [pscredential]::new($Server.User, $secret)
        ConnectionTimeout = 10
        KeepAliveInterval = 10
        ErrorOnUntrusted = $true
        TrustedHostStore = $TrustedHosts
        ErrorAction = 'Stop'
    }
    if (!$Server.Password) {
        $key = $Server.KeyFile
        if (!$key) {
            $key = @('id_ed25519', 'id_ecdsa', 'id_rsa') |
                ForEach-Object { Join-Path $HOME ".ssh/$_" } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                Select-Object -First 1
        }
        if (!$key) { throw "Set SSH_KEY_FILE or provide an SSH password for $hostname" }
        $options.KeyFile = $key
        if ($Server.KeyPassphrase) {
            $options.Passphrase = ConvertTo-SecureString $Server.KeyPassphrase -AsPlainText -Force
        }
    }

    # Quote each shell argument. Send sudo credentials only over SSH stdin.
    $quoted = ($Command | ForEach-Object { "'" + $_.Replace("'", "'\''") + "'" }) -join ' '
    $sudo = if ($Server.SudoPassword) { "sudo -S -p '' -u root --" } else { 'sudo -n -u root --' }
    $session = $null
    $sshCommand = $null
    try {
        $session = New-SSHSession @options
        $sshCommand = $session.Session.CreateCommand("$sudo $quoted")
        $sshCommand.CommandTimeout = [TimeSpan]::FromSeconds(180)
        $task = $sshCommand.ExecuteAsync()
        if ($Server.SudoPassword -and !$task.IsCompleted) {
            try {
                $inputStream = $sshCommand.CreateInputStream()
                $bytes = [Text.Encoding]::UTF8.GetBytes($Server.SudoPassword + [char]10)
                try {
                    $inputStream.Write($bytes, 0, $bytes.Length)
                } finally {
                    [Array]::Clear($bytes, 0, $bytes.Length)
                    $inputStream.Dispose()
                }
            } catch {
                # A passwordless command can exit before its input stream is opened.
                if (!$task.IsCompleted -and $null -eq $sshCommand.ExitStatus) { throw }
            }
        }
        $null = $task.GetAwaiter().GetResult()
        $status = $sshCommand.ExitStatus
        $stderr = $sshCommand.Error.Trim()
        $stdout = $sshCommand.Result.Trim()
    } catch {
        $message = "${Label}: $($_.ToString())"
        if ($Reconnect -and (Test-RebootDisconnect $_.Exception)) {
            Write-MaintenanceLog $hostname $message 'WARNING'
            return $null
        }
        throw $message
    } finally {
        if ($null -ne $sshCommand) { $sshCommand.Dispose() }
        if ($null -ne $session) { Remove-SSHSession -SSHSession $session | Out-Null }
        $secret.Dispose()
    }

    if ($null -eq $status) {
        if (!$Reconnect) { throw "${Label}: SSH closed before returning an exit status. $stderr" }
        Write-MaintenanceLog $hostname "${Label}: SSH closed before returning an exit status. $stderr" 'WARNING'
        return $null
    }
    if ($stderr) { Write-MaintenanceLog $hostname $stderr 'WARNING' }
    if ($stdout -and (!$Capture -or $status -ne 0)) { Write-MaintenanceLog $hostname $stdout }
    if ($status -ne 0 -and $status -notin $AllowedExit) { throw "${Label}: exit $status" }
    Write-MaintenanceLog $hostname "${Label}: completed (exit $status)"
    if ($status -eq 0) { return $stdout }
    return $null
}

function Wait-ForCheck($Server, [scriptblock]$Check, $Interval, $Timeout, $Description) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $pending = & $Check
        if (!$pending) {
            Write-MaintenanceLog $Server.Hostname "Verified $Description"
            return
        }
        $delay = [Math]::Min([double]$Interval, $Timeout - $clock.Elapsed.TotalSeconds)
        if ($delay -le 0) { throw "Timed out waiting for $Description. $pending" }
        Write-MaintenanceLog $Server.Hostname "$pending. Checking again in $([Math]::Round($delay, 1))s"
        Start-Sleep -Seconds $delay
    }
}

function Get-ContainerStates($Server) {
    $ids = Invoke-RootCommand $Server 'List all Docker containers' @('docker', 'container', 'ls', '-aq') -Capture
    $states = [Collections.Generic.Dictionary[string, object]]::new()
    if ($ids) {
        $command = @('docker', 'container', 'inspect', '--format',
            '{"name":{{json .Name}},"state":{{json .State}}}') + ($ids -split '\s+')
        $records = Invoke-RootCommand $Server 'Inspect Docker container states' $command -Capture
        foreach ($line in $records -split '\r?\n') {
            $record = ConvertFrom-Json -InputObject $line -AsHashtable
            $states.Add($record.name.TrimStart('/'), $record.state)
        }
    }
    return ,$states
}

function Get-BootId($Server, [switch]$Reconnect) {
    $bootId = Invoke-RootCommand $Server 'Read boot ID' @('cat', '/proc/sys/kernel/random/boot_id') -Capture -Reconnect:$Reconnect
    if ($null -ne $bootId -and $bootId -cnotmatch $BootIdPattern) { throw 'Host returned an invalid boot ID' }
    return $bootId
}

function Get-MaintenanceState($Server) {
    $present = Invoke-RootCommand $Server 'Check for saved maintenance state' @('test', '-f', $StateFile) -AllowedExit 1
    if ($null -eq $present) { return $null }
    $raw = Invoke-RootCommand $Server 'Read saved maintenance state' @('cat', $StateFile) -Capture
    $state = ConvertFrom-Json -InputObject $raw -AsHashtable -NoEnumerate
    if ($state -isnot [Collections.IDictionary] -or $state['boot_id'] -isnot [string] -or
        $state['boot_id'] -cnotmatch $BootIdPattern -or $state['complete'] -isnot [bool] -or
        $state['containers'] -isnot [array] -or !$state['containers'].Count) {
        throw 'Invalid saved maintenance state'
    }
    foreach ($name in $state['containers']) {
        if ($name -isnot [string] -or !$name) { throw 'Invalid saved container name' }
    }
    return $state
}

function Save-MaintenanceState($Server, $State) {
    $null = Invoke-RootCommand $Server 'Prepare state directory' @('install', '-d', '-m', '0700', $StateDir)
    $json = ConvertTo-Json -InputObject $State -Compress
    $null = Invoke-RootCommand $Server 'Save maintenance state' @('sh', '-c', $WriteState,
        'write-state', $json, $StateDir, $StateFile)
}

function Confirm-DockerHeld($Server) {
    foreach ($unit in $DockerUnits) {
        $status = Invoke-RootCommand $Server "Verify $unit is inactive and masked" @(
            'systemctl', 'show', '--property=ActiveState,UnitFileState', $unit) -Capture
        $properties = $status -split '\r?\n'
        if ('ActiveState=inactive' -notin $properties -or 'UnitFileState=masked' -notin $properties) {
            throw "$unit must be inactive and masked after reboot"
        }
    }
}

function Stop-KasmServer($Server, $StopTimeout, $RebootTimeout) {
    $bootId = Get-BootId $Server
    $previous = Get-MaintenanceState $Server
    if ($previous -and !$previous.complete) {
        if ($bootId -eq $previous.boot_id) { throw 'An interrupted shutdown needs recovery before retrying' }
        Confirm-DockerHeld $Server
        Write-MaintenanceLog $Server.Hostname 'Earlier shutdown already rebooted this host'
        return
    }

    foreach ($action in @('start', 'stop')) {
        $null = Invoke-RootCommand $Server "Check Kasm $action script" @('test', '-x', "$KasmBin/$action")
    }
    foreach ($unit in $DockerUnits) {
        $status = Invoke-RootCommand $Server "Check $unit" @(
            'systemctl', 'show', '--property=LoadState,UnitFileState', $unit) -Capture
        $properties = $status -split '\r?\n'
        if ('LoadState=loaded' -notin $properties -or 'UnitFileState=masked' -in $properties) {
            throw "$unit must be installed and unmasked"
        }
    }
    $names = @((Get-ContainerStates $Server).Keys | Sort-Object)
    if (!$names.Count) { throw 'Docker has no containers to verify' }
    Save-MaintenanceState $Server @{ boot_id = $bootId; containers = $names; complete = $false }
    $null = Invoke-RootCommand $Server 'Stop Kasm' @("$KasmBin/stop")

    Wait-ForCheck $Server {
        $active = foreach ($entry in (Get-ContainerStates $Server).GetEnumerator()) {
            if ($entry.Value.Status -notin @('created', 'exited', 'dead')) {
                "$($entry.Key) ($($entry.Value.Status))"
            }
        }
        if ($active) { 'Containers still active: ' + ($active -join ', ') }
    } 20 $StopTimeout 'all containers have stopped'

    $null = Invoke-RootCommand $Server 'Mask Docker across reboot' (@('systemctl', 'mask') + $DockerUnits)
    $null = Invoke-RootCommand $Server 'Request reboot' @('systemctl', '--no-block', 'reboot') -Reconnect
    Wait-ForCheck $Server {
        $newBoot = Get-BootId $Server -Reconnect
        if (!$newBoot) { 'Waiting for SSH after reboot' }
        elseif ($newBoot -eq $bootId) { 'Waiting for the boot ID to change' }
    } 10 $RebootTimeout 'host has rebooted'
    Confirm-DockerHeld $Server
}

function Start-KasmServer($Server, $StartTimeout) {
    $state = Get-MaintenanceState $Server
    if (!$state -or (Get-BootId $Server) -eq $state.boot_id) {
        throw 'Startup requires a saved shutdown and a verified reboot'
    }
    if (!$state.complete) {
        $null = Invoke-RootCommand $Server 'Unmask Docker' (@('systemctl', 'unmask') + $DockerUnits)
        $null = Invoke-RootCommand $Server 'Start Docker' @('systemctl', 'start', 'docker')
        $null = Invoke-RootCommand $Server 'Start Kasm' @("$KasmBin/start")
    }
    Wait-ForCheck $Server {
        $states = Get-ContainerStates $Server
        $waiting = @(
            foreach ($name in $state.containers) {
                if (!$states.ContainsKey($name)) { "$name (missing)" }
            }
            foreach ($entry in $states.GetEnumerator()) {
                $status = $entry.Value
                if ($status.Status -ne 'running') { "$($entry.Key) ($($status.Status))" }
                elseif ($status.Contains('Health') -and $status.Health.Status -ne 'healthy') {
                    "$($entry.Key) ($($status.Health.Status))"
                }
            }
        )
        if ($waiting.Count) { 'Containers not ready: ' + ($waiting -join ', ') }
    } 30 $StartTimeout 'all containers are ready'
    if (!$state.complete) {
        $state.complete = $true
        Save-MaintenanceState $Server $state
    }
}

function Invoke-MaintenanceStage($Role, $Action) {
    $context = "$Role $Action"
    try {
        Import-Module Posh-SSH -RequiredVersion 4.0.0
        $knownHosts = $env:SSH_KNOWN_HOSTS
        if (!$knownHosts) {
            $knownHosts = Join-Path $HOME '.ssh/known_hosts'
            if (!(Test-Path -LiteralPath $knownHosts -PathType Leaf)) {
                $knownHosts = '/etc/ssh/ssh_known_hosts'
            }
        }
        if (!(Test-Path -LiteralPath $knownHosts -PathType Leaf)) { throw 'Provide a verified SSH known_hosts file' }
        $TrustedHosts = Get-SSHOpenSSHTrustedHostStore -LocalFile $knownHosts
        $servers = (Get-MaintenanceInventory)[$Role]
        $stopTimeout = Get-TimeoutSeconds 'KASM_STOP_TIMEOUT_SECONDS' 600
        $rebootTimeout = Get-TimeoutSeconds 'KASM_REBOOT_TIMEOUT_SECONDS' 600
        $startTimeout = Get-TimeoutSeconds 'KASM_START_TIMEOUT_SECONDS' 900

        Write-MaintenanceLog $context "Stage started for $($servers.Count) hosts"
        foreach ($server in $servers) {
            $context = "$Role $Action $($server.Hostname)"
            Write-MaintenanceLog $context 'Host started'
            if ($Action -eq 'stop') { Stop-KasmServer $server $stopTimeout $rebootTimeout }
            else { Start-KasmServer $server $startTimeout }
            Write-MaintenanceLog $context 'Host completed'
        }
        Write-MaintenanceLog "$Role $Action" 'Stage completed'
        return 0
    } catch {
        Write-MaintenanceLog $context $_.ToString() 'ERROR'
        Write-MaintenanceLog $context 'Stage failed. Check the affected host before retrying.' 'ERROR'
        return 1
    }
}
