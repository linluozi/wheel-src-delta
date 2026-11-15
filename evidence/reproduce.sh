#!/bin/sh
# Exact commands used for the 2026-11-16 evidence set.
set -u
run() { # project version repo ref
  python3 -m wheelsrcdelta check --project "$1" --version "$2" --against git \
    --repo "git@github.com:$3.git" --ref "$4" --json-out "evidence/certs/$1.json"
}
run attrs 25.4.0 python-attrs/attrs 25.4.0
run click 8.5.1 pallets/click 8.5.1
run packaging 26.3 pypa/packaging 26.3
run markupsafe 3.0.4 pallets/markupsafe 3.0.4
run tomlkit 0.15.2 python-poetry/tomlkit 0.15.2
run six 1.17.0 benjaminp/six 1.17.0
# negative controls: newer wheel, older tag
python3 -m wheelsrcdelta check --project attrs --version 25.4.0 --against git \
  --repo git@github.com:python-attrs/attrs.git --ref 25.3.0 \
  --json-out evidence/negative/attrs-vs-25.3.0.json
python3 -m wheelsrcdelta check --project six --version 1.17.0 --against git \
  --repo git@github.com:benjaminp/six.git --ref 1.16.0 \
  --json-out evidence/negative/six-vs-1.16.0.json
