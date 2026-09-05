<#
.SYNOPSIS
    Control the local development PostgreSQL cluster.

.DESCRIPTION
    The cluster is a portable install of the official PostgreSQL binaries under
    %LOCALAPPDATA%\aquaria-pg. Nothing is registered as a Windows service, nothing
    is written to Program Files, and no administrator rights are needed — so it can
    be removed by deleting that one folder.

    It listens on 127.0.0.1:5433 only. The non-default port avoids clashing with any
    system PostgreSQL, and binding to loopback keeps a development database with a
    known password off the network.

.EXAMPLE
    .\scripts\pg.ps1 setup     # download, initdb, create role + database (first run)
    .\scripts\pg.ps1 start
    .\scripts\pg.ps1 status
    .\scripts\pg.ps1 psql
    .\scripts\pg.ps1 stop
    .\scripts\pg.ps1 reset     # drop and recreate the database (destroys local data)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'start', 'stop', 'restart', 'status', 'psql', 'reset', 'logs')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$PgRoot   = Join-Path $env:LOCALAPPDATA 'aquaria-pg'
$PgBin    = Join-Path $PgRoot 'pgsql\bin'
$PgData   = Join-Path $PgRoot 'data'
$PgLog    = Join-Path $PgRoot 'server.log'
$PgPort   = 5433
$SuperPw  = 'aquaria_dev_pw'
$AppUser  = 'aquaria'
$AppPw    = 'aquaria_dev_pw'
$AppDb    = 'aquaria'
$Version  = '17.7-1'

function Assert-Installed {
    if (-not (Test-Path (Join-Path $PgBin 'pg_ctl.exe'))) {
        throw "PostgreSQL is not installed at $PgRoot. Run: .\scripts\pg.ps1 setup"
    }
}

function Invoke-Psql {
    param([string]$Database = 'postgres', [string[]]$Commands)
    $env:PGPASSWORD = $SuperPw
    try {
        $args = @('-h', '127.0.0.1', '-p', $PgPort, '-U', 'postgres', '-d', $Database, '-v', 'ON_ERROR_STOP=1')
        foreach ($c in $Commands) { $args += @('-c', $c) }
        & (Join-Path $PgBin 'psql.exe') @args
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
}

switch ($Action) {

    'setup' {
        $ProgressPreference = 'SilentlyContinue'
        New-Item -ItemType Directory -Path $PgRoot -Force | Out-Null
        $zip = Join-Path $PgRoot 'pg-binaries.zip'

        if (-not (Test-Path (Join-Path $PgBin 'pg_ctl.exe'))) {
            if (-not (Test-Path $zip)) {
                $url = "https://get.enterprisedb.com/postgresql/postgresql-$Version-windows-x64-binaries.zip"
                Write-Host "Downloading PostgreSQL $Version binaries (~316 MB)..."
                Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 1800 -UseBasicParsing
            }
            Write-Host 'Extracting server files (bin, lib, share)...'
            # Selective extraction: the archive also carries pgAdmin and StackBuilder,
            # which are large and unnecessary for running a server.
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
            try {
                foreach ($entry in $archive.Entries) {
                    if ($entry.FullName -notmatch '^pgsql/(bin|lib|share)/') { continue }
                    if ($entry.FullName.EndsWith('/')) { continue }
                    $target = Join-Path $PgRoot ($entry.FullName -replace '/', '\')
                    New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
                }
            } finally {
                $archive.Dispose()
            }
        }

        if (-not (Test-Path (Join-Path $PgData 'PG_VERSION'))) {
            Write-Host 'Initialising the cluster...'
            $pwfile = Join-Path $PgRoot 'superpass.txt'
            Set-Content -Path $pwfile -Value $SuperPw -NoNewline -Encoding ascii
            try {
                & (Join-Path $PgBin 'initdb.exe') --pgdata=$PgData --username=postgres `
                    --pwfile=$pwfile --encoding=UTF8 --auth-local=trust --auth-host=scram-sha-256 | Out-Null
            } finally {
                Remove-Item $pwfile -Force -ErrorAction SilentlyContinue
            }
        }

        & $PSCommandPath start
        Start-Sleep -Seconds 3

        Write-Host 'Creating role and database...'
        # The application role is not a superuser, so the suite exercises the same
        # permission surface as production. CREATEDB is needed for the test database.
        Invoke-Psql -Commands @(
            "DO `$`$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$AppUser') THEN CREATE ROLE $AppUser WITH LOGIN PASSWORD '$AppPw' CREATEDB; END IF; END `$`$;"
        )
        $exists = Invoke-Psql -Commands @("SELECT 1 FROM pg_database WHERE datname='$AppDb'")
        if ($exists -notmatch '1') {
            Invoke-Psql -Commands @("CREATE DATABASE $AppDb OWNER $AppUser ENCODING 'UTF8'")
        }
        Write-Host ''
        Write-Host "Ready. DATABASE_URL=postgres://$AppUser`:$AppPw@127.0.0.1:$PgPort/$AppDb"
    }

    'start' {
        Assert-Installed
        & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData -l $PgLog -o "-p $PgPort -h 127.0.0.1" start
    }

    'stop' {
        Assert-Installed
        & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData -m fast stop
    }

    'restart' {
        Assert-Installed
        & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData -m fast -l $PgLog -o "-p $PgPort -h 127.0.0.1" restart
    }

    'status' {
        Assert-Installed
        & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData status
    }

    'psql' {
        Assert-Installed
        $env:PGPASSWORD = $AppPw
        try {
            & (Join-Path $PgBin 'psql.exe') -h 127.0.0.1 -p $PgPort -U $AppUser -d $AppDb
        } finally {
            Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        }
    }

    'reset' {
        Assert-Installed
        Write-Warning "This drops and recreates the '$AppDb' database. All local data is lost."
        $answer = Read-Host "Type the database name to confirm"
        if ($answer -ne $AppDb) { Write-Host 'Aborted.'; break }
        Invoke-Psql -Commands @(
            "DROP DATABASE IF EXISTS $AppDb",
            "CREATE DATABASE $AppDb OWNER $AppUser ENCODING 'UTF8'"
        )
        Write-Host 'Recreated. Run: python manage.py migrate'
    }

    'logs' {
        if (Test-Path $PgLog) { Get-Content $PgLog -Tail 60 } else { Write-Host 'No log yet.' }
    }
}
