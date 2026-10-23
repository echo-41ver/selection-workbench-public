function Protect-DeploymentSupportText([string]$Text) {
    $value = [regex]::Replace($Text, '(?s)-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----(?:.*?-----END [^-]+-----|.*$)', '[REDACTED SECRET BLOCK]')
    $value = [regex]::Replace($value, '(?im)(\b(?:Set-Cookie|Cookie|Proxy-Authorization|Authorization)\s*:\s*)[^\r\n]*', '$1[REDACTED]')
    $value = [regex]::Replace($value, '(?i)\b(Bearer|Basic)\s+[^\s"''<>]+', '$1 [REDACTED]')
    $value = [regex]::Replace($value, '(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', '$1[REDACTED]@')
    $value = [regex]::Replace($value, '(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-(?:proj-)?[A-Za-z0-9_-]+)\b', '[REDACTED TOKEN]')
    $value = [regex]::Replace($value, '(?i)([\w.-]*(?:token|secret|password|passwd|pwd|api.?key|private.?key|access.?key|authorization|cookie|credential|session)[\w.-]*["'']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|''(?:\\.|[^''\\])*(?:''|$)|[^\s,;}]+)', '$1[REDACTED]')
    $value = [regex]::Replace($value, '(?i)(--(?:api[_-]?key|token|secret|password|passwd|pwd|cookie)[=\s]+)(?:"(?:\\.|[^"\\])*(?:"|$)|''(?:\\.|[^''\\])*(?:''|$)|\S+)', '$1[REDACTED]')
    $value = [regex]::Replace($value, '\?[^\s"''<>]+', '?[REDACTED QUERY]')
    return [regex]::Replace($value, '(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])', '[REDACTED OPAQUE VALUE]')
}

function Test-DeploymentSupportPath([string]$Root, [string]$Path) {
    $rootPath=[IO.Path]::GetFullPath($Root).TrimEnd('\')
    $cursor=[IO.Path]::GetFullPath($Path)
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($cursor.TrimEnd('\'),$rootPath) -and -not $cursor.StartsWith($rootPath+'\',[StringComparison]::OrdinalIgnoreCase)) { return $false }
    while ($cursor) {
        if (-not (Test-Path -LiteralPath $cursor)) { return $false }
        $item=Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        $parent=[IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor=$parent
    }
    return $true
}

function Read-DeploymentSupportRecord([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $null }
    if (-not (Test-DeploymentSupportPath ([IO.Path]::GetDirectoryName($Path)) $Path)) { return $null }
    try { return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json }
    catch { return $null }
}

function Get-DeploymentUserFailure([string]$Root, [int]$NativeExitCode, [string]$Stage, [string]$Message) {
    $status = Read-DeploymentSupportRecord (Join-Path $Root 'runtime\install-status.json')
    $failure = Read-DeploymentSupportRecord (Join-Path $Root 'runtime\install-failure.json')
    if (-not $Stage) { $Stage = if ($status.stage -and -not $status.succeeded) { [string]$status.stage } else { '准备或启动工作台' } }
    if (-not $Message -and -not $status.succeeded) { $Message = [string]$failure.message }
    $detail = Protect-DeploymentSupportText $Message
    $reason = "【$Stage】未完成（错误编号 $NativeExitCode）。"
    $next = '请先双击“导出诊断.cmd”，将生成的诊断包交给维护者。无需查看日志或手工安装依赖。'
    if ($NativeExitCode -eq 124) {
        $reason = '安装状态暂时无法确认，后台安装可能仍在运行。'
        $next = '请勿重复启动部署或关闭安装进程。点击“导出诊断”交给维护者确认后台状态。'
    } elseif ($detail -match '(?i)access.{0,10}denied|拒绝访问|requires.{0,20}elevat|administrator') {
        $reason = 'Windows 未授予本次安装所需权限。'
        $next = '重新双击 deploy.cmd，在 Windows 授权提示中选择“是”。若公司策略阻止授权，请联系系统管理员。'
    } elseif ($detail -match '(?i)port.{0,30}(occupied|in use)|端口.{0,15}(占用|使用)') {
        $reason = '工作台需要的本机端口已被其他程序占用。'
        $next = '请关闭已经打开的另一个工作台后再试；不确定时导出诊断交给维护者，不要结束其他软件。'
    } elseif ($detail -match '(?i)network|timed? ?out|download|连接超时|下载失败|网络') {
        $reason = "【$Stage】所需的网络连接或文件下载未完成。"
        $next = '确认电脑网络可用后重新双击 deploy.cmd。再次失败时导出诊断交给维护者，无需手工下载依赖。'
    } elseif ($failure.command.exit_code_available -and -not $status.succeeded) {
        $reason = "【$Stage】安装器未能完成，安装器返回码为 $($failure.command.exit_code)。"
    }
    return [ordered]@{ native_exit_code=$NativeExitCode; stage=$Stage; reason=$reason; next_step=$next; diagnostic_entry='导出诊断.cmd'; technical_message=$detail }
}

function Export-DeploymentDiagnostics([string]$Root, [string]$OutputPath) {
    $Root = [IO.Path]::GetFullPath($Root)
    if (-not (Test-DeploymentSupportPath $Root $Root)) { throw 'Diagnostic root contains a junction or symbolic link.' }
    if (-not $OutputPath) {
        $folder = Join-Path $Root '诊断记录'
        [void][IO.Directory]::CreateDirectory($folder)
        $OutputPath = Join-Path $folder ('选品工作台诊断-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8) + '.zip')
    }
    if (-not (Test-DeploymentSupportPath ([IO.Path]::GetDirectoryName($OutputPath)) ([IO.Path]::GetDirectoryName($OutputPath)))) { throw 'Diagnostic output parent contains a junction or symbolic link.' }
    Add-Type -AssemblyName System.IO.Compression
    $stream = New-Object IO.FileStream($OutputPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $zip = $null
    try {
        $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $true)
        $files = @()
        foreach ($name in @('install-status.json','install-failure.json','deployment-smoke.json','deployment-compatibility.json','deployment-user-failure.json')) {
            $path = Join-Path $Root ('runtime\' + $name)
            if ([IO.File]::Exists($path)) { $files += Get-Item -LiteralPath $path }
        }
        $entryRoot = Join-Path $Root 'runtime\deployment-entry'
        if ([IO.Directory]::Exists($entryRoot) -and (Test-DeploymentSupportPath $Root $entryRoot)) {
            $attempts = @(Get-ChildItem -LiteralPath $entryRoot -Directory | Where-Object { $_.Name -match '^[a-f0-9]{32}$' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 3)
            foreach ($attempt in $attempts) {
                $files += Get-ChildItem -LiteralPath $attempt.FullName -File | Where-Object { $_.Name -match '^(?:(?:install|start-web|worker)\.)?(?:stdout|stderr)\.log$|^(?:result|parent-error|parent-timeout)\.json$' }
            }
        }
        # Binding failures retain their actual native stderr here. Include only
        # three recent exact launcher log pairs, with the same redaction/limits.
        $launcherRoot = Join-Path $Root 'runtime\launcher-logs'
        if ([IO.Directory]::Exists($launcherRoot) -and (Test-DeploymentSupportPath $Root $launcherRoot)) {
            $launcherErrors = @(Get-ChildItem -LiteralPath $launcherRoot -File | Where-Object { $_.Name -match '^[a-f0-9]{32}\.stderr\.log$' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 3)
            foreach ($launcherError in $launcherErrors) {
                $files += $launcherError
                $stdout = Join-Path $launcherRoot ($launcherError.Name.Replace('.stderr.log', '.stdout.log'))
                if ([IO.File]::Exists($stdout)) { $files += Get-Item -LiteralPath $stdout }
            }
        }
        $included = @()
        foreach ($file in $files) {
            if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $full = [IO.Path]::GetFullPath($file.FullName)
            if (-not $full.StartsWith($Root.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Diagnostic source escaped program root.' }
            if (-not (Test-DeploymentSupportPath $Root $full)) { continue }
            $relative = $full.Substring($Root.TrimEnd('\').Length+1).Replace('\','/')
            $content = Protect-DeploymentSupportText ((Get-Content -LiteralPath $full -Encoding UTF8 -Tail 500) -join "`n")
            if ($content.Length -gt 524288) { $content = $content.Substring($content.Length-524288) }
            $entry = $zip.CreateEntry($relative)
            $writer = New-Object IO.StreamWriter($entry.Open(), [Text.UTF8Encoding]::new($false))
            try { $writer.Write($content) } finally { $writer.Dispose() }
            $included += $relative
        }
        $build = Read-DeploymentSupportRecord (Join-Path $Root 'BUILD_INFO.json')
        $commit = if ($build.commit -match '^[a-f0-9]{40}$') { $build.commit } else { $null }
        $manifest = @{created_at=[DateTimeOffset]::Now.ToString('o'); commit=$commit; included=$included; excluded='公司配置、密钥存储、浏览器会话、运行商品数据和环境变量不进入诊断包。'; purpose='仅供维护者排查部署；用户无需查看技术日志。'}
        $entry = $zip.CreateEntry('diagnostic-manifest.json')
        $writer = New-Object IO.StreamWriter($entry.Open(), [Text.UTF8Encoding]::new($false))
        try { $writer.Write(($manifest | ConvertTo-Json -Depth 6)) } finally { $writer.Dispose() }
    } finally { if ($zip) { $zip.Dispose() }; $stream.Dispose() }
    return [IO.Path]::GetFullPath($OutputPath)
}

function Show-DeploymentFailure([string]$Root, [int]$NativeExitCode, [string]$Stage, [string]$Message, [switch]$NoDialog) {
    $friendly = Get-DeploymentUserFailure $Root $NativeExitCode $Stage $Message
    [void][IO.Directory]::CreateDirectory((Join-Path $Root 'runtime'))
    [IO.File]::WriteAllText((Join-Path $Root 'runtime\deployment-user-failure.json'), ($friendly | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    [Console]::Error.WriteLine($friendly.reason + "`n" + $friendly.next_step)
    if ($NoDialog -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected -or [Console]::IsErrorRedirected -or -not [Environment]::UserInteractive) { return }
    Add-Type -AssemblyName System.Windows.Forms
    $form = New-Object System.Windows.Forms.Form
    $form.Text = '选品工作台 · 部署未完成'; $form.Width=590; $form.Height=300; $form.StartPosition='CenterScreen'
    $label=New-Object System.Windows.Forms.Label; $label.Left=22; $label.Top=20; $label.Width=535; $label.Height=145
    $label.Text=$friendly.reason+"`r`n`r`n"+$friendly.next_step; $form.Controls.Add($label)
    $button=New-Object System.Windows.Forms.Button; $button.Text='导出诊断'; $button.Left=225; $button.Top=185; $button.Width=130
    $diagnosticsLauncher=Join-Path $Root 'scripts\export_deployment_diagnostics.ps1'
    $button.Add_Click({
        try {
            & $diagnosticsLauncher
        } catch { [void][System.Windows.Forms.MessageBox]::Show('诊断导出未完成。请把此窗口截图交给维护者，不要重复安装依赖。','未能导出') }
    }.GetNewClosure())
    $form.Controls.Add($button); [void]$form.ShowDialog(); $form.Dispose()
}
