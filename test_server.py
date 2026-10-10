import subprocess, sys, time, os, requests

os.chdir(r"E:\candidate_kit")

# Start server
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8000"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
time.sleep(3)

try:
    r = requests.get("http://127.0.0.1:8000/health", timeout=5)
    print(f"Health: {r.status_code} {r.json()}")
except Exception as e:
    print(f"Health Error: {e}")

# Test create employee
try:
    r = requests.post("http://127.0.0.1:8000/employees", json={
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    }, timeout=5)
    print(f"Create employee: {r.status_code} {r.json()}")
except Exception as e:
    print(f"Create employee Error: {e}")

proc.terminate()
proc.wait()
print("Done testing")