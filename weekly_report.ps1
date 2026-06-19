# -*- coding: utf-8 -*-
# Еженедельный отчёт Яндекс.Директ -> Telegram (PowerShell, без установки Python)
# Запуск: run_weekly_report.bat  (или powershell -ExecutionPolicy Bypass -File weekly_report.ps1)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$OutputEncoding = [System.Text.Encoding]::UTF8

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $here "config.json"
$secPath = Join-Path $here "secrets.json"
$logPath = Join-Path $here "report.log"

function Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding UTF8
}

if (-not (Test-Path $cfgPath)) { Log "НЕТ config.json рядом со скриптом: $cfgPath"; exit 1 }
if (-not (Test-Path $secPath)) { Log "НЕТ secrets.json: $secPath (скопируй secrets.example.json в secrets.json и впиши токены)"; exit 1 }

$cfg       = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
$sec       = Get-Content -Raw -Encoding UTF8 $secPath | ConvertFrom-Json
$token     = $sec.yandex_oauth_token
$botToken  = $sec.telegram_bot_token
$intro     = if ($cfg.intro) { $cfg.intro } else { "Отчёт за прошлую неделю." }
$ownerChat = $cfg.owner_chat_id
# Пауза между отправками в Telegram (сек). Защита от лимита 429.
# Если много клиентов шлют в ОДИН чат — держи 3-4 сек (лимит группы ~20 сообщений/мин).
# Если у каждого клиента свой чат — хватит 0.1-0.5 сек (глобальный лимит ~30/сек).
$sendDelay = if ($cfg.send_delay_seconds) { [double]$cfg.send_delay_seconds } else { 3.5 }

# --- период: прошлая неделя (Пн..Вс) ---
$today      = (Get-Date).Date
$deltaToMon = (([int]$today.DayOfWeek + 6) % 7)      # дней с понедельника
$lastMonday = $today.AddDays(-$deltaToMon - 7)
$lastSunday = $lastMonday.AddDays(6)
$dateFrom   = $lastMonday.ToString("yyyy-MM-dd")
$dateTo     = $lastSunday.ToString("yyyy-MM-dd")
Log "Период отчёта: $dateFrom .. $dateTo"

function Parse-Num($s) {
    if ($null -eq $s) { return 0.0 }
    $t = ($s -replace ',', '.').Trim()
    if ($t -eq "" -or $t -eq "--") { return 0.0 }
    $out = 0.0
    if ([double]::TryParse($t, [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$out)) { return $out }
    return 0.0
}

function Get-Report($login) {
    $headers = @{
        "Authorization"        = "Bearer $token"
        "Client-Login"         = $login
        "Accept-Language"      = "ru"
        "processingMode"       = "auto"
        "returnMoneyInMicros"  = "false"
        "skipReportHeader"     = "true"
        "skipReportSummary"    = "true"
    }
    $body = @{
        params = @{
            SelectionCriteria = @{ DateFrom = $dateFrom; DateTo = $dateTo }
            FieldNames        = @("Impressions","Clicks","Cost","Conversions")
            ReportName        = "weekly_{0}_{1}" -f $login, ([DateTimeOffset]::Now.ToUnixTimeSeconds())
            ReportType        = "ACCOUNT_PERFORMANCE_REPORT"
            DateRangeType     = "CUSTOM_DATE"
            Format            = "TSV"
            IncludeVAT        = "YES"
            IncludeDiscount   = "NO"
        }
    } | ConvertTo-Json -Depth 6

    for ($i = 0; $i -lt 12; $i++) {
        $resp = Invoke-WebRequest -Uri "https://api.direct.yandex.com/json/v5/reports" `
                    -Method Post -Headers $headers -Body $body `
                    -ContentType "application/json; charset=utf-8" -UseBasicParsing
        if ($resp.StatusCode -eq 200) {
            $lines = ($resp.Content -split "`n") | Where-Object { $_.Trim() -ne "" }
            if ($lines.Count -lt 2) { return @{ impressions=0; clicks=0; cost=0.0; conversions=0 } }
            $h = $lines[0] -split "`t"
            $v = $lines[1] -split "`t"
            $row = @{}
            for ($k = 0; $k -lt $h.Count; $k++) { $row[$h[$k].Trim()] = $v[$k] }
            return @{
                impressions = [int](Parse-Num $row["Impressions"])
                clicks      = [int](Parse-Num $row["Clicks"])
                cost        = [double](Parse-Num $row["Cost"])
                conversions = [int](Parse-Num $row["Conversions"])
            }
        }
        elseif ($resp.StatusCode -eq 201 -or $resp.StatusCode -eq 202) {
            $wait = 5; if ($resp.Headers["retryIn"]) { $wait = [int]$resp.Headers["retryIn"] }
            Start-Sleep -Seconds $wait
        }
        else { throw "Direct API статус $($resp.StatusCode): $($resp.Content.Substring(0,[Math]::Min(300,$resp.Content.Length)))" }
    }
    throw "Отчёт не готов за отведённое время"
}

function Format-Message($name, $m) {
    $imp = $m.impressions; $clicks = $m.clicks; $cost = $m.cost; $conv = $m.conversions
    $ctr = if ($imp)    { $clicks / $imp * 100 } else { 0 }
    $cpc = if ($clicks) { $cost / $clicks }      else { 0 }
    $cr  = if ($clicks) { $conv / $clicks * 100 }else { 0 }
    $cpa = if ($conv)   { $cost / $conv }        else { 0 }
    $ci = [System.Globalization.CultureInfo]::GetCultureInfo("ru-RU")
@"
$intro

Клиент: $name
Период: $($lastMonday.ToString('dd.MM.yyyy')) — $($lastSunday.ToString('dd.MM.yyyy'))
Итого:
— Расход: $($cost.ToString('N2',$ci)) ₽
— Показы: $($imp.ToString('N0',$ci))
— Клики: $($clicks.ToString('N0',$ci))
— CTR: $($ctr.ToString('N2',$ci))%
— CPC: $($cpc.ToString('N2',$ci)) ₽
— Конверсии: $($conv.ToString('N0',$ci))
— CR: $($cr.ToString('N2',$ci))%
— CPA: $($cpa.ToString('N2',$ci)) ₽
"@
}

function Send-Telegram($chatId, $text) {
    $payload = @{ chat_id = $chatId; text = $text } | ConvertTo-Json -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $uri   = "https://api.telegram.org/bot$botToken/sendMessage"

    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try {
            Invoke-RestMethod -Uri $uri -Method Post `
                -ContentType "application/json; charset=utf-8" -Body $bytes | Out-Null
            return
        }
        catch {
            $http = $null; try { $http = [int]$_.Exception.Response.StatusCode } catch {}
            if ($http -ne 429) { throw }   # не лимит — пробрасываем дальше

            # 429 Too Many Requests: достаём retry_after из тела ответа и ждём
            $retry = 5
            $raw = $_.ErrorDetails.Message
            if (-not $raw -and $_.Exception.Response) {
                try {
                    $sr  = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                    $raw = $sr.ReadToEnd(); $sr.Close()
                } catch {}
            }
            try {
                $j = $raw | ConvertFrom-Json
                if ($j.parameters.retry_after) { $retry = [int]$j.parameters.retry_after }
            } catch {}
            Log "  429 от Telegram (попытка $attempt/6): жду $retry сек и повторяю"
            Start-Sleep -Seconds ($retry + 1)
        }
    }
    throw "Telegram: не отправлено после 6 попыток (429 Too Many Requests)"
}

$errors = @()
$ok = 0
foreach ($c in $cfg.clients) {
    try {
        $m   = Get-Report $c.login
        $msg = Format-Message $c.name $m
        Send-Telegram $c.chat_id $msg
        $ok++
        Log "OK: $($c.name) ($($c.login)) -> чат $($c.chat_id)"
        Start-Sleep -Seconds $sendDelay   # троттлинг: не упираемся в лимит Telegram
    }
    catch {
        # Тело ответа API (JSON c error_code/error_string) лежит в ErrorDetails.Message
        # — это работает и в Windows PowerShell 5.1, и в PowerShell 7.
        $detail = $_.ErrorDetails.Message
        if (-not $detail) {
            try {
                $r = $_.Exception.Response
                if ($r) {
                    $sr = New-Object System.IO.StreamReader($r.GetResponseStream())
                    $detail = $sr.ReadToEnd(); $sr.Close()
                }
            } catch {}
        }
        if (-not $detail) { $detail = $_.Exception.Message }
        $detail = ($detail -replace '\s+', ' ').Trim()
        $errors += "$($c.name): $detail"
        Log "ОШИБКА $($c.name) ($($c.login)): $detail"
    }
}

Log "Готово. Успешно: $ok из $($cfg.clients.Count). Ошибок: $($errors.Count)."
if ($errors.Count -gt 0 -and $ownerChat) {
    Send-Telegram $ownerChat ("⚠️ Еженедельный отчёт: ошибки`n" + ($errors -join "`n"))
}
