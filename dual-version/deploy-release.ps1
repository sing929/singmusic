param([Parameter(Mandatory=$true)][string]$AppRoot)
$ErrorActionPreference='Stop'
$app=(Resolve-Path -LiteralPath $AppRoot).Path
$workspace=Split-Path -Parent $PSScriptRoot
$project=Split-Path -Parent $app
$python=Join-Path $project 'local-test\ace-env\Scripts\python.exe'
$pythonw=Join-Path $project 'local-test\ace-env\Scripts\pythonw.exe'
$release=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.json') -Raw | ConvertFrom-Json
$client=Join-Path $workspace 'sing-studio-batch-preview\dist\client'
if(-not (Test-Path -LiteralPath (Join-Path $app 'service.py')) -or -not (Test-Path -LiteralPath $pythonw)){throw 'Existing application/runtime required'}
if(-not (Test-Path -LiteralPath (Join-Path $client 'index.html'))){throw 'Build the frontend first'}
$validation=Get-Content -LiteralPath (Join-Path $PSScriptRoot ('VALIDATION-v'+$release.version+'.json')) -Raw | ConvertFrom-Json
if(-not $validation.passed){throw 'Release validation must pass first'}
function Assert-Idle {
    foreach($path in (Get-ChildItem -Path (Join-Path $app 'data\jobs\*\job.json') -File)){
        $job=Get-Content -LiteralPath $path.FullName -Raw | ConvertFrom-Json
        if($job.state -in @('running','queued')){throw 'Active user jobs: defer update until queue is idle'}
    }
}
Assert-Idle
$files=@('service.py','engine.py','yue2_engine.py','generation_controls.py','release.json')
foreach($name in $files | Where-Object {$_ -like '*.py'}){
    & $python -m py_compile (Join-Path $PSScriptRoot $name)
    if($LASTEXITCODE -ne 0){throw 'Python syntax validation failed'}
}
$backup=Join-Path $app ('backups\before-v'+$release.version+'-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup | Out-Null
foreach($name in $files){if(Test-Path -LiteralPath (Join-Path $app $name)){Copy-Item -LiteralPath (Join-Path $app $name) -Destination $backup}}
Copy-Item -LiteralPath (Join-Path $app 'frontend') -Destination $backup -Recurse
if(Test-Path -LiteralPath (Join-Path $project 'HANDOFF.md')){Copy-Item -LiteralPath (Join-Path $project 'HANDOFF.md') -Destination $backup}
$dataPaths=@((Join-Path $app 'data\tracks\*\track.json'),(Join-Path $app 'data\jobs\*\job.json'))
if(Test-Path -LiteralPath (Join-Path $app 'data\settings.json')){$dataPaths+=(Join-Path $app 'data\settings.json')}
$hashes=@(Get-ChildItem -Path $dataPaths -File | ForEach-Object {@{path=$_.FullName;sha256=(Get-FileHash -LiteralPath $_.FullName).Hash}})
Assert-Idle
$serverPidPath=Join-Path $app 'data\server.pid'
if(Test-Path -LiteralPath $serverPidPath){
    $serverPid=[int](Get-Content -LiteralPath $serverPidPath)
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
    if($proc){
        if(-not $proc.CommandLine.Contains((Join-Path $app 'service.py'))){throw 'Server PID belongs to another process'}
        Assert-Idle
        Stop-Process -Id $serverPid
        Wait-Process -Id $serverPid -ErrorAction SilentlyContinue
    }
}
try {
    foreach($name in $files){Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $app $name) -Force}
    Copy-Item -Path (Join-Path $client '*') -Destination (Join-Path $app 'frontend') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $workspace 'CHANGELOG.md') -Destination (Join-Path $app 'CHANGELOG.md') -Force
} catch {
    foreach($name in $files){if(Test-Path -LiteralPath (Join-Path $backup $name)){Copy-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $app $name) -Force}}
    Copy-Item -Path (Join-Path $backup 'frontend\*') -Destination (Join-Path $app 'frontend') -Recurse -Force
    throw
} finally {
    Start-Process -FilePath $pythonw -ArgumentList ('"'+(Join-Path $app 'service.py')+'"') -WorkingDirectory $app -WindowStyle Hidden
}
$verified=$false
for($i=0;$i -lt 30;$i++){
    try {$state=Invoke-RestMethod 'http://127.0.0.1:18765/api/state';if($state.release.version -eq $release.version){$verified=$true;break}} catch {}
    Start-Sleep -Milliseconds 300
}
if(-not $verified){throw ('Installed service did not report expected version; backup: '+$backup)}
foreach($entry in $hashes){if((Get-FileHash -LiteralPath $entry.path).Hash -ne $entry.sha256){throw ('Existing data changed: '+$entry.path)}}
$report=Join-Path $workspace '.validation\deployment.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
@{version=$release.version;backup=$backup;installed_at=(Get-Date -Format o);data_files_unchanged=$hashes.Count;hashes=$hashes;passed=$true} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding utf8NoBOM
Write-Output ('Installed v'+$release.version+'; '+$hashes.Count+' existing metadata files unchanged; backup: '+$backup)
