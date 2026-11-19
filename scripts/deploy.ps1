<#
Builds the brain image, pushes it to ECR, and creates (first time) or updates the Lambda + public Function URL.
Needs: Docker Desktop running, AWS CLI v2 configured (aws configure), backend\.env filled in.

  .\scripts\deploy.ps1 -Tag v1            (next time: -Tag v2, v3 ... never reuse a tag)
#>
param(
  [Parameter(Mandatory = $true)][string]$Tag,
  [string]$Region = "me-central-1",
  [string]$Repo = "digitaltwin-brain",
  [string]$Function = "digitaltwin-brain"
)

$ErrorActionPreference = "Continue"
function Must($what) { if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $what" -ForegroundColor Red; exit 1 } }

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backend = Join-Path $root "backend"
$envFile = Join-Path $backend ".env"
if (-not (Test-Path $envFile)) { Write-Host "Missing backend\.env (copy .env.example)" -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $backend "kb\ch1.md"))) { Write-Host "No notes in backend\kb - run scripts\extract.py first" -ForegroundColor Red; exit 1 }

$account = (aws sts get-caller-identity --query Account --output text); Must "aws sts (run 'aws configure')"
$account = $account.Trim()
$registry = "$account.dkr.ecr.$Region.amazonaws.com"
$image = "$registry/${Repo}:$Tag"

Write-Host "== 1/4 ECR repository" -ForegroundColor Cyan
$null = aws ecr describe-repositories --repository-names $Repo --region $Region 2>&1
if ($LASTEXITCODE -ne 0) { aws ecr create-repository --repository-name $Repo --region $Region | Out-Null; Must "create ECR repo" }

Write-Host "== 2/4 Build + push $image" -ForegroundColor Cyan
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry; Must "docker login"
docker buildx build --platform linux/amd64 --provenance=false -t $image --load $backend; Must "docker build"
docker push $image; Must "docker push"

Write-Host "== 3/4 Environment variables from backend\.env" -ForegroundColor Cyan
$vars = @{}
Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $k, $v = $line.Split("=", 2)
    $vars[$k.Trim()] = $v.Trim().Trim('"')
  }
}
$envPath = Join-Path $env:TEMP "digitaltwin-env.json"
[System.IO.File]::WriteAllText($envPath, (@{ Variables = $vars } | ConvertTo-Json -Compress))

Write-Host "== 4/4 Lambda" -ForegroundColor Cyan
$null = aws lambda get-function --function-name $Function --region $Region 2>&1
if ($LASTEXITCODE -eq 0) {
  aws lambda update-function-code --function-name $Function --image-uri $image --region $Region | Out-Null; Must "update code"
  aws lambda wait function-updated --function-name $Function --region $Region
  aws lambda update-function-configuration --function-name $Function --environment "file://$envPath" --timeout 60 --memory-size 1024 --region $Region | Out-Null; Must "update config"
} else {
  $roleName = "$Function-role"
  $roleArn = aws iam get-role --role-name $roleName --query Role.Arn --output text 2>&1
  if ($LASTEXITCODE -ne 0) {
    $trust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    $trustPath = Join-Path $env:TEMP "digitaltwin-trust.json"
    [System.IO.File]::WriteAllText($trustPath, $trust)
    $roleArn = aws iam create-role --role-name $roleName --assume-role-policy-document "file://$trustPath" --query Role.Arn --output text; Must "create IAM role"
    aws iam attach-role-policy --role-name $roleName --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole; Must "attach logs policy"
    Write-Host "Waiting 15s for the new IAM role..."; Start-Sleep -Seconds 15
  }
  $roleArn = "$roleArn".Trim()
  aws lambda create-function --function-name $Function --package-type Image --code ImageUri=$image --role $roleArn `
    --timeout 60 --memory-size 1024 --architectures x86_64 --environment "file://$envPath" --region $Region | Out-Null; Must "create function"
  aws lambda wait function-active-v2 --function-name $Function --region $Region
  aws lambda create-function-url-config --function-name $Function --auth-type NONE --region $Region | Out-Null; Must "create Function URL"
  aws lambda add-permission --function-name $Function --statement-id FunctionUrlPublic --action lambda:InvokeFunctionUrl `
    --principal "*" --function-url-auth-type NONE --region $Region | Out-Null; Must "URL permission"
  # Newer AWS accounts also need lambda:InvokeFunction for public Function URLs.
  $null = aws lambda add-permission --function-name $Function --statement-id FunctionUrlInvoke --action lambda:InvokeFunction `
    --principal "*" --invoked-via-function-url --region $Region 2>&1
}

$url = (aws lambda get-function-url-config --function-name $Function --region $Region --query FunctionUrl --output text).Trim().TrimEnd("/")
Write-Host ""
Write-Host "Deployed $image" -ForegroundColor Green
Write-Host "API URL: $url" -ForegroundColor Green
Write-Host "Test:    $url/api/health"
Write-Host "Put this in frontend\config.js -> apiBase: `"$url`""
