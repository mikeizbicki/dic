#!/bin/bash

set -e

# source the geni.sh script
scriptpath=$(readlink -f "$0")
genipath="$(dirname "$scriptpath")/../scripts/geni.sh"
source "$genipath"

# move to a temp folder
tmpdir=$(mktemp -d) && cd "$tmpdir"

# print a prompt and run a command
run() {
    echo "\$ $*"
    "$@" || exit $?
    echo
}

# the actual shell session starts here
run pwd
run git init --quiet
run sh -c "echo __pycache__ > .gitignore"
run git add .gitignore
run git commit -m 'add gitignore'
run geni 'create a file primes.py that computes the first 100 primes'
run python3 -m doctest primes.py
run geni -c 'add doctests (use patches)'
run python3 -m doctest primes.py
