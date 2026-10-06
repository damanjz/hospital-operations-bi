# Case-study images: build screenshot copies of the report under .captures\shots (never the main project,
# which may be open), open each page in its own Power BI window, crop to the canvas, save to docs\img.
$root = Split-Path $PSScriptRoot -Parent
$py = "$root\.venv\Scripts\python.exe"
$shots = "$root\.captures\shots"
$img = "$root\docs\img"
New-Item -ItemType Directory -Force $img | Out-Null
$variants = [ordered]@{
    default  = @()
    boarding = @("--asof", "2025-09-18 12")
    fy2526   = @("--period", "FY 2025-26")
}
$pages = @(
    @("default", "now", "01-now"), @("default", "flow", "02-flow"), @("default", "readmissions", "03-readmissions"),
    @("boarding", "now", "04-now-full"), @("fy2526", "flow", "05-flow-fy2526")
)
foreach ($v in $variants.Keys) {
    & $py "$root\powerbi\build_pbip.py" --out "$shots\$v" @($variants[$v]) | Out-Null
    & "$root\powerbi\validate_tmdl.ps1" -Folder "$shots\$v\PatientFlow.SemanticModel\definition"
    if ($LASTEXITCODE) { exit 1 }
}
foreach ($p in $pages) {
    $raw = "$root\.captures\doc-$($p[2]).png"
    # Power BI sometimes fails to open a project on the first try; retry, each attempt cleans up after itself
    for ($try = 1; $try -le 3; $try++) {
        & "$root\powerbi\open_pbip.ps1" -Page $p[1] -NoBuild -Project "$shots\$($p[0])\PatientFlow.pbip" -Out $raw | Out-Null
        if ($LASTEXITCODE -eq 0) { & "$root\powerbi\crop_canvas.ps1" -In $raw -Out "$img\$($p[2]).png" }
        if ($LASTEXITCODE -eq 0) { break }
        "$($p[2]): attempt $try failed"
    }
}
# keep only the final images: drop the screenshot copies of the report and the raw window captures
Remove-Item -Recurse -Force $shots
Get-ChildItem "$root\.captures" -Filter "doc-*.png" | Remove-Item
