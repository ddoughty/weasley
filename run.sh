#!/usr/bin/env bash
PIPENV_DONT_LOAD_ENV=1 op run --env-file=.env.op -- pipenv run python main.py "$@"
