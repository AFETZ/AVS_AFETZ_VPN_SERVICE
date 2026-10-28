#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${OPENFLUX_BUILD_DIR:-$repo_dir/.local/openflux-build}"
output_dir="${OPENFLUX_OUTPUT_DIR:-$repo_dir/app/data/openflux}"
go_bin="${OPENFLUX_GO:-go}"
commit=a8a8937c59fbb275603bd53e150ecaf46e92148e
patch_file="$repo_dir/deploy/openflux/patches/client-and-retry.patch"
mkdir -p "$build_dir" "$output_dir"
if [[ ! -d "$build_dir/source/.git" ]]; then
    git clone --no-checkout https://github.com/p1neappleXpress/OpenFlux.git "$build_dir/source"
fi
git -C "$build_dir/source" fetch --depth=1 origin "$commit"
git -C "$build_dir/source" checkout --detach "$commit"
cd "$build_dir/source"
if git apply --reverse --check "$patch_file" 2>/dev/null; then
    git apply --reverse "$patch_file"
fi
test -z "$(git status --porcelain)"
git apply --check "$patch_file"
git apply "$patch_file"

export GOMAXPROCS="${GOMAXPROCS:-2}" GOMEMLIMIT="${GOMEMLIMIT:-400MiB}" CGO_ENABLED=0
for target in linux windows; do
    extension=""
    tags=""
    [[ "$target" != windows ]] || extension=.exe
    [[ "$target" != windows ]] || tags=openflux_client
    mkdir -p "$build_dir/$target"
    GOOS="$target" GOARCH=amd64 "$go_bin" build -p 1 -trimpath -tags "$tags" -o "$build_dir/$target/openflux$extension" .
done

python3 - "$build_dir" "$output_dir" "$commit" "$patch_file" <<'PY'
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

build, output, commit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
source = build / 'source'
files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=source).decode().split('\0')
for target in ('linux', 'windows'):
    binary = build / target / ('openflux.exe' if target == 'windows' else 'openflux')
    archive = output / f'openflux-{target}-amd64.zip'
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(binary, binary.name)
        for name in files:
            # Upstream also checks in an unrelated prebuilt macOS binary.
            if name and name != 'universal-bypass-tool':
                z.write(source / name, 'source/' + name)
        for name in ('LICENSE', 'NOTICE', 'COPYRIGHT'):
            z.write(source / name, name)
        z.write(sys.argv[4], 'AFZVPN.patch')
        z.writestr('BUILD.txt', f'Source: https://github.com/p1neappleXpress/OpenFlux/tree/{commit}\n'
                   f'Commit: {commit}\nTarget: {target}/amd64; CGO_ENABLED=0\n'
                   'Build: go build -p 1 -trimpath -tags openflux_client . (Windows); omit tags on Linux\n'
                   'AFZVPN modifications: 2s reconnect delay; Windows SOCKS client without WinDivert.\n'
                   'Included source already contains AFZVPN.patch.\n'
                   'Experimental client. Launch instructions are in the AFZVPN bot.\n')
    temporary.replace(archive)
    print(archive.name, archive.stat().st_size)
checksums = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                    for p in sorted(output.glob('openflux-*-amd64.zip')))
(output / 'SHA256SUMS').write_text(checksums)
PY
