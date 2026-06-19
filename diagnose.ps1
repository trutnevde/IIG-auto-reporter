# -*- coding: utf-8 -*-
# ДИАГНОСТИКА (read-only): проверяет всех клиентов через Reports API Яндекс.Директа.
# НИЧЕГО не отправляет в Telegram. Показывает по каждому клиенту:
#   OK  — отчёт получен (с краткими цифрами), или
#   FAIL — HTTP-код + error_code/error_string из ответа API.
# Внизу — заголовок Units (остаток баллов): формат "списано_за_запрос / осталось / суточный_лимит".
# Запуск: run_diagnose.bat  (или powershell -ExecutionPolicy Bypass -File diagnose.ps1)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$OutputEncoding = [System.Text.Encoding]::UTF8

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $here "config.json"
$secPath = Join-Path $here "secrets.json"
$logPath = Join-Path $here "diagnose.log"

function Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding UTF8
}

if (-not (Test-Path $cfgPath)) { Log "НЕТ config.json рядом со скриптом: $cfgPath"; exit 1 }
if (-not (Test-Path $secPath)) { Log "НЕТ secrets.json: $secPath (скопируй secrets.example.json в secrets.json и впиши токены)"; exit 1 }

$cfg   = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
$sec   = Get-Content -Raw -Encoding UTF8 $secPath | ConvertFrom-Json
$token = $sec.yandex_oauth_token

# период: прошлая неделя (Пн..Вс) — как в основном скрипте
$today      = (Get-Date).Date
$deltaToMon = (([int]$today.DayOfWeek + 6) % 7)
$lastMonday = $today.AddDays(-$deltaToMon - 7)
$lastSunday = $lastMonday.AddDays(6)
$dateFrom   = $lastMonday.ToString("yyyy-MM-dd")
$dateTo     = $lastSunday.ToString("yyyy-MM-dd")

Log "=== ДИАГНОСТИКА (read-only, без Telegram) ==="
Log "Период: $dateFrom .. $dateTo, клиентов: $($cfg.clients.Count)"

$url = "https://api.direct.yandex.com/json/v5/reports"

function Get-Units($headers) {
    # вытаскивает заголовок Units из ответа (PS 5.1 и PS 7)
    try { return ($headers["Units"] | Select-Object -First 1) } catch {}
    try { return ($headers.GetValues("Units") -join "") } catch {}
    return $null
}

function Try-Report($login) {
    $headers = @{
        "Authorization"       = "Bearer $token"
        "Client-Login"        = $login
        "Accept-Language"     = "ru"
        "processingMode"      = "auto"
        "returnMoneyInMicros" = "false"
        "skipReportHeader"    = "true"
        "skipReportSummary"   = "true"
    }
    $body = @{
        params = @{
            SelectionCriteria = @{ DateFrom = $dateFrom; DateTo = $dateTo }
            FieldNames        = @("Impressions","Clicks","Cost","Conversions")
            ReportName        = "diag_{0}_{1}" -f $login, ([DateTimeOffset]::Now.ToUnixTimeSeconds())
            ReportType        = "ACCOUNT_PERFORMANCE_REPORT"
            DateRangeType     = "CUSTOM_DATE"
            Format            = "TSV"
            IncludeVAT        = "YES"
            IncludeDiscount   = "NO"
        }
    } | ConvertTo-Json -Depth 6

    for ($i = 0; $i -lt 10; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri $url -Method Post -Headers $headers -Body $body `
                        -ContentType "application/json; charset=utf-8" -UseBasicParsing
        }
        catch {
            # сюда попадают HTTP 4xx/5xx
            $http = $null; try { $http = [int]$_.Exception.Response.StatusCode } catch {}
            $raw  = $_.ErrorDetails.Message
            if (-not $raw -and $_.Exception.Response) {
                try {
                    $sr  = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                    $raw = $sr.ReadToEnd(); $sr.Close()
                } catch {}
            }
            if (-not $raw) { $raw = $_.Exception.Message }
            $units = $null; try { $units = Get-Units $_.Exception.Response.Headers } catch {}
            $ec = $null; $es = $null
            try {
                $j  = $raw | ConvertFrom-Json
                $ec = $j.error.error_code
                $es = (@($j.error.error_string, $j.error.error_detail) | Where-Object { $_ }) -join " — "
            } catch {}
            return [pscustomobject]@{ ok=$false; http=$http; errcode=$ec; errstr=$es; units=$units; raw=$raw }
        }

        $units = Get-Units $resp.Headers
        if ($resp.StatusCode -eq 200) {
            $lines = ($resp.Content -split "`n") | Where-Object { $_.Trim() -ne "" }
            $summary = ""
            if ($lines.Count -ge 2) {
                $h = $lines[0] -split "`t"; $v = $lines[1] -split "`t"
                $row = @{}; for ($k=0; $k -lt $h.Count; $k++) { $row[$h[$k].Trim()] = $v[$k] }
                $summary = "показы=$($row['Impressions']) клики=$($row['Clicks']) расход=$($row['Cost'])"
            } else { $summary = "нет данных за период" }
            return [pscustomobject]@{ ok=$true; http=200; summary=$summary; units=$units }
        }
        elseif ($resp.StatusCode -eq 201 -or $resp.StatusCode -eq 202) {
            $wait = 3; if ($resp.Headers["retryIn"]) { $wait = [int]$resp.Headers["retryIn"] }
            Start-Sleep -Seconds $wait
        }
        else {
            return [pscustomobject]@{ ok=$false; http=[int]$resp.StatusCode; raw=$resp.Content; units=$units }
        }
    }
    return [pscustomobject]@{ ok=$false; http=$null; errstr="timeout: отчёт не готов за отведённое время"; raw="" }
}

$okc = 0; $fail = 0; $lastUnits = $null
foreach ($c in $cfg.clients) {
    $r = Try-Report $c.login
    if ($r.units) { $lastUnits = $r.units }
    if ($r.ok) {
        $okc++
        Log ("OK   {0} ({1}) | {2}" -f $c.name, $c.login, $r.summary)
    } else {
        $fail++
        $code = if ($r.errcode) { "код $($r.errcode)" } else { "HTTP $($r.http)" }
        $txt  = if ($r.errstr)  { $r.errstr } else { (($r.raw -replace '\s+',' ').Trim()) }
        Log ("FAIL {0} ({1}): {2} | {3}" -f $c.name, $c.login, $code, $txt)
    }
}

Log "--------------------------------------------------"
Log ("ИТОГ: OK $okc, FAIL $fail из $($cfg.clients.Count).")
Log ("Остаток баллов (заголовок Units, последнее значение): {0}" -f $lastUnits)
Log "Формат Units: списано_за_запрос / осталось / суточный_лимит"
Log "Если 'осталось' близко к 0 — упёрлись в суточный лимит баллов; сбрасывается раз в сутки."
