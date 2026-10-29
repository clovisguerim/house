# Cria (ou remove) uma tarefa agendada que inicia o House quando você faz login no Windows.
# Roda só para o seu usuário, sem privilégio de administrador.
# Uso:  .\iniciar-com-windows.ps1            cria
#       .\iniciar-com-windows.ps1 -Remover   remove
param([switch]$Remover)

$nome = "House - busca de imoveis"
if ($Remover) {
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Tarefa removida."
    exit 0
}

$pasta = $PSScriptRoot
$acao = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$pasta\run.ps1`"" `
    -WorkingDirectory $pasta
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$config = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $nome -Action $acao -Trigger $gatilho -Settings $config -Force | Out-Null
Write-Host "Tarefa '$nome' criada: o House inicia sozinho no próximo login."
Write-Host "A publicação do 'tailscale serve' fica salva no Tailscale e volta sozinha."
