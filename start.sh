#!/bin/bash
cd ~/workspace/nas-claude-hub
unset CLAUDECODE
mkdir -p data
~/python313/python/bin/python3 main.py >> data/main_test.log 2>&1
