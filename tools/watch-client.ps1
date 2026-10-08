param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$Repo = '/home/qwait/MineBot',
    [string]$Display = ':99',
    [ValidateRange(1024, 65535)][int]$Port = 18990
)
$ErrorActionPreference = 'Stop'
$title = 'MineBot LIVE'
$mutex = New-Object System.Threading.Mutex($false, "Local\MineBotLiveView-$Port")
if (-not $mutex.WaitOne(0)) {
    Write-Output 'MineBot LIVE is already open.'
    $mutex.Dispose()
    return
}
try {
    $ffplay = (Get-Command ffplay.exe).Source
    $logs = Join-Path $env:LOCALAPPDATA 'MineBot\live-view'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    # The capture process owns its single-instance lock. Closing this viewer
    # leaves it available for the next open; it never owns the Minecraft client.
    $capture = Start-Process wsl.exe -ArgumentList @(
        '-d', $Distro, '--cd', $Repo, '--', '.venv/bin/python',
        '-m', 'minebot.camera.live_view', '--display', $Display, '--port', $Port
    ) -WindowStyle Hidden -PassThru -RedirectStandardError "$logs\supervisor-$PID-error.log" -RedirectStandardOutput "$logs\supervisor-$PID.log"
    $env:NO_PROXY = '127.0.0.1,localhost'
    $viewerArguments = @(
        '-hide_banner', '-loglevel', 'warning', '-nostats', '-an',
        '-probesize', '32768', '-analyzeduration', '0', '-f', 'mpjpeg', '-rw_timeout', '4000000',
        '-reconnect', '1', '-reconnect_at_eof', '1', '-reconnect_streamed', '1',
        '-reconnect_on_network_error', '1', '-reconnect_delay_max', '2',
        '-reconnect_delay_total_max', '4294',
        '-window_title', ('"' + $title + '"'), '-x', '1280', '-y', '720',
        "http://127.0.0.1:$Port/live.mjpg"
    )
    do {
        $viewer = Start-Process $ffplay -ArgumentList $viewerArguments -WindowStyle Normal -PassThru -RedirectStandardError "$logs\viewer.log" -RedirectStandardOutput "$logs\viewer.out"
        Write-Output "MineBot LIVE viewer PID: $($viewer.Id)"
        $viewer.WaitForExit()
        # A normal window close ends viewing. A transport failure retries;
        # this also covers a client restart longer than FFmpeg's retry window.
        if ($viewer.ExitCode -ne 0) { Start-Sleep -Seconds 1 }
    } while ($viewer.ExitCode -ne 0)
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
