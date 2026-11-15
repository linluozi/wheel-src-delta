#!/bin/sh
# Exact commands used for the 2026-11-16 evidence set.
set -u
run() { # project version repo ref [extra...]
  p=$1; v=$2; r=$3; ref=$4; shift 4
  python3 -m wheelsrcdelta check --project "$p" --version "$v" --against git \
    --repo "git@github.com:$r.git" --ref "$ref" --json-out "evidence/certs/$p.json" "$@"
}
run attrs 25.4.0 python-attrs/attrs 25.4.0
run click 8.5.1 pallets/click 8.5.1
run coverage 7.17.3 nedbat/coveragepy 7.17.3
run flask 3.1.3 pallets/flask 3.1.3
run isort 6.1.0 PyCQA/isort 6.1.0
run markupsafe 3.0.4 pallets/markupsafe 3.0.4
run packaging 26.3 pypa/packaging 26.3
run pytest 9.2.0 pytest-dev/pytest 9.2.0
run six 1.17.0 benjaminp/six 1.17.0
run tomlkit 0.15.2 python-poetry/tomlkit 0.15.2
run tox 4.67.13 tox-dev/tox 4.67.13
run virtualenv 21.15.6 pypa/virtualenv 21.15.6
# explicit allowlist for the three build-generated version modules
python3 -m wheelsrcdelta check --project tox --version 4.67.13 --against git \
  --repo git@github.com:tox-dev/tox.git --ref 4.67.13 --allow '*/version.py' \
  --json-out evidence/allowed/tox.json
python3 -m wheelsrcdelta check --project virtualenv --version 21.15.6 --against git \
  --repo git@github.com:pypa/virtualenv.git --ref 21.15.6 --allow '*/version.py' \
  --json-out evidence/allowed/virtualenv.json
python3 -m wheelsrcdelta check --project pytest --version 9.2.0 --against git \
  --repo git@github.com:pytest-dev/pytest.git --ref 9.2.0 --allow '*/_version.py' \
  --json-out evidence/allowed/pytest.json
# negative controls: newer wheel, older tag
python3 -m wheelsrcdelta check --project attrs --version 25.4.0 --against git \
  --repo git@github.com:python-attrs/attrs.git --ref 25.3.0 \
  --json-out evidence/negative/attrs-vs-25.3.0.json
python3 -m wheelsrcdelta check --project six --version 1.17.0 --against git \
  --repo git@github.com:benjaminp/six.git --ref 1.16.0 \
  --json-out evidence/negative/six-vs-1.16.0.json
