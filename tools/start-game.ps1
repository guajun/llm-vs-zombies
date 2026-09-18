$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$gameDirectory = Join-Path $projectRoot 'game/original'
$gameExe = Join-Path $gameDirectory 'PlantsVsZombies.exe'
if (!(Test-Path -LiteralPath $gameExe)) { throw 'Copy your local game to game/original first.' }
Write-Output 'Starting the project game copy. Original PvZ may still use shared ProgramData saves.'
# A visible window is intentional: this script is explicitly invoked to play the game.
Start-Process -FilePath $gameExe -WorkingDirectory $gameDirectory
