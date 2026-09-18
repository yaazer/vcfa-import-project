<#
.SYNOPSIS
    Export vCenter VMs into a vcfa-import inventory CSV.

.DESCRIPTION
    Produces one row per VM with its managed object reference (moref), which is
    what ImportOperationBatch uses to identify the source VM, plus one nicN_*
    column pair per network adapter.

    Supply -MappingCsv to translate each VM's portgroup into the target
    namespace and subnet. The mapping file needs these columns:

        portgroup,namespace,subnet[,wave][,device_key]

    Rows whose portgroup is not in the mapping are still exported, with the
    namespace and subnet left blank and a note in the 'notes' column, so you
    can see what is unmapped rather than silently dropping VMs.

.EXAMPLE
    Connect-VIServer vcenter.example.local
    .\Export-VcenterInventory.ps1 -MappingCsv .\portgroup-map.csv -OutFile .\inventory.csv

.EXAMPLE
    # Only VMs under a folder path (subfolders included), powered on, excluding templates
    .\Export-VcenterInventory.ps1 -FolderPath 'Production/Web' -PoweredOnOnly `
        -MappingCsv .\portgroup-map.csv -OutFile .\inventory.csv

.NOTES
    Requires VMware PowerCLI and an existing Connect-VIServer session.
    Read-only: this script never modifies vCenter.
#>

[CmdletBinding()]
param(
    [string]   $OutFile = ".\inventory.csv",
    [string]   $MappingCsv,
    [string[]] $Location,
    [string[]] $FolderPath,          # e.g. 'Production/Web'; includes subfolders
    [string[]] $Cluster,
    [string]   $NameFilter = "*",
    [switch]   $PoweredOnOnly,
    [switch]   $IncludeTemplates,
    [int]      $DefaultWave = 1,
    [int]      $BaseDeviceKey = 4000
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    throw "PowerCLI not found. Install-Module VMware.PowerCLI, then Connect-VIServer first."
}
if (-not $global:DefaultVIServers -or $global:DefaultVIServers.Count -eq 0) {
    throw "No vCenter connection. Run Connect-VIServer <vcenter> first."
}

# ----------------------------------------------------------------- folders
function Get-FolderPath {
    # Full path below the datacenter, e.g. Production/Web/Tier1. The hidden
    # "vm" root folder and the datacenter itself are not part of it, so this
    # matches what `vcfa-import discover` produces.
    param($Folder)
    $parts = New-Object System.Collections.Generic.List[string]
    $current = $Folder
    while ($current -and $current.ExtensionData -and
           $current.ExtensionData.MoRef.Type -eq 'Folder') {
        $parentRef = $current.ExtensionData.Parent
        if (-not $parentRef -or $parentRef.Type -ne 'Folder') { break }   # child of the datacenter: the hidden root
        $parts.Insert(0, $current.Name)
        $current = Get-Folder -Id ("Folder-" + $parentRef.Value) -ErrorAction SilentlyContinue
    }
    return ($parts -join '/')
}

# ---------------------------------------------------------------- mapping
$map = @{}
if ($MappingCsv) {
    if (-not (Test-Path $MappingCsv)) { throw "Mapping file not found: $MappingCsv" }
    foreach ($row in Import-Csv $MappingCsv) {
        if (-not $row.portgroup) { continue }
        $map[$row.portgroup.Trim()] = [pscustomobject]@{
            Namespace = $row.namespace
            Subnet    = $row.subnet
            Wave      = if ($row.PSObject.Properties.Name -contains 'wave' -and $row.wave) { $row.wave } else { $DefaultWave }
            DeviceKey = if ($row.PSObject.Properties.Name -contains 'device_key' -and $row.device_key) { $row.device_key } else { $null }
        }
    }
    Write-Host "loaded $($map.Count) portgroup mapping(s) from $MappingCsv"
}

# ------------------------------------------------------------------- VMs
$getVmParams = @{ Name = $NameFilter }
if ($Location) { $getVmParams['Location'] = $Location }
Write-Host "querying vCenter..."
$vms = Get-VM @getVmParams

if ($Cluster) {
    $vms = $vms | Where-Object { $Cluster -contains $_.VMHost.Parent.Name }
}
if ($FolderPath) {
    $wanted = $FolderPath | ForEach-Object { $_.Trim('/').ToLower() }
    $vms = $vms | Where-Object {
        $p = (Get-FolderPath $_.Folder).ToLower()
        $hit = $false
        foreach ($w in $wanted) { if ($p -eq $w -or $p.StartsWith($w + '/')) { $hit = $true } }
        $hit
    }
}
if (-not $IncludeTemplates) {
    $vms = $vms | Where-Object { -not $_.ExtensionData.Config.Template }
}
if ($PoweredOnOnly) {
    $vms = $vms | Where-Object { $_.PowerState -eq 'PoweredOn' }
}

Write-Host "found $($vms.Count) VM(s); reading network adapters..."

# One bulk call is far faster than per-VM Get-NetworkAdapter on large estates.
$adaptersByVm = @{}
Get-NetworkAdapter -VM $vms | ForEach-Object {
    $vmName = $_.Parent.Name
    if (-not $adaptersByVm.ContainsKey($vmName)) { $adaptersByVm[$vmName] = @() }
    $adaptersByVm[$vmName] += $_
}

$maxNics = 1
foreach ($list in $adaptersByVm.Values) {
    if ($list.Count -gt $maxNics) { $maxNics = $list.Count }
}

$rows = New-Object System.Collections.Generic.List[object]
$unmapped = @{}

foreach ($vm in $vms) {
    $adapters = @()
    if ($adaptersByVm.ContainsKey($vm.Name)) {
        $adapters = $adaptersByVm[$vm.Name] | Sort-Object Name
    }

    $namespace = ''
    $wave      = $DefaultWave
    $notes     = @()

    $row = [ordered]@{
        vm_name   = $vm.Name
        moref     = $vm.ExtensionData.MoRef.Value      # e.g. vm-1483405
        namespace = ''
        wave      = $DefaultWave
        group     = ''
        mode      = ''
        power     = $vm.PowerState
        vcenter   = $vm.Uid.Split(':')[0].Split('@')[-1]
        folder    = Get-FolderPath $vm.Folder
        cluster   = $vm.VMHost.Parent.Name
        tools     = $vm.ExtensionData.Guest.ToolsRunningStatus
        notes     = ''
    }

    for ($i = 1; $i -le $maxNics; $i++) {
        $row["nic${i}_subnet"]     = ''
        $row["nic${i}_device_key"] = ''
        $row["nic${i}_portgroup"]  = ''
    }

    $idx = 0
    foreach ($adapter in $adapters) {
        $idx++
        $pg = $adapter.NetworkName
        $row["nic${idx}_portgroup"] = $pg
        # The device key is stable in vCenter; fall back to the 4000+n convention.
        $key = $adapter.ExtensionData.Key
        if (-not $key) { $key = $BaseDeviceKey + ($idx - 1) }
        $row["nic${idx}_device_key"] = $key

        if ($map.ContainsKey($pg)) {
            $entry = $map[$pg]
            $row["nic${idx}_subnet"] = $entry.Subnet
            if ($entry.DeviceKey) { $row["nic${idx}_device_key"] = $entry.DeviceKey }
            if (-not $namespace) {
                $namespace = $entry.Namespace
                $wave = $entry.Wave
            }
            elseif ($entry.Namespace -and $entry.Namespace -ne $namespace) {
                $notes += "nic$idx maps to namespace $($entry.Namespace), not $namespace"
            }
        }
        elseif ($MappingCsv) {
            $notes += "unmapped portgroup: $pg"
            $unmapped[$pg] = $true
        }
    }

    if ($adapters.Count -eq 0) { $notes += 'no network adapters' }
    if ($vm.ExtensionData.Guest.ToolsRunningStatus -ne 'guestToolsRunning') {
        $notes += 'VM Tools not running'
    }

    $row.namespace = $namespace
    $row.wave      = $wave
    $row.notes     = ($notes -join '; ')
    $rows.Add([pscustomobject]$row)
}

$rows | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8
Write-Host "wrote $($rows.Count) row(s) to $OutFile"

$missingNs = @($rows | Where-Object { -not $_.namespace }).Count
if ($missingNs -gt 0) {
    Write-Warning "$missingNs VM(s) have no target namespace; fill these in before loading."
}
if ($unmapped.Count -gt 0) {
    Write-Warning "unmapped portgroups: $($unmapped.Keys -join ', ')"
}
$noTools = @($rows | Where-Object { $_.tools -ne 'guestToolsRunning' }).Count
if ($noTools -gt 0) {
    Write-Warning "$noTools VM(s) are not running VM Tools; the import precheck will likely reject them."
}

Write-Host ""
Write-Host "Next: python vcfa-import.py validate -i $OutFile"
