<#
.SYNOPSIS
    Projects battle.json files from .legion folders into fleet.d/ shards, and/or
    generates synthetic test battles, for testing the Legatus UI.

.DESCRIPTION
    A shard (fleet.d/<sha1>.json) is a projection of a battle's battle.json (+ usage.jsonl
    for tokens/skills). The legion orchestrator is the only writer of the REAL index, so
    this script writes into an isolated test directory by default; point the app at it with
    LEGION_FLEET=<OutputBase>.

.PARAMETER BattlePath
    Zero or more paths, each a repo root (containing .legion) or a .legion folder.

.PARAMETER Synthetic
    Also generate a set of varied fake battles (active/blocked/closed at different phases,
    with tokens, skills and usage.jsonl) under <OutputBase>/synthetic-repos.

.PARAMETER Count
    Number of synthetic battles to generate (default 10). Used only with -Synthetic.

.PARAMETER OutputBase
    Base directory; shards are written under <OutputBase>/fleet.d/.
    Default: <repo>/testdata/fleet-base.

.PARAMETER Clean
    Remove existing shards in the target fleet.d/ before writing.

.EXAMPLE
    ./tools/Seed-Fleet.ps1 -Synthetic -Count 10 -Clean
    $env:LEGION_FLEET = (Resolve-Path ./testdata/fleet-base).Path
    dotnet run --project src/presentation
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string[]] $BattlePath = @(),

    [switch] $Synthetic,

    [int] $Count = 10,

    [string] $OutputBase = (Join-Path $PSScriptRoot "..\testdata\fleet-base"),

    [switch] $Clean
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$PhaseOrder = @('think', 'plan', 'build', 'review', 'test', 'deliver', 'reflect')

function Write-Utf8([string] $path, [string] $content) {
    [System.IO.File]::WriteAllText($path, $content, $Utf8NoBom)
}

function Get-ShardName([string] $repoPath, [string] $id) {
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $hash = $sha1.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$repoPath::$id"))
    }
    finally { $sha1.Dispose() }
    return (($hash[0..7] | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-PhaseStatus($phases, [string] $name) {
    $p = $phases.PSObject.Properties[$name]
    if ($null -eq $p -or $null -eq $p.Value.status) { return $null }
    return [string] $p.Value.status
}

function Get-Verdict($phases, [string] $name) {
    $p = $phases.PSObject.Properties[$name]
    if ($null -eq $p) { return $null }
    $verdict = $p.Value.PSObject.Properties['verdict']
    if ($null -eq $verdict) { return $null }
    return $verdict.Value
}

# Aggregate tokens + skills from a battle's usage.jsonl (sibling of battle.json), if present.
function Get-UsageAggregate([string] $battleDir) {
    $usagePath = Join-Path $battleDir 'usage.jsonl'
    if (-not (Test-Path $usagePath)) { return $null }

    $input = 0L; $output = 0L; $cacheRead = 0L; $cacheCreation = 0L
    $skills = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content -Path $usagePath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try { $rec = $line | ConvertFrom-Json } catch { continue }
        if ($rec.tokens) {
            $input += [int64]($rec.tokens.input | ForEach-Object { $_ })
            $output += [int64]($rec.tokens.output | ForEach-Object { $_ })
            if ($rec.tokens.cache_read) { $cacheRead += [int64] $rec.tokens.cache_read }
            if ($rec.tokens.cache_creation) { $cacheCreation += [int64] $rec.tokens.cache_creation }
        }
        if ($rec.skills) { foreach ($s in $rec.skills) { if ($skills -notcontains $s) { $skills.Add($s) } } }
    }
    return [ordered]@{
        tokens_total = $input + $output
        tokens       = [ordered]@{ input = $input; output = $output; cache_read = $cacheRead; cache_creation = $cacheCreation }
        skills       = @($skills)
    }
}

function ConvertTo-Shard([string] $battleJsonPath) {
    $battleDir = Split-Path -Parent $battleJsonPath
    $battle = Get-Content -Raw -Encoding UTF8 -Path $battleJsonPath | ConvertFrom-Json
    $phases = $battle.phases

    # repo_path is the directory three levels above battle.json: <repo>/.legion/battles/<id>/battle.json
    $repoPath = (Get-Item $battleJsonPath).Directory.Parent.Parent.Parent.FullName
    $repo = Split-Path -Leaf $repoPath

    $blocked = $null; $inProgress = $null; $lastDone = $null
    foreach ($name in $PhaseOrder) {
        switch (Get-PhaseStatus $phases $name) {
            'blocked' { $blocked = $name }
            'in_progress' { $inProgress = $name }
            'done' { $lastDone = $name }
        }
    }
    $current = if ($blocked) { $blocked } elseif ($inProgress) { $inProgress } elseif ($lastDone) { $lastDone } else { 'think' }
    $status = Get-PhaseStatus $phases $current
    if (-not $status) { $status = 'pending' }

    $hasBadVerdict = $false
    foreach ($name in $PhaseOrder) {
        if ((Get-Verdict $phases $name) -in @('revise', 'reject')) { $hasBadVerdict = $true }
    }
    $battleStatus =
    if ($blocked -or $hasBadVerdict) { 'blocked' }
    elseif ((Get-PhaseStatus $phases 'reflect') -eq 'done') { 'closed' }
    else { 'active' }

    $updated = (Get-Item $battleJsonPath).LastWriteTimeUtc.ToString("yyyy-MM-ddTHH:mm:ss+00:00")

    $shard = [ordered]@{
        id            = [string] $battle.id
        repo          = $repo
        repo_path     = $repoPath
        ticket        = $battle.ticket
        title         = $battle.title
        profile       = $battle.profile
        phase         = $current
        status        = $status
        battle_status = $battleStatus
        pr_url        = $battle.delivery.pr_url
    }

    # Project cost & skills from usage.jsonl when available.
    $usage = Get-UsageAggregate $battleDir
    if ($null -ne $usage) {
        $shard.tokens_total = $usage.tokens_total
        $shard.tokens = $usage.tokens
        $shard.skills = $usage.skills
    }

    $shard.updated = $updated
    return $shard
}

# --- Synthetic test data ---------------------------------------------------------------

function New-Md($h, $b) { "# $h`n`n$b`n" }

function New-Battle($root, $repo, $id, $ticket, $title, $profile, $phases, $prUrl, $contribs, $artifacts) {
    $battleDir = Join-Path $root "$repo\.legion\battles\$id"
    New-Item -ItemType Directory -Force -Path $battleDir | Out-Null

    $battleJson = [ordered]@{
        id             = $id
        ticket         = $ticket
        title          = $title
        profile        = $profile
        required_gates = @('architect', 'reviewer', 'test-engineer')
        phases         = $phases
        guard          = $null
        delivery       = [ordered]@{ pr_url = $prUrl }
    }
    Write-Utf8 (Join-Path $battleDir 'battle.json') ($battleJson | ConvertTo-Json -Depth 6)

    foreach ($name in $artifacts.Keys) {
        Write-Utf8 (Join-Path $battleDir $name) $artifacts[$name]
    }

    $lines = foreach ($c in $contribs) { ($c | ConvertTo-Json -Compress -Depth 5) }
    Write-Utf8 (Join-Path $battleDir 'usage.jsonl') (($lines -join "`n") + "`n")

    return (Join-Path $root $repo)
}

# Pools and scenarios driving generated variety.
$RepoPool = @('billing-api', 'auth-service', 'notify-worker', 'catalog-api', 'search-index',
    'payments-gw', 'report-engine', 'user-profile', 'api-gateway', 'scheduler', 'audit-log', 'file-store')
$TitlePool = @('Endpoint de facturation récurrente', 'Rotation des clés de signature JWT',
    "File d'attente de notifications e-mail", 'Recherche full-text du catalogue',
    'Index de recherche incrémental', 'Passerelle de paiement Stripe', 'Moteur de rapports PDF',
    'Profil utilisateur enrichi', 'Routage API versionné', 'Planificateur de tâches CRON',
    "Journal d'audit immuable", 'Stockage de fichiers par chunks')
$ProfilePool = @('feature', 'security', 'feature', 'feature', 'spike', 'feature',
    'feature', 'feature', 'feature', 'hotfix', 'security', 'feature')
$SkillByPhase = @{ think = @('plan'); plan = @('clean-architecture'); build = @('scaffold', 'ef-core', 'minimal-api');
    review = @('code-review'); test = @('testing'); deliver = @('verify'); reflect = @('de-sloppify') }
$ArtifactByPhase = @{ think = 'spec.md'; plan = 'plan.md'; build = 'build-report.md'; review = 'gate-review.md';
    test = 'gate-test.md'; deliver = 'pr-body.md'; reflect = 'retro.md' }
$GatePhases = @('plan', 'review', 'test')
# Each scenario = how far the pipeline got + the current phase's status.
$Scenarios = @(
    @{ phase = 'build'; status = 'in_progress' },
    @{ phase = 'review'; status = 'blocked' },
    @{ phase = 'reflect'; status = 'done' },
    @{ phase = 'test'; status = 'in_progress' },
    @{ phase = 'plan'; status = 'in_progress' },
    @{ phase = 'deliver'; status = 'in_progress' },
    @{ phase = 'reflect'; status = 'done' },
    @{ phase = 'build'; status = 'blocked' },
    @{ phase = 'think'; status = 'in_progress' },
    @{ phase = 'deliver'; status = 'done' },
    @{ phase = 'test'; status = 'done' }
)

function New-ScenarioBattle($root, $index) {
    $repo = $RepoPool[$index % $RepoPool.Count]
    $title = $TitlePool[$index % $TitlePool.Count]
    $profile = $ProfilePool[$index % $ProfilePool.Count]
    $sc = $Scenarios[$index % $Scenarios.Count]
    $curIdx = $PhaseOrder.IndexOf($sc.phase)
    $curStatus = $sc.status

    $day = 20 - $index
    if ($day -lt 1) { $day = 1 }
    $issueNum = 410 - $index * 7
    $id = "2026-06-{0:d2}-GH-{1}" -f $day, $issueNum
    $ticket = "GH#$issueNum"

    $phases = [ordered]@{}
    $artifacts = @{}
    $skills = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $PhaseOrder.Count; $i++) {
        $key = $PhaseOrder[$i]
        if ($i -lt $curIdx) { $st = 'done' } elseif ($i -eq $curIdx) { $st = $curStatus } else { $st = 'pending' }
        $entry = [ordered]@{ status = $st }
        if ($st -eq 'done' -or $st -eq 'blocked') {
            $entry.artifact = $ArtifactByPhase[$key]
            $artifacts[$ArtifactByPhase[$key]] = (New-Md $key "Artefact de la phase **$key** de la battle $repo / $ticket.")
            foreach ($s in $SkillByPhase[$key]) { if ($skills -notcontains $s) { $skills.Add($s) } }
        }
        if ($GatePhases -contains $key) {
            if ($st -eq 'done') {
                $entry.verdict = if ($key -eq 'plan' -and ($index % 3) -eq 0) { 'accept_with_opportunity' } else { 'accept' }
            }
            elseif ($st -eq 'blocked') { $entry.verdict = 'revise' }
        }
        $phases[$key] = $entry
    }

    $delivered = ($phases['deliver'].status -eq 'done')
    $prUrl = if ($delivered) { "https://github.com/mickaelfrancois/$repo/pull/$issueNum" } else { $null }

    $scale = $curIdx + 1
    $contribs = @(
        [ordered]@{ scope = 'main'; skills = @($skills); tokens = [ordered]@{ input = 35000 + $scale * 21000; output = 7000 + $scale * 3500; cache_read = $scale * 6000; cache_creation = 1500 } }
    )
    if ($phases['plan'].status -in @('done', 'blocked')) { $contribs += [ordered]@{ scope = 'subagent'; agent_type = 'architect'; skills = @('clean-architecture'); tokens = [ordered]@{ input = 28000; output = 5200; cache_read = 0; cache_creation = 0 } } }
    if ($phases['review'].status -in @('done', 'blocked')) { $contribs += [ordered]@{ scope = 'subagent'; agent_type = 'reviewer'; skills = @('code-review'); tokens = [ordered]@{ input = 41000; output = 8000; cache_read = 0; cache_creation = 0 } } }
    if ($phases['test'].status -in @('done', 'blocked')) { $contribs += [ordered]@{ scope = 'subagent'; agent_type = 'test-engineer'; skills = @('testing'); tokens = [ordered]@{ input = 33000; output = 6400; cache_read = 0; cache_creation = 0 } } }

    return (New-Battle $root $repo $id $ticket $title $profile $phases $prUrl $contribs $artifacts)
}

function New-SyntheticData([string] $baseDir, [int] $count) {
    $root = Join-Path $baseDir 'synthetic-repos'
    if (Test-Path $root) { Remove-Item -Recurse -Force $root }
    New-Item -ItemType Directory -Force -Path $root | Out-Null

    $repos = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $count; $i++) {
        $r = New-ScenarioBattle $root $i
        if (-not $repos.Contains($r)) { $repos.Add($r) }
    }
    return $repos
}

# --- Main ------------------------------------------------------------------------------

$fleetDir = Join-Path $OutputBase "fleet.d"
New-Item -ItemType Directory -Force -Path $fleetDir | Out-Null
if ($Clean) {
    Get-ChildItem -Path $fleetDir -Filter *.json -ErrorAction SilentlyContinue | Remove-Item -Force
}

$paths = @() + $BattlePath
if ($Synthetic) {
    $paths += New-SyntheticData (Resolve-Path $OutputBase).Path $Count
}
if ($paths.Count -eq 0) {
    throw "Provide -BattlePath and/or -Synthetic."
}

$battleFiles = @()
foreach ($path in $paths) {
    $legion = if ((Split-Path -Leaf $path) -eq '.legion') { $path } else { Join-Path $path '.legion' }
    $battlesDir = Join-Path $legion 'battles'
    if (-not (Test-Path $battlesDir)) {
        Write-Warning "No battles directory under $path (looked at $battlesDir)"
        continue
    }
    $battleFiles += Get-ChildItem -Path $battlesDir -Recurse -Filter 'battle.json' -File
}

$count = 0
foreach ($file in $battleFiles) {
    $shard = ConvertTo-Shard $file.FullName
    $name = Get-ShardName $shard.repo_path $shard.id
    Write-Utf8 (Join-Path $fleetDir "$name.json") ($shard | ConvertTo-Json -Depth 6)
    $cost = if ($shard.tokens_total) { ", $($shard.tokens_total) tok" } else { "" }
    Write-Host "  $($shard.repo)  $($shard.id)  [$($shard.phase)/$($shard.status), $($shard.battle_status)$cost] -> $name.json"
    $count++
}

Write-Host ""
Write-Host "Wrote $count shard(s) to $fleetDir"
Write-Host "Run the app against this index with:"
Write-Host "  `$env:LEGION_FLEET = '$((Resolve-Path $OutputBase).Path)'"
Write-Host "  dotnet run --project src/presentation"
