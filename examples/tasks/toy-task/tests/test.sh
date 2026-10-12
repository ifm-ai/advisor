#!/bin/bash
# Harbor runs this inside the task container after the agent finishes.
mkdir -p /logs/verifier
cd /app
if python -m pytest -q /tests/test_outputs.py; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
