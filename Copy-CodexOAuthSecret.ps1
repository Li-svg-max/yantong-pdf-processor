[CmdletBinding()]
param(
  [string]$AuthDirectory = "C:\Users\16950\Desktop\研通\services\CLIProxyAPI-v7.2.144\auth"
)

$ErrorActionPreference = "Stop"

$authFile = Get-ChildItem -LiteralPath $AuthDirectory -Filter "codex-*.json" -File |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

if (-not $authFile) {
  throw "No codex-*.json OAuth credential was found in $AuthDirectory"
}

$raw = [IO.File]::ReadAllText($authFile.FullName)
$credential = $raw | ConvertFrom-Json
if ($credential.type -ne "codex" -or [string]::IsNullOrWhiteSpace($credential.refresh_token)) {
  throw "The selected file is not a valid Codex OAuth credential."
}

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($raw))
Set-Clipboard -Value $encoded

[PSCustomObject]@{
  CopiedToClipboard = $true
  SourceFile = $authFile.Name
  Expires = $credential.expired
  EncodedLength = $encoded.Length
}
