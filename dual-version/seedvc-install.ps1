<#
.SYNOPSIS
Install the voice swap engine (Seed-VC) on the app PC from a folder made by seedvc-download.ps1.

.DESCRIPTION
Works offline. Verifies the copied files, creates <Runtime>\seed-vc with its own Python
environment (PyTorch 2.7.1 + CUDA 12.8 for the RTX 5070), the pinned Seed-VC source and
model weights, then runs a short GPU self-test. Sing Studio enables voice swap only after
the self-test passes (it writes seed-vc\install.json last). The app, songs, works, settings
and the YuE2 / UVR / Whisper environments are not touched; nothing is uploaded.
Run it while the queue is idle.

.EXAMPLE
.\seedvc-install.ps1 -Bundle E:\seedvc-bundle -Runtime D:\music\local-test
#>
param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$BasePython,
    [switch]$Replace
)
$ErrorActionPreference='Stop'
$commit='51383efd921027683c89e5348211d93ff12ac2a8'
function Invoke-Native([string]$Exe,[string[]]$Arguments){
    & $Exe @Arguments
    if($LASTEXITCODE -ne 0){throw ("Command failed ($LASTEXITCODE): $Exe "+($Arguments -join ' '))}
}
function Write-Utf8([string]$Path,[string]$Text){[System.IO.File]::WriteAllText($Path,$Text,(New-Object System.Text.UTF8Encoding $false))}

$Bundle=(Resolve-Path -LiteralPath $Bundle).Path
$Runtime=(Resolve-Path -LiteralPath $Runtime).Path
$info=Get-Content -LiteralPath (Join-Path $Bundle 'bundle.json') -Raw | ConvertFrom-Json
if($info.seedvc_commit -ne $commit){throw "The bundle holds Seed-VC $($info.seedvc_commit); this release needs $commit. Download again with this release's seedvc-download.ps1."}
Write-Output ('Checking {0} copied files' -f @($info.files).Count)
foreach($file in $info.files){
    $path=Join-Path $Bundle $file.path
    if(-not (Test-Path -LiteralPath $path)){throw "Missing from the bundle: $($file.path)"}
    if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $file.sha256){throw "Damaged during the copy: $($file.path). Copy the bundle again."}
}

if(-not $BasePython){
    # The new environment uses the same Python install as the existing music environment.
    $config=Join-Path $Runtime 'ace-env\pyvenv.cfg'
    if(-not (Test-Path -LiteralPath $config)){throw 'No ace-env under -Runtime; pass -BasePython <path to python.exe>.'}
    $pythonHome=((Get-Content -LiteralPath $config | Where-Object {$_ -match '^\s*home\s*='} | Select-Object -First 1) -replace '^\s*home\s*=\s*','').Trim()
    $BasePython=Join-Path $pythonHome 'python.exe'
}
$found=(& $BasePython -c "import struct,sys;print('%d.%d-%d'%(sys.version_info[0],sys.version_info[1],struct.calcsize('P')*8))").Trim()
if($found -ne "$($info.python_version)-64"){throw "The bundle was made for 64-bit Python $($info.python_version) but $BasePython is $found. On the download PC run seedvc-download.ps1 -PythonVersion $($found.Split('-')[0])."}

$target=Join-Path $Runtime 'seed-vc'
if(Test-Path -LiteralPath $target){
    if(-not $Replace){throw "$target already exists. Re-run with -Replace to move it aside and install again."}
    $old=$target+'.old-'+(Get-Date -Format 'yyyyMMdd-HHmmss')
    Move-Item -LiteralPath $target -Destination $old
    Write-Output "Previous engine moved to $old"
}
New-Item -ItemType Directory -Path $target | Out-Null
Write-Output 'Copying Seed-VC source and model weights'
Copy-Item -LiteralPath (Join-Path $Bundle 'seed-vc') -Destination (Join-Path $target 'seed-vc') -Recurse

Write-Output 'Creating the separate Python environment (offline)'
$envDir=Join-Path $target 'env'
Invoke-Native $BasePython @('-m','venv',$envDir)
$py=Join-Path $envDir 'Scripts\python.exe'
Invoke-Native $py @('-m','pip','install','--disable-pip-version-check','--no-index','--find-links',(Join-Path $Bundle 'wheels'),'-r',(Join-Path $Bundle 'seedvc-requirements.txt'))

Write-Output 'GPU self-test: load the models offline and convert a 6-second test tone'
$report=Join-Path $target 'self-test.json'
$offline=@{HF_HUB_OFFLINE='1';TRANSFORMERS_OFFLINE='1';HF_HUB_DISABLE_TELEMETRY='1';PYTHONUTF8='1';PYTHONNOUSERSITE='1'}
$saved=@{}
foreach($key in $offline.Keys){$saved[$key]=[Environment]::GetEnvironmentVariable($key);[Environment]::SetEnvironmentVariable($key,$offline[$key])}
try {Invoke-Native $py @((Join-Path $PSScriptRoot 'seedvc_runner.py'),'--seedvc',(Join-Path $target 'seed-vc'),'--self-test','--report',$report)}
finally {foreach($key in $saved.Keys){[Environment]::SetEnvironmentVariable($key,$saved[$key])}}
$test=Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
if(-not $test.self_test_passed -or $test.device -ne 'cuda'){throw "Self-test did not pass on the GPU; see $report"}

$install=[ordered]@{complete=$true;seedvc_commit=$commit;python_version=$info.python_version;base_python=$BasePython;
    torch=$test.torch;cuda=$test.cuda;gpu=$test.gpu;capability=$test.capability;peak_vram_mib=$test.peak_vram_mib;
    load_seconds=$test.load_seconds;convert_seconds=$test.convert_seconds;bundle_created_at=$info.created_at;installed_at=(Get-Date -Format o)}
Write-Utf8 (Join-Path $target 'install.json') ($install | ConvertTo-Json -Depth 4)
Write-Output ('Installed Seed-VC for voice swap on {0} (torch {1}, CUDA {2}, compute {3}). Refresh Sing Studio to use it.' -f $test.gpu,$test.torch,$test.cuda,(@($test.capability) -join '.'))
