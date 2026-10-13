[CmdletBinding()]
param([string]$ManifestPath, [string]$CompanyConfig, [string]$OutputZip)
$ErrorActionPreference = 'Stop'

function Add-CompanyConfigToPackage {
    param([string]$BaseZip, [string]$ConfigPath, [string]$Destination, [bool]$HumanConfirmed)
    # Enforce confirmation before even opening the plaintext configuration.
    if (-not $HumanConfirmed) { throw '未确认，不读取配置，也不生成含配置的包。' }
    $source = [IO.Path]::GetFullPath($BaseZip)
    $config = [IO.Path]::GetFullPath($ConfigPath)
    $target = [IO.Path]::GetFullPath($Destination)
    if ($target -eq $source -or $target -eq $config) { throw '新包不能覆盖源文件。' }
    if (Test-Path -LiteralPath $target) { throw '目标文件已存在，请选择新文件名。' }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Copy-Item -LiteralPath $source -Destination $target
    try {
        $archive = [IO.Compression.ZipFile]::Open($target, [IO.Compression.ZipArchiveMode]::Update)
        try {
            if ($archive.GetEntry('company.local.json')) { throw '源包已包含公司配置，请使用无秘密的内部运行版。' }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $config, 'company.local.json') | Out-Null
            $metadata = $archive.GetEntry('BUILD_INFO.json')
            if ($metadata) {
                $reader = [IO.StreamReader]::new($metadata.Open())
                try { $info = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                $metadata.Delete()
                $info.contains_company_credentials = $true
                $entry = $archive.CreateEntry('BUILD_INFO.json')
                $writer = [IO.StreamWriter]::new($entry.Open(), [Text.UTF8Encoding]::new($false))
                try { $writer.Write(($info | ConvertTo-Json -Depth 8)) } finally { $writer.Dispose() }
            }
        } finally { $archive.Dispose() }
    } catch {
        # Exact newly-created file only; never delete the base package or source configuration.
        if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target }
        throw '装包失败，新包已清理；原始包与配置仍保留。'
    }
    Write-Output $target
}

# Dot sourcing is reserved for fake-fixture tests; a double-click runs this section.
if ($MyInvocation.InvocationName -ne '.') {
    if (-not $ManifestPath) {
        $candidate = Join-Path $PSScriptRoot '最新部署包.json'
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path (Split-Path -Parent $PSScriptRoot) 'dist\最新部署包.json' }
        $ManifestPath = $candidate
    }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $baseZip = Join-Path (Split-Path -Parent $ManifestPath) $manifest.internal_package
    Add-Type -AssemblyName System.Windows.Forms
    # Every user-facing route uses the same explicit-consent, allowlisted wizard.
    if ($true) {
        $folder = [Windows.Forms.FolderBrowserDialog]::new()
        $folder.Description = '选择本机已经部署的工作台目录（包含 .venv 和 runtime）；本人确认后读取保存的配置，缺项可在表单补充。'
        $folder.SelectedPath = Split-Path -Parent $PSScriptRoot
        try {
            if ($folder.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 0 }
            $installation = $folder.SelectedPath
        } finally { $folder.Dispose() }
        $python = Join-Path $installation '.venv\Scripts\python.exe'
        $wizard = Join-Path $installation 'scripts\package_company_wizard.py'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $wizard -PathType Leaf)) {
            throw '所选目录没有新版已部署工作台。请选择已更新的工作台目录，不要选择 dist 或 ZIP 文件。'
        }
        if (-not $OutputZip) {
            $OutputZip = Join-Path (Split-Path -Parent $baseZip) (([IO.Path]::GetFileNameWithoutExtension($baseZip)) + '-含公司完整配置-' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '.zip')
        }
        $wizardArguments = @($wizard, '--root', $installation, '--base', $baseZip, '--output', $OutputZip)
        if ($CompanyConfig) { $wizardArguments += @('--company-json', $CompanyConfig) }
        & $python @wizardArguments
        if ($LASTEXITCODE -ne 0) { throw '本机配置向导未完成；原包和原配置未改。' }
        exit 0
    }
    if (-not $CompanyConfig) {
        $dialog = [Windows.Forms.OpenFileDialog]::new()
        $dialog.Title = '选择公司配置文件（仅在下一步确认后读取内容）'
        $dialog.Filter = '公司配置 JSON (*.json)|*.json'
        try {
            if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 0 }
            $CompanyConfig = $dialog.FileName
        } finally { $dialog.Dispose() }
    }
    if (-not $OutputZip) {
        $OutputZip = Join-Path (Split-Path -Parent $baseZip) (([IO.Path]::GetFileNameWithoutExtension($baseZip)) + '-含公司配置-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
    }
    $prompt = "将读取：`r`n$CompanyConfig`r`n`r`n将生成：`r`n$OutputZip`r`n`r`n新 ZIP 会包含公司代理凭据及 AdsPower API Key。只发给获准的同事，禁止上传公开 GitHub。原包和原配置不变。`r`n`r`n是否现在生成？"
    $answer = [Windows.Forms.MessageBox]::Show($prompt, '确认生成含公司配置的内部包',
        [Windows.Forms.MessageBoxButtons]::YesNo, [Windows.Forms.MessageBoxIcon]::Warning,
        [Windows.Forms.MessageBoxDefaultButton]::Button2)
    if ($answer -ne [Windows.Forms.DialogResult]::Yes) { exit 0 }
    $created = Add-CompanyConfigToPackage -BaseZip $baseZip -ConfigPath $CompanyConfig -Destination $OutputZip -HumanConfirmed $true
    [Windows.Forms.MessageBox]::Show("内部包已生成：`r`n$created`r`n`r`n接收人在自己电脑上双击 deploy.cmd，并确认导入公司配置。", '完成') | Out-Null
}
