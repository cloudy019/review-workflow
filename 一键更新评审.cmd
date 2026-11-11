@echo off
cd /d "%~dp0"
python -m review_workflow.cli request-workbuddy-refresh --launch
pause
