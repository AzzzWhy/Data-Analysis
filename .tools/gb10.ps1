<#
.SYNOPSIS
  Run a command on the GB10 over SSH, or copy a file to it, using a password
  supplied through an environment variable.

.DESCRIPTION
  Windows OpenSSH cannot accept a password non-interactively, so this drives the
  SSH.NET library instead. The password is read from $env:GB10_PW and is never
  echoed, logged, or written to disk.

.EXAMPLE
  $env:GB10_PW='...'; .\gb10.ps1 -Cmd "uname -a"
  $env:GB10_PW='...'; .\gb10.ps1 -Put "C:\local\file.py" -To "/home/Developer/file.py"
  $env:GB10_PW='...'; .\gb10.ps1 -Get "/home/Developer/out.json" -From "C:\local\out.json"
#>
[CmdletBinding(DefaultParameterSetName = 'Cmd')]
param(
    [Parameter(ParameterSetName = 'Cmd', Mandatory = $true, Position = 0)]
    [string]$Cmd,

    [Parameter(ParameterSetName = 'Put', Mandatory = $true)]
    [string]$Put,

    [Parameter(ParameterSetName = 'Put', Mandatory = $true)]
    [string]$To,

    [Parameter(ParameterSetName = 'Get', Mandatory = $true)]
    [string]$Get,

    [Parameter(ParameterSetName = 'Get', Mandatory = $true)]
    [string]$From,

    [string]$HostName = '106.13.186.155',
    [int]$Port = 6060,
    [string]$User = 'Developer',
    [int]$TimeoutSec = 600
)

$ErrorActionPreference = 'Stop'

$pw = $env:GB10_PW
if ([string]::IsNullOrEmpty($pw)) {
    Write-Error 'GB10_PW environment variable is not set. Refusing to prompt interactively.'
    exit 2
}

# Both assemblies must be loaded from the same directory: SSH.NET's crypto layer
# needs BouncyCastle.Cryptography 2.0.0.0, resolved next to it at runtime.
# The net7.0 target is used because the net8.0 build's CryptoAbstraction static
# initializer throws on PowerShell 7.6 / .NET 10.
$libDir = Join-Path $PSScriptRoot 'lib'
foreach ($name in @('BouncyCastle.Cryptography.dll', 'Renci.SshNet.dll')) {
    $path = Join-Path $libDir $name
    if (-not (Test-Path $path)) { Write-Error "missing dependency: $path"; exit 2 }
    [void][System.Reflection.Assembly]::LoadFrom($path)
}

$conn = New-Object Renci.SshNet.PasswordConnectionInfo($HostName, $Port, $User, $pw)
$conn.Timeout = [TimeSpan]::FromSeconds(30)
$client = New-Object Renci.SshNet.SshClient($conn)

try {
    $client.Connect()
} catch {
    Write-Error "SSH connect failed: $($_.Exception.Message)"
    exit 3
}

try {
    switch ($PSCmdlet.ParameterSetName) {
        'Cmd' {
            $command = $client.CreateCommand($Cmd)
            $command.CommandTimeout = [TimeSpan]::FromSeconds($TimeoutSec)
            $out = $command.Execute()
            if ($out) { Write-Output $out.TrimEnd() }
            $err = $command.Error
            if ($err) { Write-Output "---STDERR---"; Write-Output $err.TrimEnd() }
            exit $command.ExitStatus
        }
        'Put' {
            $sftp = New-Object Renci.SshNet.SftpClient($conn)
            $sftp.Connect()
            try {
                # .NET static call rather than Resolve-Path: the workspace path contains non-ASCII
# characters that provider path resolution mangles. Also avoids Get-Item
# returning the path without a .FullName property.
$resolved = [System.IO.Path]::GetFullPath($Put)
if (-not [System.IO.File]::Exists($resolved)) {
    Write-Error "local file not found: $resolved"
    exit 2
}
$bytes = [System.IO.File]::ReadAllBytes($resolved)
                $stream = New-Object System.IO.MemoryStream(, $bytes)
                $sftp.UploadFile($stream, $To, $true)
                $stream.Dispose()
                $attrs = $sftp.GetAttributes($To)
                Write-Output "uploaded $($bytes.Length) bytes -> ${HostName}:$To"
            } finally { $sftp.Disconnect() }
            exit 0
        }
        'Get' {
            $sftp = New-Object Renci.SshNet.SftpClient($conn)
            $sftp.Connect()
            try {
                $ms = New-Object System.IO.MemoryStream
                $sftp.DownloadFile($From, $ms)
                [System.IO.File]::WriteAllBytes($Get, $ms.ToArray())
                Write-Output "downloaded $($ms.Length) bytes -> $Get"
                $ms.Dispose()
            } finally { $sftp.Disconnect() }
            exit 0
        }
    }
} catch {
    Write-Error "remote operation failed: $($_.Exception.Message)"
    exit 4
} finally {
    if ($client.IsConnected) { $client.Disconnect() }
    $client.Dispose()
}