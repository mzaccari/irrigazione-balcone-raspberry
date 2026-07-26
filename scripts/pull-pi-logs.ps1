<#
.SYNOPSIS
    Archivia log, stato runtime e config dal Raspberry Pi dell'irrigatore.

.DESCRIPTION
    Scarica in una cartella datata:
      - software/runtime/  (state.json, events.jsonl, sensors.jsonl, weather.json)
      - software/programs.json  (config live, spesso divergente dal repo)
      - ~/pi-temp.csv           (temperatura/throttling del Pi)
      - journalctl dei servizi irrigatore-daemon / irrigatore-web / net-watchdog
      - info.txt                (uptime, stato servizi, spazio disco, temperatura)

    La destinazione e' FUORI dal repo: sono dati runtime che crescono senza limite,
    e dentro il repo verrebbero cancellati dal primo "git clean -xdf".

    NOTA PowerShell 5.1: gli stream binari nelle pipe vengono corrotti, quindi il
    tar viene creato su file remoto e trasferito con scp invece di "ssh ... | tar".

.EXAMPLE
    .\scripts\pull-pi-logs.ps1
    .\scripts\pull-pi-logs.ps1 -JournalDays 60
#>
[CmdletBinding()]
param(
    [string]$PiHost      = "mzaccari@192.168.178.39",
    [string]$Dest        = "$HOME\Documents\irrigatore-logs",
    [int]   $JournalDays = 30
)

$ErrorActionPreference = "Stop"

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dir   = Join-Path $Dest $stamp
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Write-Host "Destinazione: $dir"

$remoteTar = "/tmp/irrigatore-pull-$stamp.tar.gz"
$localTar  = Join-Path $dir "pi-files.tar.gz"

# --- 1. File: runtime/ + programs.json + pi-temp.csv -------------------------
# --exclude=commands: coda di comandi effimera, senza valore diagnostico.
# --ignore-failed-read: sensors.jsonl e weather.json non esistono prima della Fase 5.
Write-Host "[1/4] Creazione archivio sul Pi..."
$tarCmd = "tar -czf $remoteTar --ignore-failed-read --exclude=commands " +
          "-C ~/irrigatore/software runtime programs.json -C ~ pi-temp.csv"
ssh $PiHost $tarCmd
if ($LASTEXITCODE -ne 0) { Write-Warning "tar ha segnalato file mancanti (exit $LASTEXITCODE) - continuo" }

Write-Host "[2/4] Trasferimento..."
scp -q "${PiHost}:$remoteTar" $localTar
ssh $PiHost "rm -f $remoteTar"

tar -xzf $localTar -C $dir
Remove-Item $localTar

# --- 2. Journal dei servizi --------------------------------------------------
Write-Host "[3/4] Journal (ultimi $JournalDays giorni)..."
$units = @(
    @{ Arg = "-u irrigatore-daemon"; File = "journal-daemon.txt"      },
    @{ Arg = "-u irrigatore-web";    File = "journal-web.txt"         },
    @{ Arg = "-t net-watchdog";      File = "journal-netwatchdog.txt" }
)
foreach ($u in $units) {
    $out = Join-Path $dir $u.File
    ssh $PiHost "sudo journalctl $($u.Arg) --since '-$JournalDays days' --no-pager" |
        Out-File -FilePath $out -Encoding utf8
}

# --- 3. Contesto di sistema --------------------------------------------------
Write-Host "[4/4] Info di sistema..."
$infoCmd = "date -Iseconds; echo; uptime; echo; " +
           "systemctl is-active irrigatore-daemon irrigatore-web; echo; " +
           "df -h /; echo; journalctl --disk-usage; echo; vcgencmd measure_temp"
ssh $PiHost $infoCmd | Out-File -FilePath (Join-Path $dir "info.txt") -Encoding utf8

Write-Host ""
Write-Host "Fatto. Contenuto:"
Get-ChildItem -Recurse -File $dir |
    Select-Object @{N = "File"; E = { $_.FullName.Substring($dir.Length + 1) }},
                  @{N = "KB";   E = { [math]::Round($_.Length / 1KB, 1) }} |
    Format-Table -AutoSize
