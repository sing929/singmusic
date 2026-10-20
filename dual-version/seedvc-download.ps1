<#
.SYNOPSIS
Download the voice swap engine (Seed-VC) into one portable folder on any Windows PC with internet.

.DESCRIPTION
Run this on the PC that has internet (for example the i5 PC), copy the whole folder to the app
PC, then run seedvc-install.ps1 there; the app PC needs no internet. Only public code, packages
and model weights are downloaded; no audio, lyrics or settings leave either PC. About 6 GB:
  - Seed-VC source at a pinned commit (github.com/Plachtaa/seed-vc, GPL-3.0)
  - Python wheels from PyPI and the PyTorch CUDA 12.8 index; the two pure-Python source
    packages are built into wheels here, so the app PC needs no compiler
  - Model weights from Hugging Face: Seed-VC singing model, RMVPE, CAM++, BigVGAN 44 kHz, Whisper-small
The Python minor version used here must equal the app PC's. Check it there with
local-test\ace-env\Scripts\python.exe --version (default here: 3.11).

.EXAMPLE
.\seedvc-download.ps1 -Bundle D:\seedvc-bundle
#>
param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [string]$PythonVersion='3.11',
    [string]$Python
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$commit='51383efd921027683c89e5348211d93ff12ac2a8'
$requirements=Join-Path $PSScriptRoot 'seedvc-requirements.txt'
function Invoke-Native([string]$Exe,[string[]]$Arguments){
    & $Exe @Arguments
    if($LASTEXITCODE -ne 0){throw ("Command failed ($LASTEXITCODE): $Exe "+($Arguments -join ' '))}
}
function Write-Utf8([string]$Path,[string]$Text){[System.IO.File]::WriteAllText($Path,$Text,(New-Object System.Text.UTF8Encoding $false))}

if(-not $Python){
    if(-not (Get-Command py -ErrorAction SilentlyContinue)){throw "Install 64-bit Python $PythonVersion from python.org (with the py launcher), or pass -Python <path to python.exe>."}
    $Python=(& py "-$PythonVersion" -c 'import sys;print(sys.executable)')
    if($LASTEXITCODE -ne 0 -or -not $Python){throw "Python $PythonVersion was not found. Install 64-bit Python $PythonVersion from python.org, or pass -Python <path to python.exe>."}
    $Python=$Python.Trim()
}
$found=(& $Python -c "import struct,sys;print('%d.%d-%d'%(sys.version_info[0],sys.version_info[1],struct.calcsize('P')*8))").Trim()
if($found -ne "$PythonVersion-64"){throw "Need 64-bit Python $PythonVersion to match the app PC; $Python is $found."}

New-Item -ItemType Directory -Path $Bundle -Force | Out-Null
$Bundle=(Resolve-Path -LiteralPath $Bundle).Path
$wheels=Join-Path $Bundle 'wheels'
$source=Join-Path $Bundle 'seed-vc'
$checkpoints=Join-Path $source 'checkpoints'
$envDir=Join-Path $Bundle '_download-env'
foreach($path in @($envDir,(Join-Path $Bundle '_source'))){if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Recurse -Force}}

Write-Output 'Creating a temporary download environment'
Invoke-Native $Python @('-m','venv',$envDir)
$py=Join-Path $envDir 'Scripts\python.exe'
Invoke-Native $py @('-m','pip','install','--disable-pip-version-check','--quiet','--upgrade','pip','wheel','setuptools')

Write-Output 'Downloading Python packages (PyTorch with CUDA 12.8 alone is about 3.3 GB)'
Invoke-Native $py @('-m','pip','wheel','--disable-pip-version-check','--wheel-dir',$wheels,'-r',$requirements)
Copy-Item -LiteralPath $requirements -Destination (Join-Path $Bundle 'seedvc-requirements.txt') -Force

Write-Output "Downloading Seed-VC source at $commit"
$zip=Join-Path $Bundle "seed-vc-$commit.zip"
Invoke-WebRequest -Uri "https://codeload.github.com/Plachtaa/seed-vc/zip/$commit" -OutFile $zip -UseBasicParsing
$extract=Join-Path $Bundle '_source'
Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
# Keep weights fetched by an earlier, interrupted run; replace only the code.
$kept=$null
if(Test-Path -LiteralPath $checkpoints){$kept=Join-Path $Bundle '_checkpoints';Move-Item -LiteralPath $checkpoints -Destination $kept}
if(Test-Path -LiteralPath $source){Remove-Item -LiteralPath $source -Recurse -Force}
Move-Item -LiteralPath (Join-Path $extract "seed-vc-$commit") -Destination $source
if($kept){Move-Item -LiteralPath $kept -Destination $checkpoints}
Remove-Item -LiteralPath $extract -Recurse -Force
Remove-Item -LiteralPath $zip -Force

Write-Output 'Downloading model weights from Hugging Face (about 2 GB)'
Invoke-Native $py @('-m','pip','install','--disable-pip-version-check','--quiet','--no-index','--find-links',$wheels,'huggingface-hub==0.28.1')
# The same cache folders Seed-VC's inference.py reads when it runs from its own folder:
# ./checkpoints for its own hf_utils downloads, ./checkpoints/hf_cache for BigVGAN and Whisper.
$fetch=@'
import os, sys
os.chdir(sys.argv[1])
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
from huggingface_hub import hf_hub_download, snapshot_download
for repo, name in (('Plachta/Seed-VC', 'DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth'),
                   ('Plachta/Seed-VC', 'config_dit_mel_seed_uvit_whisper_base_f0_44k.yml'),
                   ('lj1995/VoiceConversionWebUI', 'rmvpe.pt'),
                   ('funasr/campplus', 'campplus_cn_common.bin')):
    print(hf_hub_download(repo_id=repo, filename=name, cache_dir='./checkpoints'), flush=True)
print(snapshot_download('nvidia/bigvgan_v2_44khz_128band_512x', allow_patterns=['config.json', 'bigvgan_generator.pt'],
                        cache_dir='./checkpoints/hf_cache'), flush=True)
print(snapshot_download('openai/whisper-small', allow_patterns=['*.json', '*.txt', 'model.safetensors'],
                        cache_dir='./checkpoints/hf_cache'), flush=True)
'@
$script=Join-Path $Bundle '_fetch_models.py'
Write-Utf8 $script $fetch
try {Invoke-Native $py @($script,$source)} finally {Remove-Item -LiteralPath $script -Force}

# Hugging Face may store files as symlinks into blobs\; copy them as plain files so the folder
# survives USB drives and network copies, then drop the duplicate blobs.
foreach($link in @(Get-ChildItem -LiteralPath $checkpoints -Recurse -File | Where-Object {$_.LinkType -eq 'SymbolicLink'})){
    $target=[System.IO.Path]::GetFullPath((Join-Path $link.DirectoryName ([string]@($link.Target)[0])))
    $copy=$link.FullName+'.copy'
    Copy-Item -LiteralPath $target -Destination $copy
    Remove-Item -LiteralPath $link.FullName -Force
    Move-Item -LiteralPath $copy -Destination $link.FullName
}
foreach($blobs in @(Get-ChildItem -LiteralPath $checkpoints -Recurse -Directory -Filter 'blobs')){Remove-Item -LiteralPath $blobs.FullName -Recurse -Force}
foreach($lock in @(Get-ChildItem -LiteralPath $checkpoints -Recurse -Directory -Filter '.locks')){Remove-Item -LiteralPath $lock.FullName -Recurse -Force}

Write-Output 'Recording checksums so the app PC can detect a damaged copy'
$files=@()
foreach($file in @(Get-ChildItem -LiteralPath $wheels -File)+@(Get-ChildItem -LiteralPath $checkpoints -Recurse -File)){
    $files+=[ordered]@{path=$file.FullName.Substring($Bundle.Length+1);bytes=$file.Length;sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash}
}
$info=[ordered]@{seedvc_commit=$commit;python_version=$PythonVersion;created_at=(Get-Date -Format o);files=$files}
Write-Utf8 (Join-Path $Bundle 'bundle.json') ($info | ConvertTo-Json -Depth 5)
Remove-Item -LiteralPath $envDir -Recurse -Force
$total=($files | ForEach-Object {$_.bytes} | Measure-Object -Sum).Sum
Write-Output ('Done: {0} files, {1:N1} GB in {2}. Copy this folder to the app PC and run seedvc-install.ps1 there.' -f $files.Count,($total/1GB),$Bundle)
