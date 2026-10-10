@echo off
echo Starting Attendance & Analytics API with Integrated UI on port 8000...
start http://localhost:8000
python -m uvicorn app.main:app --port 8000 --reload
pause