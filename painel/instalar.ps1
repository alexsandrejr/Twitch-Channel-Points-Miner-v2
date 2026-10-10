# Instala tudo o que o Twitch Miner precisa: um Python portátil, as bibliotecas e os atalhos.
# Não precisa de administrador e não mexe em nenhum outro Python instalado no computador.
# Pode ser executado de novo a qualquer momento para reparar a instalação.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # sem isso os downloads ficam muito lentos no PowerShell 5
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$PythonVersion = '3.12.10'
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = 'https://bootstrap.pypa.io/get-pip.py'
$DevRequirements = @('pre-commit')

$Root = Split-Path -Parent $PSScriptRoot
$Painel = Join-Path $Root 'painel'
$Runtime = Join-Path $env:LOCALAPPDATA 'TwitchMinerPainel\python'
$Python = Join-Path $Runtime 'python.exe'
$PythonW = Join-Path $Runtime 'pythonw.exe'
$Temp = Join-Path $env:TEMP 'TwitchMinerPainel'

function Write-Step($number, $text) {
    Write-Host ''
    Write-Host "[$number/4] $text" -ForegroundColor Cyan
}

function Get-File($url, $file) {
    for ($try = 1; $try -le 3; $try++) {
        try {
            Invoke-WebRequest $url -OutFile $file -UseBasicParsing
            return
        } catch {
            if ($try -eq 3) { throw "Não foi possível baixar $url. Verifique sua conexão com a internet." }
            Start-Sleep -Seconds 3
        }
    }
}

function Invoke-Python([string[]]$arguments, $failMessage) {
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw $failMessage }
}

function New-Shortcut($folder) {
    New-Item -ItemType Directory -Force $folder | Out-Null
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder 'Twitch Miner.lnk'))
    $shortcut.TargetPath = $PythonW
    $shortcut.Arguments = "`"$Painel\app.py`""
    $shortcut.WorkingDirectory = $Painel
    $shortcut.IconLocation = "$Painel\static\icone.ico"
    $shortcut.Description = 'Abre o painel do Twitch Channel Points Miner'
    $shortcut.Save()
}

try {
    $Host.UI.RawUI.WindowTitle = 'Instalador - Twitch Miner'
    Write-Host '=============================================' -ForegroundColor Magenta
    Write-Host '        Instalador do Twitch Miner' -ForegroundColor Magenta
    Write-Host '=============================================' -ForegroundColor Magenta
    Write-Host 'Não feche esta janela. A instalação leva alguns minutos.'

    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Este programa precisa do Windows 64 bits.' }
    New-Item -ItemType Directory -Force $Temp | Out-Null

    Write-Step 1 'Python'
    if (Test-Path $Python) {
        Write-Host 'Já instalado.'
    } else {
        Write-Host "Baixando o Python $PythonVersion..."
        $zip = Join-Path $Temp "python-$PythonVersion.zip"
        Get-File $PythonUrl $zip
        Expand-Archive $zip $Runtime -Force
        # Habilita o site-packages, onde o pip instala as bibliotecas
        $pth = Get-ChildItem $Runtime -Filter 'python*._pth' | Select-Object -First 1
        (Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ASCII
        Write-Host 'Python instalado.'
    }

    Write-Step 2 'Instalador de bibliotecas (pip)'
    if (Test-Path (Join-Path $Runtime 'Lib\site-packages\pip')) {
        Write-Host 'Já instalado.'
    } else {
        $getPip = Join-Path $Temp 'get-pip.py'
        Get-File $GetPipUrl $getPip
        Invoke-Python @($getPip, '--no-warn-script-location', '--disable-pip-version-check', '-q') 'Falha ao instalar o pip.'
        Write-Host 'pip instalado.'
    }

    Write-Step 3 'Bibliotecas do miner (pode demorar alguns minutos)'
    $packages = Get-Content (Join-Path $Root 'requirements.txt') | ForEach-Object { $_.Trim() } | Where-Object {
        $_ -and -not $_.StartsWith('#') -and ($DevRequirements -notcontains ($_ -split '[<>=!~\[; ]')[0])
    }
    Invoke-Python (@('-m', 'pip', 'install', '--no-warn-script-location', '--disable-pip-version-check') + $packages) `
        'Falha ao instalar as bibliotecas. Verifique sua internet e tente de novo.'

    Write-Step 4 'Atalhos'
    New-Shortcut ([Environment]::GetFolderPath('Desktop'))
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'Twitch Miner')
    Write-Host 'Atalho "Twitch Miner" criado na Área de Trabalho e no Menu Iniciar.'

    Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue
    Start-Process $PythonW -ArgumentList "`"$Painel\app.py`"" -WorkingDirectory $Painel

    Write-Host ''
    Write-Host 'Tudo pronto! O painel está abrindo no seu navegador.' -ForegroundColor Green
    Write-Host 'Nas próximas vezes, abra pelo atalho "Twitch Miner" na Área de Trabalho.' -ForegroundColor Green
    Start-Sleep -Seconds 10
    exit 0
} catch {
    Write-Host ''
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Tente executar o Instalar.bat de novo. Se o erro continuar, tire um print desta janela'
    Write-Host 'e envie para quem te passou o programa.'
    exit 1
}
