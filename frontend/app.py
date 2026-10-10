"""
Flask Frontend for Employee Attendance & Analytics API
Connects to FastAPI backend at BACKEND_URL (default: http://127.0.0.1:8000)
"""
import os
import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for

app = Flask(__name__)
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def api_request(method, path, **kwargs):
    """Helper to send requests to FastAPI backend with timeout."""
    url = f"{BACKEND_URL}{path}"
    try:
        kwargs.setdefault("timeout", 5)
        resp = requests.request(method, url, **kwargs)
        return resp
    except requests.exceptions.RequestException as e:
        return None


@app.context_processor
def inject_global_vars():
    return {
        "backend_url": BACKEND_URL,
    }


# ---------------------------------------------------------------------------
# HTML Page Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Main Dashboard with live stats and quick punch actions."""
    # Check backend health
    health_resp = api_request("GET", "/health")
    backend_online = health_resp is not None and health_resp.status_code == 200
    backend_status = "Online (MongoDB Connected)" if backend_online else "Offline / DB Unavailable"
    
    # Fetch recent attendance and employee count if backend is online
    recent_logs = []
    employees_count = 0
    if backend_online:
        emp_res = api_request("GET", "/employees?page=1&page_size=1")
        if emp_res and emp_res.status_code == 200:
            employees_count = emp_res.json().get("total", 0)

        att_res = api_request("GET", "/attendance?page=1&page_size=8")
        if att_res and att_res.status_code == 200:
            recent_logs = att_res.json().get("items", [])

    return render_template(
        "index.html",
        active_page="dashboard",
        backend_online=backend_online,
        backend_status=backend_status,
        employees_count=employees_count,
        recent_logs=recent_logs,
    )


@app.route("/employees")
def employees():
    """Employee Directory page with pagination & filtering."""
    dept = request.args.get("department", "").strip()
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 20))

    params = {"page": page, "page_size": page_size}
    if dept:
        params["department"] = dept

    res = api_request("GET", "/employees", params=params)
    data = res.json() if (res and res.status_code == 200) else {"items": [], "total": 0, "page": page, "page_size": page_size}

    # Fetch departments for filter dropdown
    all_emp_res = api_request("GET", "/employees?page=1&page_size=100")
    departments = sorted(list({e.get("department") for e in all_emp_res.json().get("items", [])})) if (all_emp_res and all_emp_res.status_code == 200) else []

    total_pages = max(1, (data["total"] + page_size - 1) // page_size)

    return render_template(
        "employees.html",
        active_page="employees",
        data=data,
        department=dept,
        departments=departments,
        current_page=page,
        total_pages=total_pages,
    )


@app.route("/attendance")
def attendance():
    """Attendance Records page with filtering & punch modals."""
    emp_code = request.args.get("emp_code", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    status = request.args.get("status", "").strip()
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 20))

    params = {"page": page, "page_size": page_size}
    if emp_code:
        params["emp_code"] = emp_code
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if status:
        params["status"] = status

    res = api_request("GET", "/attendance", params=params)
    data = res.json() if (res and res.status_code == 200) else {"items": [], "total": 0, "page": page, "page_size": page_size}
    total_pages = max(1, (data["total"] + page_size - 1) // page_size)

    return render_template(
        "attendance.html",
        active_page="attendance",
        data=data,
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        current_page=page,
        total_pages=total_pages,
    )


@app.route("/analytics")
def analytics():
    """Analytics Hub (Monthly, Departments, Leaderboard, Trend)."""
    return render_template("analytics.html", active_page="analytics")


@app.route("/explain")
def explain():
    """MongoDB Query Plan Visualizer."""
    return render_template("explain.html", active_page="explain")


# ---------------------------------------------------------------------------
# API Proxy Endpoints (consumed by interactive JS on the frontend)
# ---------------------------------------------------------------------------

@app.route("/api/health")
def proxy_health():
    res = api_request("GET", "/health")
    if res is None:
        return jsonify({"status": "error", "message": "FastAPI backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/employees", methods=["POST"])
def proxy_create_employee():
    payload = request.get_json() or {}
    res = api_request("POST", "/employees", json=payload)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/attendance/punch-in", methods=["POST"])
def proxy_punch_in():
    payload = request.get_json() or {}
    res = api_request("POST", "/attendance/punch-in", json=payload)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/attendance/punch-out", methods=["POST"])
def proxy_punch_out():
    payload = request.get_json() or {}
    res = api_request("POST", "/attendance/punch-out", json=payload)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/attendance/regularize/<emp_code>/<date>", methods=["PATCH", "POST"])
def proxy_regularize(emp_code, date):
    payload = request.get_json() or {}
    res = api_request("PATCH", f"/attendance/{emp_code}/{date}", json=payload)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/analytics/monthly")
def proxy_analytics_monthly():
    emp_code = request.args.get("emp_code")
    month = request.args.get("month")
    res = api_request("GET", f"/analytics/employees/{emp_code}/monthly?month={month}")
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/analytics/departments")
def proxy_analytics_departments():
    params = request.args.to_dict()
    res = api_request("GET", "/analytics/departments/summary", params=params)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/analytics/leaderboard")
def proxy_analytics_leaderboard():
    params = request.args.to_dict()
    res = api_request("GET", "/analytics/leaderboard/late", params=params)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/analytics/trend/<department>")
def proxy_analytics_trend(department):
    params = request.args.to_dict()
    res = api_request("GET", f"/analytics/departments/{department}/trend", params=params)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


@app.route("/api/explain/<endpoint>")
def proxy_explain(endpoint):
    params = request.args.to_dict()
    res = api_request("GET", f"/admin/explain/{endpoint}", params=params)
    if res is None:
        return jsonify({"detail": "Backend unreachable"}), 503
    return jsonify(res.json()), res.status_code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask Frontend on http://127.0.0.1:{port} -> Backend: {BACKEND_URL}")
    app.run(host="0.0.0.0", port=port, debug=True)
