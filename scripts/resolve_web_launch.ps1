function Resolve-WebLaunchRoot {
    param([Parameter(Mandatory=$true)][string]$SourceRoot,
          [Parameter(Mandatory=$true)][string]$EntryName)
    $current = [IO.Path]::GetFullPath($SourceRoot)
    $visited = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    for ($hop = 0; $hop -lt 16; $hop++) {
        if (-not $visited.Add($current)) { throw 'Local web launch configuration contains a cycle.' }
        $configPath = Join-Path $current 'start-web.local.json'
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return $current }
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.target_root -isnot [string] -or [string]::IsNullOrWhiteSpace($config.target_root)) {
            throw 'start-web.local.json requires a nonempty target_root string.'
        }
        $target = $config.target_root.Trim()
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $current $target }
        $target = [IO.Path]::GetFullPath($target).TrimEnd([IO.Path]::DirectorySeparatorChar)
        if ($visited.Contains($target)) { throw 'Local web launch target points to itself or creates a cycle.' }
        foreach ($required in @('start-web.ps1', $EntryName)) {
            if (-not (Test-Path -LiteralPath (Join-Path $target $required) -PathType Leaf)) {
                throw "Local web launch target is missing $required."
            }
        }
        $current = $target
    }
    throw 'Local web launch configuration exceeds the allowed redirect count.'
}
