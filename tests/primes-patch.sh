#!/bin/bash

set -e

# source the committe.sh script
scriptpath=$(readlink -f "$0")
committepath="$(dirname "$scriptpath")/../scripts/committe.sh"
source "$committepath"

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
run committe 'create a file primes.py that computes the first 100 primes'
run python3 -m doctest primes.py
run committe -c 'add doctests (use patches)'
run python3 -m doctest primes.py
