param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$Repo = '/home/qwait/MineBot',
    [string]$Server = '127.0.0.1:25566',
    [string]$Bot = 'MineBotCombat',
    [ValidateRange(1, 65535)][int]$Port = 8768,
    [switch]$DisposableWorld
)
$ErrorActionPreference = 'Stop'
$clientArgs = @('-d', $Distro, '--cd', $Repo, '--', 'env',
    "MINEBOT_CLIENT_SERVER=$Server", "MINEBOT_CLIENT_USERNAME=$Bot",
    "MINEBOT_CLIENT_IPC_PORT=$Port",
    "MINEBOT_CLIENT_DISPOSABLE_WORLD=$($DisposableWorld.IsPresent.ToString().ToLower())",
    'bash', 'tools/open-client.sh')
& wsl.exe @clientArgs
exit $LASTEXITCODE
