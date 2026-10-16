# Installer helpers. Dot sourcing does not read company configuration or install anything.
function Assert-ProjectTarget {
    param([string]$Root, [string]$Target)
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $path = [IO.Path]::GetFullPath($Target)
    if (-not $path.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw '安装目标超出项目目录。' }
    return $path
}
function Confirm-CompanyImport {
    param([string]$ConfigPath, [string]$Root, [switch]$Accepted)
    if (-not $ConfigPath) { return }
    if ($Accepted) { Write-Host '导入获授权的内部包公司配置，按当前 Windows 用户重新加密。'; return }
    Add-Type -AssemblyName System.Windows.Forms
    $answer = [Windows.Forms.MessageBox]::Show(
        "将从以下文件导入公司配置：`r`n$ConfigPath`r`n`r`n将保存到：`r`n$(Join-Path $Root 'runtime\secrets\company.dpapi')`r`n`r`n配置包含代理凭据及 AdsPower API Key、D1 API Key、DeepSeek API Key/地址/模型（完整配置包），将由 Windows 按当前用户加密保存。现有配置会备份。是否现在导入？",
        '确认本机导入公司配置', [Windows.Forms.MessageBoxButtons]::YesNo,
        [Windows.Forms.MessageBoxIcon]::Warning, [Windows.Forms.MessageBoxDefaultButton]::Button2)
    if ($answer -ne [Windows.Forms.DialogResult]::Yes) { throw '用户取消配置导入；未读取公司配置。' }
}
function Assert-DeploymentAssets {
    param([string]$Root)
    $assets = Join-Path $Root 'deployment-assets'
    $manifestPath = Join-Path $assets 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $path = Assert-ProjectTarget $assets (Join-Path $assets ($entry.Name.Replace('/', '\')))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "部署依赖资产缺失：$($entry.Name)" }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256) { throw "部署依赖资产校验失败：$($entry.Name)" }
    }
}
function Assert-DeploymentPathLength {
    param([string]$Root)
    # Keep space for nested dependencies, category exports and atomic filenames.
    # This does not change the Windows system long-path setting.
    $absolute = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($absolute.Length -gt 100) {
        throw '安装路径过长：请解压到 D:\选品工作台 等较短目录（绝对路径不超过100字符）后重试。尚未读取公司配置或更改现有环境；Windows长路径安装暂不保证兼容。'
    }
}
function Get-DeploymentPreflight {
    param([string]$Root)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw '此一键部署入口需要 Windows。' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw '此部署包需要 64 位 Windows。' }
    Assert-DeploymentPathLength $Root
    foreach ($relative in @('requirements.lock.txt','package-lock.json','scripts\company_setup.py','web\index.html')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) { throw "部署源缺少文件：$relative" }
    }
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Root)))
    $directory = Get-Item -LiteralPath $Root
    while ($directory) {
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '安装路径经过目录链接或挂载点，请解压到本地磁盘的普通短目录，避免检查和写入错误磁盘。' }
        $directory = $directory.Parent
    }
    if ($drive.AvailableFreeSpace -lt 12GB) { throw '剩余磁盘不足 12 GB，未开始安装。' }
    [pscustomobject]@{ platform='Windows'; source_complete=$true; free_gb=[math]::Round($drive.AvailableFreeSpace/1GB,1); configuration_read=$false }
}
function Start-ProjectDeployment {
    param([string]$Root, [bool]$ImportCompany)
    $backup = Assert-ProjectTarget $Root (Join-Path $Root ('.deployment-backup\' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff')))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    $targets = @('.venv','node_modules','runtime\agent-config','runtime\tools')
    $transaction = [pscustomobject]@{ root=$Root; backup=$backup; targets=$targets; company=$ImportCompany; company_existed=$false; deepseek_existed=$false; liquid_d1_existed=$false; moved=@() }
    try {
        foreach ($relative in $targets) {
            $target = Assert-ProjectTarget $Root (Join-Path $Root $relative)
            if (Test-Path -LiteralPath $target) {
                $destination = Assert-ProjectTarget $Root (Join-Path $backup $relative)
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                Move-Item -LiteralPath $target -Destination $destination
                $transaction.moved += $relative
            }
        }
        if ($ImportCompany) {
            $d1Secret = Join-Path $Root 'runtime\secrets\liquid_d1.dpapi'
            if (Test-Path -LiteralPath $d1Secret -PathType Leaf) {
                Copy-Item -LiteralPath $d1Secret -Destination (Join-Path $backup 'previous-liquid_d1.dpapi')
                $transaction.liquid_d1_existed = $true
            }
            $modelSecret = Join-Path $Root 'runtime\secrets\deepseek.dpapi'
            if (Test-Path -LiteralPath $modelSecret -PathType Leaf) {
                Copy-Item -LiteralPath $modelSecret -Destination (Join-Path $backup 'previous-deepseek.dpapi')
                $transaction.deepseek_existed = $true
            }
            $secret = Join-Path $Root 'runtime\secrets\company.dpapi'
            if (Test-Path -LiteralPath $secret -PathType Leaf) {
                Copy-Item -LiteralPath $secret -Destination (Join-Path $backup 'previous-company.dpapi')
                $transaction.company_existed = $true
            }
        }
    } catch {
        # Only restore paths that were actually moved before this partial failure.
        foreach ($relative in $transaction.moved) {
            Move-Item -LiteralPath (Join-Path $backup $relative) -Destination (Join-Path $Root $relative)
        }
        throw '准备安装备份失败；已恢复此前移动的环境。'
    }
    return $transaction
}
function Restore-ProjectDeployment {
    param($Transaction)
    foreach ($relative in $Transaction.targets) {
        $target = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.root $relative)
        $previous = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.backup $relative)
        # Retain failed files; no recursive deletion or traversal of junctions.
        if (Test-Path -LiteralPath $target) {
            $failed = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.backup ('failed\' + $relative))
            New-Item -ItemType Directory -Path (Split-Path -Parent $failed) -Force | Out-Null
            Move-Item -LiteralPath $target -Destination $failed
        }
        if (Test-Path -LiteralPath $previous) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Move-Item -LiteralPath $previous -Destination $target
        }
    }
    if ($Transaction.company) {
        $d1Secret = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.root 'runtime\secrets\liquid_d1.dpapi')
        if ($Transaction.liquid_d1_existed) {
            Copy-Item -LiteralPath (Join-Path $Transaction.backup 'previous-liquid_d1.dpapi') -Destination $d1Secret -Force
        } elseif (Test-Path -LiteralPath $d1Secret -PathType Leaf) {
            Move-Item -LiteralPath $d1Secret -Destination (Join-Path $Transaction.backup 'failed-liquid_d1.dpapi')
        }
        $modelSecret = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.root 'runtime\secrets\deepseek.dpapi')
        if ($Transaction.deepseek_existed) {
            Copy-Item -LiteralPath (Join-Path $Transaction.backup 'previous-deepseek.dpapi') -Destination $modelSecret -Force
        } elseif (Test-Path -LiteralPath $modelSecret -PathType Leaf) {
            Move-Item -LiteralPath $modelSecret -Destination (Join-Path $Transaction.backup 'failed-deepseek.dpapi')
        }
        $secret = Assert-ProjectTarget $Transaction.root (Join-Path $Transaction.root 'runtime\secrets\company.dpapi')
        if ($Transaction.company_existed) {
            Copy-Item -LiteralPath (Join-Path $Transaction.backup 'previous-company.dpapi') -Destination $secret -Force
        } elseif (Test-Path -LiteralPath $secret -PathType Leaf) {
            Move-Item -LiteralPath $secret -Destination (Join-Path $Transaction.backup 'failed-company.dpapi')
        }
    }
}
function Write-DeploymentStatus {
    param([string]$Root, [string]$Stage, [bool]$Succeeded, [bool]$RolledBack)
    $directory = Join-Path $Root 'runtime'
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    # No arbitrary exception text, command arguments, or configuration values.
    $value = @{ stage=$Stage; succeeded=$Succeeded; project_environment_rolled_back=$RolledBack; updated_at=(Get-Date).ToUniversalTime().ToString('o') }
    [IO.File]::WriteAllText((Join-Path $directory 'install-status.json'), ($value | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
}
