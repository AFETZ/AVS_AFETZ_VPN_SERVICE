#!/usr/bin/env bash
set -euo pipefail
mobile_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$mobile_dir/../.." && pwd)"
tool_dir="${AFZ_MOBILE_TOOLS:-$repo_dir/.local/openflux-mobile-tools}"
export JAVA_HOME="${JAVA_HOME:-$tool_dir/jdk}"
export ANDROID_HOME="${ANDROID_HOME:-$tool_dir/sdk}"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$ANDROID_HOME/ndk/27.2.12479018}"
export PATH="$JAVA_HOME/bin:$PATH"
export GOMAXPROCS="${GOMAXPROCS:-2}" GOMEMLIMIT="${GOMEMLIMIT:-300MiB}"
build_dir="$mobile_dir/android/build"
output_dir="${AFZ_MOBILE_OUTPUT:-$repo_dir/app/data/openflux}"
sign_dir="${AFZ_MOBILE_SIGNING:-$repo_dir/.local/openflux-mobile-signing}"
sdk_tools="$ANDROID_HOME/build-tools/35.0.0"
android_jar="$ANDROID_HOME/platforms/android-35/android.jar"
mkdir -p "$build_dir" "$output_dir" "$sign_dir"
chmod 700 "$sign_dir"
test -f "$android_jar"
if [[ "${AFZ_SKIP_CORE:-0}" == 1 ]]; then
    test -f "$build_dir/core.aar"
else
(
    cd "$mobile_dir/core"
    gomobile bind -target=android/arm64,android/amd64 -androidapi=26 \
        -ldflags='-s -w -extldflags=-Wl,-z,max-page-size=16384' -o "$build_dir/core.aar" ./mobile
)
fi
python3 - "$build_dir" <<'PY'
from pathlib import Path
import shutil,sys,zipfile
root=Path(sys.argv[1])
for name in ('aar','classes','gen','dex'):
    p=root/name
    if p.exists():shutil.rmtree(p)
    p.mkdir()
with zipfile.ZipFile(root/'core.aar') as z:z.extractall(root/'aar')
PY
"$sdk_tools/aapt2" compile --dir "$mobile_dir/android/res" -o "$build_dir/resources.zip"
"$sdk_tools/aapt2" link --manifest "$mobile_dir/android/AndroidManifest.xml" \
    -I "$android_jar" --java "$build_dir/gen" --min-sdk-version 26 --target-sdk-version 35 \
    -o "$build_dir/base.apk" "$build_dir/resources.zip"
python3 - "$mobile_dir/android/src" "$build_dir" <<'PY'
from pathlib import Path
import sys
source,root=map(Path,sys.argv[1:])
(root/'java-sources.txt').write_text('\n'.join('"'+str(p)+'"' for directory in (source,root/'gen') for p in directory.rglob('*.java'))+'\n')
PY
javac -J-Xmx256m -encoding UTF-8 --release 8 \
    -classpath "$android_jar:$build_dir/aar/classes.jar" -d "$build_dir/classes" "@$build_dir/java-sources.txt"
python3 - "$build_dir" "$sdk_tools" "$android_jar" <<'PY'
import subprocess,sys
from pathlib import Path
root,tools,platform=map(Path,sys.argv[1:])
subprocess.run(['java','-Xmx256m','-cp',str(tools/'lib/d8.jar'),'com.android.tools.r8.D8',
    '--min-api','26','--lib',str(platform),'--output',str(root/'dex'),str(root/'aar/classes.jar'),
    *[str(p) for p in (root/'classes').rglob('*.class')]],check=True)
PY
python3 - "$build_dir" <<'PY'
import shutil,sys,zipfile
from pathlib import Path
root=Path(sys.argv[1]);shutil.copy2(root/'base.apk',root/'unsigned.apk')
with zipfile.ZipFile(root/'unsigned.apk','a') as z:
    for p in (root/'dex').glob('*.dex'):z.write(p,p.name,compress_type=zipfile.ZIP_DEFLATED)
    for p in (root/'aar/jni').rglob('*.so'):z.write(p,'lib/'+str(p.relative_to(root/'aar/jni')),compress_type=zipfile.ZIP_STORED)
PY
"$sdk_tools/zipalign" -P 16 -f 4 "$build_dir/unsigned.apk" "$build_dir/aligned.apk"
if [[ ! -f "$sign_dir/password" ]]; then
    (umask 077; openssl rand -hex 32 > "$sign_dir/password")
fi
export AFZ_KEYSTORE_PASSWORD
AFZ_KEYSTORE_PASSWORD="$(cat "$sign_dir/password")"
if [[ ! -f "$sign_dir/afzvpn.p12" ]]; then
    keytool -genkeypair -noprompt -keystore "$sign_dir/afzvpn.p12" -storetype PKCS12 \
        -storepass:env AFZ_KEYSTORE_PASSWORD -keypass:env AFZ_KEYSTORE_PASSWORD -alias afzvpn \
        -keyalg RSA -keysize 3072 -validity 3650 -dname 'CN=AFZVPN OpenFlux pilot'
    chmod 600 "$sign_dir/afzvpn.p12"
fi
"$sdk_tools/apksigner" sign --ks "$sign_dir/afzvpn.p12" --ks-key-alias afzvpn \
    --ks-pass env:AFZ_KEYSTORE_PASSWORD --key-pass env:AFZ_KEYSTORE_PASSWORD \
    --out "$output_dir/afz-openflux-android.apk" "$build_dir/aligned.apk"
unset AFZ_KEYSTORE_PASSWORD
"$sdk_tools/apksigner" verify --verbose --print-certs "$output_dir/afz-openflux-android.apk"
"$sdk_tools/zipalign" -c -P 16 4 "$output_dir/afz-openflux-android.apk"
echo "$output_dir/afz-openflux-android.apk"
