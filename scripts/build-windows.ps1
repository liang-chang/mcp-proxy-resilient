param()
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location $repo
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $repo 'build/portable-venv'
    # bugfix: an editable install can retain old dist-info after a dynamic version bump.
    uv sync --locked --no-dev --group build --python 3.13 --reinstall-package mcp-proxy-resilient
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    $python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe'
    & $python -c "import platform; assert platform.system() == 'Windows' and platform.machine() == 'AMD64', 'Windows x64 build required'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported build platform' }
    $version = & $python -c 'from mcp_proxy import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve version' }
    & $python -m PyInstaller --noconfirm --clean --onedir --console --noupx --name mcp-proxy-resilient --paths src --copy-metadata mcp --copy-metadata mcp-proxy-resilient --collect-submodules uvicorn --hidden-import anyio._backends._asyncio --distpath dist/portable --workpath build/pyinstaller --specpath build scripts/portable_entry.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed' }
    $bundle = Join-Path $repo 'dist/portable/mcp-proxy-resilient'
    Copy-Item -LiteralPath LICENSE, uv.lock, README.md, README.zh-CN.md, CHANGELOG.md, servers.example.json, examples/start-proxy.cmd -Destination $bundle
    $bundleDocs = New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'docs')
    Copy-Item -LiteralPath docs/DEPLOYMENT.zh-CN.md, docs/RELEASING.md -Destination $bundleDocs
    # Include dependency license files and metadata without copying build-time executables.
    $notices = New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'dependency-notices')
    Get-ChildItem (Join-Path $env:UV_PROJECT_ENVIRONMENT 'Lib/site-packages') -Directory -Filter '*.dist-info' | ForEach-Object {
        $packageNotices = New-Item -ItemType Directory -Force -Path (Join-Path $notices.FullName $_.Name)
        Get-ChildItem -LiteralPath $_.FullName | Where-Object {
            $_.Name -eq 'METADATA' -or $_.Name -match '^(licenses?|copying|notice)'
        } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $packageNotices.FullName -Recurse -Force
        }
    }
    $releaseVersion = $version.Replace('+resilient.', '-r')
    $archive = Join-Path $repo "dist/mcp-proxy-resilient-$releaseVersion-windows-x64.zip"
    Compress-Archive -LiteralPath $bundle -DestinationPath $archive -Force
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $(Split-Path $archive -Leaf)" | Set-Content -LiteralPath "$archive.sha256" -Encoding ascii
    Write-Output $archive
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    Pop-Location
}
