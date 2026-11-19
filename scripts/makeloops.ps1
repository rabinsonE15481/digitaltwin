<#
Cuts the teacher's 1-minute video into the avatar files (needs ffmpeg on PATH: winget install ffmpeg).
Watch the video first and note: a stretch where she is TALKING, and a stretch where she is quiet / nodding.

  .\scripts\makeloops.ps1 -Video "C:\path\teacher.mp4" -TalkStart 00:00:12 -TalkSeconds 8 -IdleStart 00:00:45 -IdleSeconds 6
#>
param(
  [Parameter(Mandatory = $true)][string]$Video,
  [string]$TalkStart = "00:00:10",
  [int]$TalkSeconds = 8,
  [string]$IdleStart = "00:00:40",
  [int]$IdleSeconds = 6,
  [string]$FaceAt = "00:00:05"
)

$media = Join-Path $PSScriptRoot "..\frontend\media"
New-Item -ItemType Directory -Force -Path $media | Out-Null
$vf = "scale=720:-2,fps=25"

ffmpeg -y -ss $TalkStart -t $TalkSeconds -i $Video -an -vf $vf -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart (Join-Path $media "talkloop.mp4")
ffmpeg -y -ss $IdleStart -t $IdleSeconds -i $Video -an -vf $vf -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart (Join-Path $media "idleloop.mp4")
ffmpeg -y -ss $FaceAt -i $Video -frames:v 1 -q:v 2 (Join-Path $media "face.jpg")

# Her voice sample - kept for version 2 (cloned voice). Not used by version 1.
ffmpeg -y -i $Video -vn -ac 1 -ar 24000 (Join-Path $PSScriptRoot "..\voicesample.wav")

Write-Host "Done. Files are in frontend\media. Open frontend\index.html via a local server to preview." -ForegroundColor Green
