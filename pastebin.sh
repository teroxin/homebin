#!/bin/bash
#
# apaste
#
# author: lydgate
# http://lydgate.nonlogic.org/archlinux/apaste.sh
#
# revision: 3
#
# history:
# r3: reads from stdin if no file given
# r2: spits out URL
# r1: just uploads
#
# Simple bash script to paste to archlinux's pastebin.  Requires urlencode.sh,
# file, and curl.
#
# urlencode.sh: http://www.acmesystems.it/articles/00080/urlencode.sh
#
# Todo:
# - check for dependencies

USER=bettse


case `file "$1"` in
    *Bourne*)
        TYPE="bash";;
    *python*)
        TYPE="python";;
    *perl*)
        TYPE="perl";;
    *HTML*)
        TYPE="html4strict";;
    *)
        TYPE="text";;
esac

DATA=`cat "$@" | urlencode.sh`

curl -d format=$TYPE -d code2="$DATA" -d poster=$USER \
  -d expiry=expiry_day -d paste=Send -i -s http://ericbetts.org/pastebin/\
  | grep Location
