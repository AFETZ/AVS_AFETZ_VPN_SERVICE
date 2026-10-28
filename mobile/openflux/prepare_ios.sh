#!/usr/bin/env bash
set -euo pipefail
mobile_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
    echo 'iOS frameworks require macOS and Xcode. Android and the shared core can be built on Linux.' >&2
    exit 1
fi
xcodebuild -version
command -v xcodegen >/dev/null
command -v gomobile >/dev/null
mkdir -p "$mobile_dir/ios/Frameworks"
(
    cd "$mobile_dir/core"
    gomobile bind -target=ios -iosversion=15.0 -prefix=AFZ -ldflags='-s -w' \
        -o "$mobile_dir/ios/Frameworks/Afzcore.xcframework" ./mobile
)
cd "$mobile_dir/ios"
xcodegen generate --spec project.yml
echo 'Open ios/AFZOpenFlux.xcodeproj, select a signing Team for both targets, and run on your iPhone.'
