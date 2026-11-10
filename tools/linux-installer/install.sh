#!/bin/sh
# Installs vcfa-import from the directory this script lives in (the extracted
# vcfa-import-<ver>-linux-<arch>-installer.tar.gz). No pip, no system packages,
# no network access needed -- the payload is a self-contained PyInstaller build.
#
#   ./install.sh        installs to ~/.local/share/vcfa-import, links into ~/.local/bin
#   sudo ./install.sh   installs to /opt/vcfa-import, links into /usr/local/bin
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$HERE/vcfa-import"

if [ ! -x "$PAYLOAD/vcfa-import" ]; then
    echo "install.sh must be run from inside the extracted installer directory" >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    PREFIX="/opt/vcfa-import"
    BINDIR="/usr/local/bin"
else
    PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/vcfa-import"
    BINDIR="$HOME/.local/bin"
fi

mkdir -p "$(dirname "$PREFIX")" "$BINDIR"
rm -rf "$PREFIX"
cp -r "$PAYLOAD" "$PREFIX"
ln -sf "$PREFIX/vcfa-import" "$BINDIR/vcfa-import"

echo "Installed to $PREFIX"
echo "Linked     $BINDIR/vcfa-import"

case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)
        echo
        echo "$BINDIR is not on your PATH. Add it, e.g.:"
        echo "  echo 'export PATH=\"$BINDIR:\$PATH\"' >> ~/.profile"
        ;;
esac

echo
echo "Before first use, read and accept the terms of use:"
echo "  vcfa-import disclaimer"
echo "  vcfa-import accept"
