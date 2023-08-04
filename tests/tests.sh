#!/bin/bash
echo check: app is running?
wget -qO- https://raw.githubusercontent.com/eficode/wait-for/v2.2.2/wait-for | sh -s -- http://localhost:8000/status -- echo ok is running
echo "run test"
coverage run --omit=/modes,/tests -m pytest --junitxml=/tests/report.xml /tests
coverage report --omit=/models,/tests
coverage html -d=/tests/test_report.html
echo "make project: Done."