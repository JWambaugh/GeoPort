#!/usr/bin/env fish
# Run GeoPort v5 from source.
# macOS + Homebrew + fish: `brew install python@3.13` so `python3.13` is on PATH.
set -l root (dirname (status filename))
cd $root

if not test -d .venv
    if command -q python3.13
        python3.13 -m venv .venv
    else
        python3 -m venv .venv
    end
end

source .venv/bin/activate.fish
python -m pip install -r requirements.txt
python src/main.py $argv
