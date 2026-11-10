#!/bin/sh
# Removes what install.sh put in place. Run with the same privilege level you
# installed with (plain user, or sudo for the /opt install).
set -eu

if [ "$(id -u)" = "0" ]; then
    PREFIX="/opt/vcfa-import"
    BINDIR="/usr/local/bin"
else
    PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/vcfa-import"
    BINDIR="$HOME/.local/bin"
fi

rm -f "$BINDIR/vcfa-import"
rm -rf "$PREFIX"

echo "Removed $PREFIX and $BINDIR/vcfa-import"
