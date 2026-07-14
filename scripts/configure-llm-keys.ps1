param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot ".env"

if ((Test-Path -LiteralPath $EnvFile) -and -not $Force) {
    throw ".env already exists. Re-run with -Force only if you intend to replace it."
}

function ConvertTo-PlainText([Security.SecureString]$SecureValue) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$OpenAISecure = Read-Host "OpenAI/GPT API Key (input hidden)" -AsSecureString
$DeepSeekSecure = Read-Host "DeepSeek API Key (input hidden)" -AsSecureString
$OpenAIKey = ConvertTo-PlainText $OpenAISecure
$DeepSeekKey = ConvertTo-PlainText $DeepSeekSecure
$BundledNode = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$NodeExecutable = if (Test-Path -LiteralPath $BundledNode) { $BundledNode } else { "node" }

if ([string]::IsNullOrWhiteSpace($OpenAIKey) -and [string]::IsNullOrWhiteSpace($DeepSeekKey)) {
    throw "At least one API key is required."
}

$Lines = @(
    "QUANTLAB_LLM_PROVIDER=auto"
    "QUANTLAB_LLM_MODEL="
    "QUANTLAB_LLM_BASE_URL="
    "QUANTLAB_OPENAI_MODEL=gpt-5.6-terra"
    "QUANTLAB_OPENAI_REASONING_EFFORT=medium"
    "QUANTLAB_DEEPSEEK_MODEL=deepseek-chat"
    "QUANTLAB_OPENAI_ENABLED=true"
    "QUANTLAB_DEEPSEEK_ENABLED=true"
    "QUANTLAB_OPENAI_BASE_URL=https://code-plan.site/v1"
    "QUANTLAB_DEEPSEEK_BASE_URL=https://api.deepseek.com"
    "OPENAI_API_KEY=$OpenAIKey"
    "DEEPSEEK_API_KEY=$DeepSeekKey"
    "OPENAI_API_KEYS="
    "DEEPSEEK_API_KEYS="
    "QUANTLAB_NODE_EXECUTABLE=$NodeExecutable"
)

[IO.File]::WriteAllLines($EnvFile, $Lines, [Text.UTF8Encoding]::new($false))
$OpenAIKey = $null
$DeepSeekKey = $null
Write-Host "Saved local LLM configuration to $EnvFile. Keys were not printed."
Write-Host "Run: quantlab llm-status"
Write-Host "Then: quantlab llm-replay --suite smoke --runs 1"
