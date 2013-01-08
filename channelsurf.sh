#!/bin/bash

while :
do

    SHOW=$(find . -type d | grep -v season | shuf | head -n1)

    EPISODE=$(find "$SHOW" -type f | grep -v .DS_Store | shuf | head -n1)

    mplayer -fs "$EPISODE"
    read code
    if [ $code == 1 ]
    then
        exit
    fi
done
