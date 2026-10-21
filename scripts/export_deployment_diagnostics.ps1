param([string]$OutputPath)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'deployment_support.ps1')
try {
    $path=Export-DeploymentDiagnostics $root $OutputPath
    Write-Output ('诊断包已保存：'+$path)
    if (-not [Console]::IsInputRedirected -and [Environment]::UserInteractive) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show('诊断包已保存，请交给维护者：'+"`r`n"+$path,'选品工作台')
        Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList @('/select,',('"'+$path+'"'))
    }
} catch { Write-Error '诊断导出未完成，请把此窗口截图交给维护者。' -ErrorAction Continue; exit 1 }
