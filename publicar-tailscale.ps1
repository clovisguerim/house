# Publica o House na sua rede privada Tailscale (tailnet) com HTTPS, sem abrir porta no firewall.
# O servidor continua escutando só em 127.0.0.1:8000; o Tailscale faz o proxy.
#
# Pré-requisitos (feitos por você): Tailscale instalado e logado nesta máquina.
# Uso:  .\publicar-tailscale.ps1          publica
#       .\publicar-tailscale.ps1 -Parar   remove a publicação
param([switch]$Parar, [int]$Port = 8000)

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    $padrao = "$env:ProgramFiles\Tailscale\tailscale.exe"
    if (Test-Path $padrao) { $ts = $padrao } else {
        Write-Host "Tailscale não encontrado. Instale em https://tailscale.com/download/windows e faça login." -ForegroundColor Yellow
        exit 1
    }
} else { $ts = $ts.Source }

if ($Parar) {
    & $ts serve reset
    Write-Host "Publicação removida."
    exit 0
}

$status = & $ts status --json | ConvertFrom-Json
if ($status.BackendState -ne "Running") {
    Write-Host "O Tailscale não está conectado (estado: $($status.BackendState)). Abra o Tailscale e faça login." -ForegroundColor Yellow
    exit 1
}

try { Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3 | Out-Null }
catch { Write-Host "O House não está rodando em 127.0.0.1:$Port. Rode .\run.ps1 primeiro." -ForegroundColor Yellow; exit 1 }

# Se HTTPS/MagicDNS ainda não estiverem ligados na tailnet, o próprio comando mostra o link para ligar
& $ts serve --bg $Port
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$dns = $status.Self.DNSName.TrimEnd(".")
Write-Host ""
Write-Host "Pronto! Acesse de qualquer aparelho da sua tailnet:" -ForegroundColor Green
Write-Host "  https://$dns/"
