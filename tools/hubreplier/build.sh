#!/usr/bin/env bash
# 构建 com.chenty.hubreplier（不依赖 gradle；只用 javac + d8 + aapt2 + zipalign + apksigner）。
# 契约与用法见 README.md §1/§5。所有路径可用环境变量覆盖。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ANDROID_JAR="${ANDROID_JAR:-$ANDROID_SDK/platforms/android-36/android.jar}"
BUILD_TOOLS="${BUILD_TOOLS:-$BUILD_TOOLS}"
AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"
AAPT="$BUILD_TOOLS/aapt"
KEYSTORE="${KEYSTORE:-$HERE/hubreplier-debug.keystore}"
KS_PASS="${KS_PASS:-android}"
KEY_ALIAS="${KEY_ALIAS:-hubreplier}"
APK_NAME="${APK_NAME:-hubreplier-0.1.0-debug.apk}"
BUILD="$HERE/build"

for f in "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER" "$ANDROID_JAR"; do
  [ -e "$f" ] || { echo "FATAL: 缺少工具链文件 $f" >&2; exit 1; }
done

echo "==> [1/6] 清理 $BUILD（只清构建产物，不用 rm -rf：本机 rm 会移入备份区）"
case "$BUILD" in
  */hubreplier/build) ;;
  *) echo "FATAL: 拒绝清理非预期目录 $BUILD" >&2; exit 1 ;;
esac
mkdir -p "$BUILD"
find "$BUILD" -mindepth 1 -delete
mkdir -p "$BUILD/classes" "$BUILD/dex" "$BUILD/gen"

echo "==> [2/6] aapt2 compile + link（android.jar=$ANDROID_JAR）"
"$AAPT2" compile --dir res -o "$BUILD/res.zip"
"$AAPT2" link \
  -o "$BUILD/base.apk" \
  -I "$ANDROID_JAR" \
  --manifest AndroidManifest.xml \
  --java "$BUILD/gen" \
  --auto-add-overlay \
  -R "$BUILD/res.zip"

echo "==> [3/6] javac（-source/-target 8 + bootclasspath=android.jar，d8 负责 desugar）"
# JDK 21 不允许 -bootclasspath 与 -source 17 同用；本工程不用 Java 9+ 语法，故锁 8
javac -source 8 -target 8 -encoding UTF-8 -Xlint:-options \
  -bootclasspath "$ANDROID_JAR" -classpath "$ANDROID_JAR" \
  -d "$BUILD/classes" \
  $(find src "$BUILD/gen" -name '*.java')

echo "==> [4/6] d8 → classes.dex（min-api 26）"
# shellcheck disable=SC2046
"$D8" --release --min-api 26 --lib "$ANDROID_JAR" --output "$BUILD/dex" \
  $(find "$BUILD/classes" -name '*.class')

echo "==> [5/6] 组装 + zipalign"
cp "$BUILD/base.apk" "$BUILD/unsigned.apk"
( cd "$BUILD" && zip -q -j unsigned.apk dex/classes.dex )
"$ZIPALIGN" -f -p 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"

echo "==> [6/6] 签名（debug keystore: $KEYSTORE）"
if [ ! -f "$KEYSTORE" ]; then
  echo "    生成 debug keystore"
  keytool -genkeypair -v \
    -keystore "$KEYSTORE" -alias "$KEY_ALIAS" \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -dname "CN=hubreplier, OU=info-hub, O=chenty, L=Beijing, ST=Beijing, C=CN" >/dev/null
fi
"$APKSIGNER" sign \
  --ks "$KEYSTORE" --ks-key-alias "$KEY_ALIAS" \
  --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
  --v1-signing-enabled true --v2-signing-enabled true \
  --out "$HERE/$APK_NAME" "$BUILD/aligned.apk"

echo "==> 校验"
"$APKSIGNER" verify --print-certs "$HERE/$APK_NAME" | head -5
if [ -x "$AAPT" ]; then
  "$AAPT" dump badging "$HERE/$APK_NAME" | grep -E "^package:|sdkVersion|targetSdkVersion|launchable-activity|uses-permission" | head -8
fi
unzip -l "$HERE/$APK_NAME" | grep -E "classes.dex|AndroidManifest.xml|resources.arsc" || true
ls -la "$HERE/$APK_NAME"
echo "OK: $HERE/$APK_NAME"
