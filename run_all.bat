@echo off
echo ====================================================
echo Starting Full Stack: FastAPI Backend + Flask UI
echo ====================================================

echo [1/2] Starting FastAPI Backend on port 8000...
start "FastAPI Backend (Port 8000)" cmd /k "python -m uvicorn app.main:app --port 8000"

timeout /t 2 > NUL

echo [2/2] Starting Flask Frontend on port 5000...
start "Flask Frontend (Port 5000)" cmd /k "python frontend/app.py"

timeout /t 1 > NUL

echo.
echo Both servers started!
echo - API Docs (Swagger): http://127.0.0.1:8000/docs
echo - Web Dashboard:      http://127.0.0.1:5000
echo.
echo Opening Web Dashboard in browser...
start http://127.0.0.1:5000
