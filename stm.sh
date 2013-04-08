#!/bin/bash


temp=$HOME/.stm

if [[ ! -f $temp ]]; then
    #Stole old values
    defaults read com.projectswithlove.servetome STMSharedFolderPaths > $temp 
fi

if [[ $# > 0 && $1 -eq 'add' ]]; then
    echo "Add"
    #add download
    defaults write com.projectswithlove.servetome STMSharedFolderPaths -array-add  "/Users/bettse/Downloads"
else
    echo "Restore"
    #restore old values
    folders=`cat $temp |tr -d ')' | tr -d ',' | tr -d '(' | tr -d '"'`
    defaults write com.projectswithlove.servetome STMSharedFolderPaths -array $folders
fi

defaults read com.projectswithlove.servetome STMSharedFolderPaths
