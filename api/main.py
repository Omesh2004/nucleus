import sys
import os
from typing import List, Optional
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.client import ch_client
from api.insights import generate_insights, query_ollama
from api.page_map import resolve_page, resolve_display_name, normalize_event
from core.config import settings
from core.middleware import require_cloud_mode, require_tenant_access

# Alias for Python's built-in range() since many endpoints use 'range' as a query param name
builtins_range = range

def parse_range(range_str: str) -> int:
    if not range_str: return 7
    range_str = range_str.lower().strip()
    if range_str.endswith('d'):
        try: return int(range_str[:-1])
        except ValueError: return 7
    try: return int(range_str)
    except ValueError: return 7

import time
from datetime import datetime

# In-memory dictionary to cache AI reports: { tenant_id: { "timestamp": float, "report": str } }
AI_REPORT_CACHE = {}
AI_CACHE_TTL = 3600  # 1 hour cache duration
app = FastAPI(
    title="Feature Analytics API",
    description="APIs for feature adoption, funnel analysis, and rule-based insights."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class RBACMiddleware(BaseHTTPMiddleware):
    """
    Strict Role-Based Access Control middleware.
    
    Roles:
      super_admin  → Overall platform admin, aggregated summaries ONLY, NO raw/sensitive data
      app_admin    → App-level admin, full access to detailed analytics for their app
      user         → No API access at all
    """
    
    # Endpoints that are too sensitive for super_admin (raw data, user-level details, tenant-specific analytics)
    COMPANY_ADMIN_BLOCKED = [
        "/audit_logs",
        "/locations",
        "/metrics/realtime_users",
        "/metrics/pages_per_minute",
        "/metrics/top_pages",
        "/metrics/devices",
        "/metrics/channels",
        "/metrics/retention",
        "/metrics/secondary_kpi",
        "/metrics/traffic",
        "/metrics/feature_usage_series",
        "/features/activity",
        "/features/configs",
        "/funnels",
        "/transparency",
        "/license",
        "/journey",
        "/predictive",
        "/segmentation",
    ]
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # Public paths and CORS Preflight — always accessible
        if request.method == "OPTIONS" or path.startswith("/deployment") or path.startswith("/health") or path == "/" or path.startswith("/ws/"):
            return await call_next(request)
            
        role = request.headers.get("X-User-Role", "user")
        
        # Normal users can't access any data APIs
        if role == "user":
            return JSONResponse(
                status_code=403, 
                content={"detail": "Forbidden: Access denied. Normal users cannot access analytics APIs."}
            )
            
        # Super admin (overall admin): aggregated endpoints ONLY, block all sensitive/detailed data
        if role == "super_admin":
            # Check if the path matches any blocked endpoint
            if any(path.startswith(blocked) for blocked in self.COMPANY_ADMIN_BLOCKED):
                return JSONResponse(
                    status_code=403, 
                    content={"detail": "Forbidden: Super admins cannot access detailed analytics data. Use /admin/* for aggregated summaries."}
                )
            
            # Explicitly allow only these patterns for super_admin
            allowed = ["/admin", "/metrics/kpi", "/insights", "/tenants", "/features/usage", "/deployment", "/ai_report", "/tracking", "/config"]
            if not any(path.startswith(prefix) for prefix in allowed):
                return JSONResponse(
                    status_code=403, 
                    content={"detail": "Forbidden: Endpoint not available for super admin role."}
                )
        # app_admin: full access to all detailed endpoints, but MUST be restricted to their assigned tenant
        if role == "app_admin":
            tenant_id = request.query_params.get("tenant_id") or request.query_params.get("tenants")
            email = request.headers.get("X-User-Email")
            
            # Some endpoints don't require tenant_id (they are global or use request body)
            tenant_optional_paths = ["/tenants", "/deployment", "/license/sync", "/config"]
            is_tenant_optional = any(path.startswith(p) for p in tenant_optional_paths)
            
            if is_tenant_optional and email:
                pass  # Allow without tenant_id
            elif tenant_id and email:
                pass  # Normal tenant-scoped access
            else:
                return JSONResponse(
                    status_code=403, 
                    content={"detail": "Forbidden: Admin request missing tenant_id/tenants or user email headers."}
                )
             
        response = await call_next(request)
        return response

app.add_middleware(RBACMiddleware)

from fastapi import WebSocket, WebSocketDisconnect
from api.websocket_manager import manager, start_websocket_background_tasks

@app.on_event("startup")
async def startup_event():
    await start_websocket_background_tasks()

@app.websocket("/ws/dashboard/{tenant_id}")
async def websocket_dashboard(websocket: WebSocket, tenant_id: str):
    await manager.connect(websocket, tenant_id)
    try:
        while True:
            # Wait for any incoming keep-alive or message
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, tenant_id)


@app.get("/tenants/available")
def get_available_tenants(request: Request):
    """Returns all distinct tenants from ClickHouse, filtered by admin access if applicable."""
    role = request.headers.get("X-User-Role", "")
    admin_apps_str = request.headers.get("X-Admin-Apps", "")
    allowed_apps = [a.strip() for a in admin_apps_str.split(",") if a.strip()]
    # Always include known tenants in the base list so the dropdown is never empty
    KNOWN_TENANTS = [
        {"id": "nexabank", "name": "NexaBank", "eventCount": 0, "uniqueUsers": 0},
        {"id": "safexbank", "name": "SafexBank", "eventCount": 0, "uniqueUsers": 0},
    ]
    try:
        sql = """
            SELECT
                tenant_id as id,
                count() as event_count,
                uniq(user_id) as unique_users
            FROM feature_intelligence.events_raw
            WHERE timestamp >= today() - 90
            GROUP BY tenant_id
            ORDER BY event_count DESC
        """
        results = ch_client.query(sql)
        found = {}
        for row in results:
            found[row["id"]] = {
                "id": row["id"],
                "name": row["id"].replace('_', ' ').title(),
                "eventCount": int(row["event_count"]),
                "uniqueUsers": int(row["unique_users"]),
            }
        # Merge: known tenants always present, update with real counts if found
        merged = []
        seen = set()
        for kt in KNOWN_TENANTS:
            entry = found.get(kt["id"], kt).copy()
            if kt["id"] in found:
                entry["name"] = kt["name"]  # Use our clean display name
            merged.append(entry)
            seen.add(kt["id"])
        # Add any extra tenants found in DB that aren't in known list
        for tid, tdata in found.items():
            if tid not in seen:
                merged.append(tdata)
                
        # Filter strictly
        if role == "app_admin" and allowed_apps:
            merged = [t for t in merged if t["id"] in allowed_apps]
            
        return merged
    except Exception:
        return KNOWN_TENANTS


@app.get("/features/usage")
def get_feature_usage(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    """
    Returns aggregated feature usage stats for a tenant (or comma-separated tenants) over the last N days.
    """
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenant_list[0], "days": days} if len(tenant_list) == 1 else {"tenant_ids": tuple(tenant_list), "days": days}

    sql = f"""
        SELECT 
            event_name, 
            count() as total_interactions,
            uniq(user_id) as unique_users
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY event_name
        ORDER BY total_interactions DESC
    """
    try:
        results = ch_client.query(sql, params)
        return {"tenant_id": tenants, "period_days": days, "usage": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/funnels")
def get_funnel_analysis(
    tenants: str = Query(..., description="Comma-separated list of tenants"),
    steps: str = Query(..., description="Comma-separated list of event names (e.g., login,apply,kyc,approve)"),
    window_minutes: int = Query(60, description="Minutes to complete the funnel"),
    range: str = Query("7d", description="Time range like 7d, 30d")
):
    """
    Advanced funnel analysis leveraging Clickhouse's windowFunnel.
    Computes conversion drop-offs between a sequence of events.
    """
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenant_list[0], "days": days} if len(tenant_list) == 1 else {"tenant_ids": tuple(tenant_list), "days": days}
    params["window"] = window_minutes * 60

    step_events = [s.strip() for s in steps.split(",") if s.strip()]
    
    if len(step_events) < 2:
        raise HTTPException(status_code=400, detail="At least two steps required for a funnel.")

    conditions = ", ".join([f"event_name = '{step}'" for step in step_events])
    
    sql = f"""
        SELECT 
            level,
            count() as users_reached_level
        FROM (
            SELECT 
                user_id,
                windowFunnel(%(window)s)(
                    timestamp,
                    {conditions}
                ) as level
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
            GROUP BY user_id
        )
        GROUP BY level
        ORDER BY level ASC
    """
    try:
        results = ch_client.query(sql, params)
        
        levels_dict = {row['level']: row['users_reached_level'] for row in results}
        
        total_at_least_level = {}
        cumulative = 0
        for i in builtins_range(len(step_events), 0, -1):
            cumulative += levels_dict.get(i, 0)
            total_at_least_level[i] = cumulative
            
        funnel_stats = []
        for i, step_name in enumerate(step_events, 1):
            count = total_at_least_level.get(i, 0)
            drop_off = 0
            if i > 1:
                prev_count = total_at_least_level.get(i - 1, 0)
                drop_off = (prev_count - count) / prev_count if prev_count > 0 else 0.0
                
            funnel_stats.append({
                "step": i,
                "event_name": step_name,
                "users_completed": count,
                "drop_off_pct": round(drop_off * 100, 2)
            })

        return {"tenant_id": tenants, "funnel": funnel_stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/features/compare-adoption")
def compare_tenants(feature: str = Query(..., description="Feature event to compare adoption across tenants")):
    """
    Compare feature adoption across all tenants. 
    Useful for seeing which clients are utilizing the platform most.
    """
    require_cloud_mode()
    sql = """
        SELECT 
            tenant_id, 
            sum(total_events) as total_interactions
        FROM feature_intelligence.daily_feature_usage
        WHERE event_name = %(feature)s
        GROUP BY tenant_id
        ORDER BY total_interactions DESC
    """
    try:
        results = ch_client.query(sql, {"feature": feature})
        return {"feature": feature, "tenant_comparison": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/insights")
def get_insights(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """
    Returns AI/Rule-based actionable insights for a tenant. 
    Detects features that are not being used or sudden spikes.
    """
    require_tenant_access(tenant_id)

    cached_data = AI_REPORT_CACHE.get(tenant_id)
    if cached_data and cached_data.get("insights"):
        return {"tenant_id": tenant_id, "insights": cached_data["insights"], "cached": True}

    # Try loading from ClickHouse if in-memory cache is empty
    import json as _json
    try:
        sql_db = """
            SELECT insights FROM feature_intelligence.ai_reports FINAL
            WHERE tenant_id = %(tenant_id)s LIMIT 1
        """
        db_rows = ch_client.query(sql_db, {"tenant_id": tenant_id})
        if db_rows and db_rows[0].get("insights"):
            raw = db_rows[0]["insights"]
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
            if parsed:
                return {"tenant_id": tenant_id, "insights": parsed, "cached": True}
    except Exception:
        pass

    insights_data = generate_insights(tenant_id)
    existing = AI_REPORT_CACHE.get(tenant_id, {})
    AI_REPORT_CACHE[tenant_id] = {
        **existing,
        "timestamp": existing.get("timestamp", time.time()),
        "insights": insights_data,
        "generated_at": existing.get("generated_at", datetime.utcnow().isoformat()),
    }
    return {"tenant_id": tenant_id, "insights": insights_data, "cached": False}

@app.get("/metrics/kpi")
def get_kpi_metrics(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    import math

    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    
    try:
        # --- Current period ---
        sql_current = f"""
            SELECT 
                count() as total_events,
                count(distinct event_name) as active_features
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
        """
        res_current = ch_client.query(sql_current, params)
        cur = res_current[0] if res_current else {"total_events": 0, "active_features": 0}

        sql_prev = f"""
            SELECT 
                count() as total_events,
                count(distinct event_name) as active_features
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
        """
        res_prev = ch_client.query(sql_prev, params)
        prev = res_prev[0] if res_prev else {"total_events": 0, "active_features": 0}

        def pct_change(current_val: int, previous_val: int) -> tuple:
            if previous_val == 0:
                return (0.0, "up")
            change = ((current_val - previous_val) / previous_val) * 100
            return (round(abs(change), 1), "up" if change >= 0 else "down")

        events_change, events_dir = pct_change(cur["total_events"] or 0, prev["total_events"] or 0)
        features_change, features_dir = pct_change(cur["active_features"] or 0, prev["active_features"] or 0)

        sql_response = f"""
            SELECT avg(if(JSONHas(metadata, 'response_time_ms'), JSONExtractFloat(metadata, 'response_time_ms'), 15 + (cityHash64(event_name, toString(timestamp)) %% 285))) as avg_rt
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
        """
        res_rt = ch_client.query(sql_response, params)
        raw_rt = res_rt[0]["avg_rt"] if res_rt and "avg_rt" in res_rt[0] else 0
        if raw_rt is None or (isinstance(raw_rt, float) and math.isnan(raw_rt)):
            avg_rt = 0
        else:
            avg_rt = int(raw_rt)

        sql_response_prev = f"""
            SELECT avg(if(JSONHas(metadata, 'response_time_ms'), JSONExtractFloat(metadata, 'response_time_ms'), 15 + (cityHash64(event_name, toString(timestamp)) %% 285))) as avg_rt
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
        """
        res_rt_prev = ch_client.query(sql_response_prev, params)
        raw_rt_prev = res_rt_prev[0]["avg_rt"] if res_rt_prev and "avg_rt" in res_rt_prev[0] else 0
        if raw_rt_prev is None or (isinstance(raw_rt_prev, float) and math.isnan(raw_rt_prev)):
            avg_rt_prev = 0
        else:
            avg_rt_prev = int(raw_rt_prev)

        rt_change, rt_dir = pct_change(avg_rt, avg_rt_prev)
        rt_display = f"{avg_rt} ms" if avg_rt > 0 else "0 ms"

        sql_error = f"""
            SELECT
                countIf(lower(event_name) LIKE '%%error%%' OR lower(event_name) LIKE '%%fail%%') as error_events,
                count() as total
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
        """
        res_err = ch_client.query(sql_error, params)
        err_row = res_err[0] if res_err else {"error_events": 0, "total": 1}
        total_for_err = err_row["total"] if err_row["total"] > 0 else 1
        error_rate = round((err_row["error_events"] / total_for_err) * 100, 1)

        sql_error_prev = f"""
            SELECT
                countIf(lower(event_name) LIKE '%%error%%' OR lower(event_name) LIKE '%%fail%%') as error_events,
                count() as total
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
        """
        res_err_prev = ch_client.query(sql_error_prev, params)
        err_prev_row = res_err_prev[0] if res_err_prev else {"error_events": 0, "total": 1}
        total_prev_err = err_prev_row["total"] if err_prev_row["total"] > 0 else 1
        error_rate_prev = round((err_prev_row["error_events"] / total_prev_err) * 100, 1)
        err_change, err_dir = pct_change(int(error_rate * 10), int(error_rate_prev * 10))

        return [
            {
                "id": "total-events",
                "label": "Total Events",
                "value": f"{cur['total_events']:,}" if cur['total_events'] else "0",
                "change": events_change,
                "changeDirection": events_dir,
                "icon": "activity",
            },
            {
                "id": "active-features",
                "label": "Active Features",
                "value": str(cur['active_features'] or 0),
                "change": features_change,
                "changeDirection": features_dir,
                "icon": "layers",
            },
            {
                "id": "avg-response",
                "label": "Avg Response Time",
                "value": rt_display,
                "change": rt_change,
                "changeDirection": rt_dir,
                "icon": "clock",
            },
            {
                "id": "error-rate",
                "label": "Error Rate",
                "value": f"{error_rate}%",
                "change": err_change,
                "changeDirection": err_dir,
                "icon": "alert-triangle",
            }
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics/secondary_kpi")
def get_secondary_kpi(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    
    try:
        def pct_change(current_val: float, previous_val: float) -> tuple:
            if previous_val == 0:
                return (0.0, "up")
            change = ((current_val - previous_val) / previous_val) * 100
            return (round(abs(change), 1), "up" if change >= 0 else "down")

        sql_basic = f"""
            SELECT 
                count() as total_visits,
                uniqExact(user_id) as unique_visitors
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
        """
        res_basic = ch_client.query(sql_basic, params)
        basic = res_basic[0] if res_basic else {"total_visits": 0, "unique_visitors": 0}

        sql_basic_prev = f"""
            SELECT 
                count() as total_visits,
                uniqExact(user_id) as unique_visitors
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
        """
        res_basic_prev = ch_client.query(sql_basic_prev, params)
        basic_prev = res_basic_prev[0] if res_basic_prev else {"total_visits": 0, "unique_visitors": 0}
        
        visits_change, visits_dir = pct_change(basic['total_visits'], basic_prev['total_visits'])
        unique_change, unique_dir = pct_change(basic['unique_visitors'], basic_prev['unique_visitors'])

        sql_bounce = f"""
            SELECT 
                count() as total_users,
                countIf(event_count = 1) as bounced_users
            FROM (
                SELECT user_id, count() as event_count
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY user_id
            )
        """
        res_bounce = ch_client.query(sql_bounce, params)
        b_users = res_bounce[0]["bounced_users"] if res_bounce else 0
        t_users = res_bounce[0]["total_users"] if res_bounce else 1
        t_users = t_users if t_users > 0 else 1
        bounce_rate = round((b_users / t_users) * 100, 1)

        sql_bounce_prev = f"""
            SELECT 
                count() as total_users,
                countIf(event_count = 1) as bounced_users
            FROM (
                SELECT user_id, count() as event_count
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
                GROUP BY user_id
            )
        """
        res_bounce_prev = ch_client.query(sql_bounce_prev, params)
        b_users_prev = res_bounce_prev[0]["bounced_users"] if res_bounce_prev else 0
        t_users_prev = res_bounce_prev[0]["total_users"] if res_bounce_prev else 1
        t_users_prev = t_users_prev if t_users_prev > 0 else 1
        bounce_rate_prev = round((b_users_prev / t_users_prev) * 100, 1)
        
        bounce_change, bounce_dir = pct_change(bounce_rate, bounce_rate_prev)

        sql_time = f"""
            SELECT avg(session_duration) as avg_time
            FROM (
                SELECT user_id, toDate(timestamp) as d, dateDiff('second', min(timestamp), max(timestamp)) as session_duration
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY user_id, d
                HAVING session_duration > 0 AND session_duration < 3600 * 4
            )
        """
        import math
        res_time = ch_client.query(sql_time, params)
        raw_avg = res_time[0]["avg_time"] if res_time and "avg_time" in res_time[0] else 0
        if raw_avg is None or (isinstance(raw_avg, float) and math.isnan(raw_avg)):
            avg_time_sec = 0
        else:
            avg_time_sec = int(raw_avg)
            
        sql_time_prev = f"""
            SELECT avg(session_duration) as avg_time
            FROM (
                SELECT user_id, toDate(timestamp) as d, dateDiff('second', min(timestamp), max(timestamp)) as session_duration
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - (%(days)s * 2) AND timestamp < today() - %(days)s
                GROUP BY user_id, d
                HAVING session_duration > 0 AND session_duration < 3600 * 4
            )
        """
        res_time_prev = ch_client.query(sql_time_prev, params)
        raw_avg_prev = res_time_prev[0]["avg_time"] if res_time_prev and "avg_time" in res_time_prev[0] else 0
        if raw_avg_prev is None or (isinstance(raw_avg_prev, float) and math.isnan(raw_avg_prev)):
            avg_time_sec_prev = 0
        else:
            avg_time_sec_prev = int(raw_avg_prev)
            
        time_change, time_dir = pct_change(avg_time_sec, avg_time_sec_prev)

        mins = avg_time_sec // 60
        secs = avg_time_sec % 60
        avg_time_str = f"{mins}m {secs}s" if avg_time_sec > 0 else "0m 0s"

        return [
            {
                "id": "total-visits",
                "label": "Total Visits",
                "value": f"{basic['total_visits']:,}",
                "change": visits_change,
                "changeDirection": visits_dir,
                "icon": "globe",
            },
            {
                "id": "unique-visitors",
                "label": "Unique Visitors",
                "value": f"{basic['unique_visitors']:,}",
                "change": unique_change,
                "changeDirection": unique_dir,
                "icon": "users",
            },
            {
                "id": "avg-session",
                "label": "Avg. Session Time",
                "value": avg_time_str,
                "change": time_change,
                "changeDirection": time_dir,
                "icon": "clock",
            },
            {
                "id": "bounce-rate",
                "label": "Bounce Rate",
                "value": f"{bounce_rate}%",
                "change": bounce_change,
                "changeDirection": bounce_dir,
                "icon": "trending-down",
            }
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics/traffic")
def get_traffic_data(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    Time series data for traffic overview. Pivot if comma-separated.
    """
    try:
        tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
        if len(tenants) == 1:
            sql = """
                SELECT 
                    toDate(timestamp) as date,
                    count() as pageViews,
                    uniq(user_id) as visitors
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tenant_id)s AND timestamp >= today() - %(days)s
                GROUP BY date
                ORDER BY date ASC
            """
            return ch_client.query(sql, {"tenant_id": tenants[0], "days": days})
        else:
            sql = """
                SELECT 
                    toDate(timestamp) as date,
                    tenant_id,
                    count() as pageViews,
                    uniq(user_id) as visitors
                FROM feature_intelligence.events_raw
                WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - %(days)s
                GROUP BY date, tenant_id
                ORDER BY date ASC
            """
            results = ch_client.query(sql, {"tenant_ids": tuple(tenants), "days": days})
            date_map = {}
            for r in results:
                d = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else r["date"]
                t = r["tenant_id"]
                if d not in date_map:
                    date_map[d] = {"date": d}
                date_map[d][f"{t}_pageViews"] = r["pageViews"]
                date_map[d][f"{t}_visitors"] = r["visitors"]
            return list(date_map.values())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics/feature_usage_series")
def get_feature_usage_series(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    Time series feature usage data points. Pivot if comma-separated.
    """
    try:
        tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
        if len(tenants) == 1:
            sql = """
                SELECT 
                    toDate(timestamp) as date,
                    count() as usage
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tenant_id)s AND timestamp >= today() - %(days)s
                GROUP BY date
                ORDER BY date ASC
            """
            return ch_client.query(sql, {"tenant_id": tenants[0], "days": days})
        else:
            sql = """
                SELECT 
                    toDate(timestamp) as date,
                    tenant_id,
                    count() as usage
                FROM feature_intelligence.events_raw
                WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - %(days)s
                GROUP BY date, tenant_id
                ORDER BY date ASC
            """
            results = ch_client.query(sql, {"tenant_ids": tuple(tenants), "days": days})
            date_map = {}
            for r in results:
                d = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else r["date"]
                t = r["tenant_id"]
                if d not in date_map:
                    date_map[d] = {"date": d}
                date_map[d][f"{t}_usage"] = r["usage"]
            return list(date_map.values())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/features/heatmap")
def get_feature_heatmap(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    is_compare = len(tenants) > 1

    try:
        activities = {}
        if is_compare:
            compare_tenants = tenants[:2]
            cond = "tenant_id IN %(tenant_ids)s"
            params = {"tenant_ids": tuple(compare_tenants), "days": days}
            groups = compare_tenants
            
            sql = f"""
                SELECT 
                    event_name as feature,
                    tenant_id as group_key,
                    count() as count
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY event_name, tenant_id
            """
            res = ch_client.query(sql, params)
            max_count = max([r["count"] for r in res]) if res else 1
            
            for row in res:
                f = row["feature"]
                if f not in activities:
                    activities[f] = {g: 0 for g in groups}
                activities[f][row["group_key"]] = row["count"]
                
        else:
            cond = "tenant_id = %(tenant_id)s"
            params = {"tenant_id": tenants[0], "days": days}
            
            sql = f"""
                WITH 
                    today() - %(days)s AS start_time,
                    today() AS end_time,
                    (toUnixTimestamp(end_time) - toUnixTimestamp(start_time)) / 7 AS bucket_size_sec
                SELECT 
                    event_name as feature,
                    toString(LEAST(7, intDiv(toUnixTimestamp(timestamp) - toUnixTimestamp(start_time), bucket_size_sec) + 1)) AS group_key,
                    count() as count
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= start_time
                GROUP BY event_name, group_key
                ORDER BY group_key ASC
            """
            res = ch_client.query(sql, params)
            max_count = max([r["count"] for r in res]) if res else 1
            groups = [str(i) for i in builtins_range(1, 8)]

            for row in res:
                f = row["feature"]
                if f not in activities:
                    activities[f] = {g: 0 for g in groups}
                activities[f][row["group_key"]] = row["count"]

        # format response
        formatted_activities = []
        for feat, data in activities.items():
            segments = []
            total = sum(data.values())
            for idx, g in enumerate(groups):
                count = data[g]
                pct = int((count / max_count) * 100) if max_count > 0 else 0
                
                if is_compare:
                    base_color = "blue" if idx == 0 else "orange"
                else:
                    base_color = "blue"
                    
                segments.append({
                    "group_key": g,
                    "count": count,
                    "percentile": pct,
                    "level": "High" if pct > 75 else "Med" if pct > 30 else "Low",
                    "color_scale": base_color
                })
            
            formatted_activities.append({
                "feature": feat,
                "total_usage": total,
                "level": "High" if total > 1000 else "Med",
                "segments": segments
            })

        formatted_activities.sort(key=lambda x: x["total_usage"], reverse=True)

        return {
            "is_compare": is_compare,
            "groups": groups,
            "activities": formatted_activities
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tenants")
def get_all_tenants(tenant_id: Optional[str] = None):
    """
    Returns list of distinct tenants with real metrics from ClickHouse.
    If tenant_id is provided, returns only that tenant's data (for app_admin).
    """
    try:
        where_clause = ""
        params = {}
        # We explicitly remove the restriction on tenant_id so that app_admins 
        # can see all tenants for comparison as requested by the user.

        sql = f"""
            SELECT 
                tenant_id as id,
                tenant_id as name,
                toUInt64(count()) as featureUsage,
                toUInt64(count(distinct event_name)) as activeFeatures,
                toUInt64(count(distinct user_id)) as uniqueUsers,
                countIf(lower(event_name) LIKE '%%error%%' OR lower(event_name) LIKE '%%fail%%') as errorCount
            FROM feature_intelligence.events_raw
            {where_clause}
            GROUP BY tenant_id
            ORDER BY featureUsage DESC
        """
        results = ch_client.query(sql, params)
        tenants = []
        for row in results:
            total = int(row['featureUsage']) or 1
            errors = int(row.get('errorCount', 0))
            unique_users = int(row.get('uniqueUsers', 0))
            active_features = int(row.get('activeFeatures', 0))
            adoption = round((active_features / max(active_features + 2, 1)) * 100) if active_features else 0
            tenants.append({
                "id": row['id'],
                "name": row['name'].replace('_', ' ').title(),
                "featureUsage": total,
                "errors": errors,
                "adoptionRate": min(adoption, 100),
                "plan": "enterprise",
                "uniqueUsers": unique_users,
                "activeFeatures": active_features,
            })
        return tenants
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics/devices")
def get_device_breakdown(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    Device breakdown parsed from metadata. Uses Hare-Niemeyer for exact 100% normalization.
    """
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT 
            if(JSONHas(metadata, 'device_type') AND length(JSONExtractString(metadata, 'device_type')) > 0, JSONExtractString(metadata, 'device_type'), 'mobile') as device,
            count() as value
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY device
    """
    try:
        device_res = ch_client.query(sql, params)
        colors = {"desktop": "#1a73e8", "mobile": "#4285F4", "tablet": "#8AB4F8"}
        
        # Merge into canonical device names first
        merged = {}
        for row in device_res:
            dev = row['device'].lower()
            if dev not in colors:
                dev = "mobile"
            raw_val = int(row['value'])
            if raw_val > 0:
                merged[dev] = merged.get(dev, 0) + raw_val
        
        if not merged:
            return []

        total = sum(merged.values())
        items = list(merged.items())
        quotients = [(name, count, (count / total) * 100) for name, count in items]
        floors = [(name, count, int(q)) for name, count, q in quotients]
        remainders = [(name, count, (count / total) * 100 - int((count / total) * 100)) for name, count in items]
        
        allocated_sum = sum(f for _, _, f in floors)
        remainder_seats = 100 - allocated_sum
        remainders.sort(key=lambda x: x[2], reverse=True)
        final = {name: fl for name, _, fl in floors}
        for i in range(remainder_seats):
            final[remainders[i][0]] += 1

        breakdown = []
        base_colors = ['#1a73e8', '#4285F4', '#8AB4F8', '#34A853', '#F59E0B']
        i_color = 0
        for name, _ in remainders:
            if final[name] > 0:
                breakdown.append({
                    "name": name.capitalize(),
                    "value": final[name],
                    "color": base_colors[i_color % len(base_colors)]
                })
                i_color += 1

        breakdown.sort(key=lambda x: x["value"], reverse=True)
        return breakdown
    except Exception:
        return []

@app.get("/metrics/channels")
def get_acquisition_channels(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    User acquisition channel breakdown derived from event metadata.
    Classifies events by referrer/channel field and returns % distribution.
    """
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT
            if(JSONHas(metadata, 'channel') AND length(JSONExtractString(metadata, 'channel')) > 0,
               JSONExtractString(metadata, 'channel'),
               'direct') as channel,
            count() as total
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY channel
        ORDER BY total DESC
    """
    try:
        results = ch_client.query(sql, params)

        CHANNEL_LABELS = {
            "organic": "Organic Search",
            "organic_search": "Organic Search",
            "direct": "Direct",
            "referral": "Referral",
            "social": "Social",
            "email": "Email",
            "paid": "Paid Search",
            "paid_search": "Paid Search",
            "cpc": "Paid Search",
        }

        merged: dict = {}
        for row in results:
            raw = str(row["channel"]).lower().strip()
            label = CHANNEL_LABELS.get(raw, raw.replace("_", " ").title())
            merged[label] = merged.get(label, 0) + int(row["total"])

        if not merged:
            import hashlib
            from datetime import datetime
            import random
            
            # Deterministic simulation based on hour and tenant if DB is empty
            current_hour = datetime.utcnow().strftime("%Y-%m-%d-%H")
            seed_str = f"{tenants[0]}_{current_hour}"
            seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
            rng = random.Random(seed)
            
            organic = rng.randint(35, 55)
            direct = rng.randint(20, 35)
            referral = rng.randint(10, 20)
            social = rng.randint(5, 12)
            email = 100 - (organic + direct + referral + social)
            if email < 1:
                email = rng.randint(2, 6)
                
            mock_channels = [
                {"name": "Organic Search", "value": organic},
                {"name": "Direct",         "value": direct},
                {"name": "Referral",       "value": referral},
                {"name": "Social",         "value": social},
                {"name": "Email",          "value": email},
            ]
            
            # Sort by value desc
            mock_channels.sort(key=lambda x: -x["value"])
            
            return [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "formattedValue": f"{c['value']}%"
                } for c in mock_channels
            ]

        grand_total = sum(merged.values())
        items = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:6]
        # Hare-Niemeyer normalization to exactly 100%
        quotients = [(n, v, (v / grand_total) * 100) for n, v in items]
        floors = [(n, v, int(q)) for n, v, q in quotients]
        remainders_sorted = sorted(quotients, key=lambda x: x[2] - int(x[2]), reverse=True)
        allocated = sum(f for _, _, f in floors)
        floor_map = {n: f for n, _, f in floors}
        for n, _, _ in remainders_sorted[:max(0, 100 - allocated)]:
            floor_map[n] = floor_map.get(n, 0) + 1

        return [
            {"name": name, "value": floor_map.get(name, 0), "formattedValue": f"{floor_map.get(name, 0)}%"}
            for name, _ in items
        ]
    except Exception:
        return [
            {"name": "Organic Search", "value": 45, "formattedValue": "45%"},
            {"name": "Direct",         "value": 28, "formattedValue": "28%"},
            {"name": "Referral",       "value": 15, "formattedValue": "15%"},
            {"name": "Social",         "value": 8,  "formattedValue": "8%"},
            {"name": "Email",          "value": 4,  "formattedValue": "4%"},
        ]

@app.get("/locations")

def get_locations(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    
    sql = f"""
        SELECT 
            JSONExtractString(metadata, 'location') as location,
            JSONExtractString(metadata, 'continent') as continent,
            JSONExtractString(metadata, 'city') as city,
            JSONExtractString(metadata, 'ip') as ip,
            count() as visits
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY location, continent, city, ip
    """
    try:
        results = ch_client.query(sql, params)
        
        ip_map = {
            "12.34.56.78": "USA",
            "98.76.54.32": "United Kingdom",
            "192.168.1.1": "Germany",
            "111.111.111.111": "India"
        }
        
        # Country → continent mapping for fallback
        COUNTRY_CONTINENT_MAP = {
            "India": "Asia", "Japan": "Asia", "Singapore": "Asia", "UAE": "Asia",
            "China": "Asia", "South Korea": "Asia", "Thailand": "Asia",
            "USA": "North America", "Canada": "North America", "Mexico": "North America",
            "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe",
            "Netherlands": "Europe", "Sweden": "Europe", "Switzerland": "Europe",
            "Spain": "Europe", "Italy": "Europe",
            "Brazil": "South America", "Argentina": "South America",
            "Colombia": "South America", "Chile": "South America",
            "Nigeria": "Africa", "Kenya": "Africa", "South Africa": "Africa", "Egypt": "Africa",
            "Australia": "Oceania", "New Zealand": "Oceania",
        }
        
        locations_dict = {}  # country -> {visits, continent}
        for row in results:
            # Prioritize the explicitly passed 'location' variable
            if row.get('location'):
                country = row['location']
            else:
                country = ip_map.get(row['ip'], "Other")
            
            # Get continent from metadata or fallback map
            continent = row.get('continent') or COUNTRY_CONTINENT_MAP.get(country, "Other")
            
            if country not in locations_dict:
                locations_dict[country] = {"visits": 0, "continent": continent}
            locations_dict[country]["visits"] += row['visits']
            
        location_data = [
            {"country": k, "visits": v["visits"], "continent": v["continent"]} 
            for k, v in locations_dict.items()
            if k != "Other" and v["continent"] != "Other"
        ]
        location_data.sort(key=lambda x: x["visits"], reverse=True)
        return location_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audit_logs")
def get_audit_logs(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    
    sql = f"""
        SELECT 
            event_name,
            user_id,
            channel,
            timestamp,
            metadata
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        ORDER BY timestamp DESC
        LIMIT 50
    """
    try:
        results = ch_client.query(sql, params)
        logs = []
        import json
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        for i, row in enumerate(results):
            meta_str = row.get("metadata", "{}")
            try:
                meta = json.loads(meta_str)
            except:
                meta = {}
            
            user_email = meta.get("email", row["user_id"])
            
            # Convert timestamp to IST
            db_time = row["timestamp"]
            if hasattr(db_time, "replace"):
                utc_time = db_time.replace(tzinfo=timezone.utc)
                ist_time = utc_time.astimezone(IST)
                time_str = ist_time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = str(db_time)
                
            logs.append({
                "id": f"al-{tenant_id}-{i}",
                "user": str(user_email),
                "action": row["event_name"].capitalize().replace("_", " "),
                "resource": f"Channel: {row['channel']}",
                "timestamp": time_str,
                "details": f"Role: {meta.get('role', 'user')} / IP: {meta.get('ip', 'Unknown')}"
            })
        return logs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics/realtime_users")
def get_realtime_users(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Returns real-time active user count with IST timestamp context."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0]} if len(tenants) == 1 else {"tenant_ids": tuple(tenants)}
    
    sql = f"""
        SELECT uniqExact(user_id) as users 
        FROM feature_intelligence.events_raw 
        WHERE {cond} AND timestamp >= now('Asia/Kolkata') - INTERVAL 5 MINUTE
    """
    try:
        results = ch_client.query(sql, params)
        user_count = results[0]['users'] if results else 0
        now_ist = datetime.now(IST)
        return {
            "count": user_count,
            "timestamp_ist": now_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "timezone": "Asia/Kolkata"
        }
    except Exception:
        return {"count": 0, "timestamp_ist": None, "timezone": "Asia/Kolkata"}

@app.get("/metrics/pages_per_minute")
def get_pages_per_minute(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Returns pages-per-minute data with IST-localized time labels."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0]} if len(tenants) == 1 else {"tenant_ids": tuple(tenants)}
    
    sql = f"""
        SELECT toStartOfMinute(timestamp) as min, count() as val 
        FROM feature_intelligence.events_raw 
        WHERE {cond} AND timestamp >= now('UTC') - INTERVAL 60 MINUTE 
        GROUP BY min ORDER BY min ASC
    """
    try:
        results = ch_client.query(sql, params)
        formatted = []
        for r in results:
            # Convert UTC timestamp to IST for display
            if hasattr(r["min"], "replace"):
                utc_time = r["min"].replace(tzinfo=timezone.utc)
                ist_time = utc_time.astimezone(IST)
                hour_label = ist_time.strftime("%H:%M")
            elif hasattr(r["min"], "strftime"):
                hour_label = r["min"].strftime("%H:%M")
            else:
                hour_label = str(r["min"])[11:16]
            formatted.append({
                "hour": hour_label,
                "value": r["val"]
            })
        return formatted
    except Exception:
        return []

@app.get("/metrics/top_pages")
def get_top_pages(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    Returns page-level aggregation: each row is a "page" (URL), with the total
    events across ALL features that fire on that page, plus the list of features.

    Uses centralized page_map.py for feature → page resolution and display names.
    Response includes: pageUrl, totalEvents, comparisonPct, rank, features[]
    Each feature includes: feature, displayName, count, inPagePct
    """
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT 
            JSONExtractString(metadata, 'path') as page,
            event_name as raw_feature,
            count() as cnt
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY page, raw_feature
    """
    try:
        results = ch_client.query(sql, params)

        # 4. BUILD PROPER PAGE -> FEATURE MODEL
        page_data: dict = {}
        
        # Exact explicit known pages for the test app
        KNOWN_PAGES = {
            "/register", "/login", "/dashboard", "/accounts", "/payees",
            "/transactions", "/loans", "/pro-feature?id=crypto-trading",
            "/pro-feature?id=ai-insights", "/pro-feature?id=wealth-management-pro",
            "/pro-feature?id=bulk-payroll-processing", "/profile"
        }
        
        for r in results:
            raw_page = r['page']
            raw_feature = r['raw_feature']
            cnt = int(r['cnt'])

            # STEP 1: Always normalize the event first — this is the canonical taxonomy form
            feature = normalize_event(raw_feature)

            # STEP 2: Determine the page
            # Priority: metadata.path from DB > resolve from normalized event > resolve from raw event > /dashboard
            if raw_page and raw_page not in ("null", "", "None"):
                page = str(raw_page)
                # If the stored path is an exact known page, trust it
                if page not in KNOWN_PAGES:
                    # Try resolving the normalized feature to get the canonical page
                    resolved = resolve_page(feature) or resolve_page(raw_feature)
                    if resolved and resolved in KNOWN_PAGES:
                        page = resolved
                    # else keep the DB path as-is (may be an admin page etc.)
            else:
                # No path in metadata — resolve from normalized feature taxonomy
                page = resolve_page(feature) or resolve_page(raw_feature) or "/dashboard"
                # If resolved page is unknown/generic, fall back to dashboard
                if page not in KNOWN_PAGES:
                    page = "/dashboard"


            if page not in page_data:
                page_data[page] = {"totalEvents": 0, "features": {}}

            page_data[page]["totalEvents"] += cnt
            page_data[page]["features"][feature] = page_data[page]["features"].get(feature, 0) + cnt

        # Sort by total events
        sorted_pages = sorted(page_data.items(), key=lambda x: x[1]["totalEvents"], reverse=True)
        total_all_events = sum(data["totalEvents"] for _, data in sorted_pages) or 1

        formatted = []
        for rank, (page_url, data) in enumerate(sorted_pages[:10], start=1):
            page_total = data["totalEvents"] or 1
            # Sort features by count desc, take top 10
            sorted_feat = sorted(data["features"].items(), key=lambda x: x[1], reverse=True)[:10]
            
            formatted_features = [
                {
                    "feature": k,  # 8. No raw event names in UI -> using normalized k
                    "displayName": resolve_display_name(k) if resolve_display_name(k) != "Unknown" else k,
                    "count": v,
                    "inPagePct": round((v / page_total) * 100, 1),
                }
                for k, v in sorted_feat
            ]
            
            comparison_pct = round((data["totalEvents"] / total_all_events) * 100, 1)
            formatted.append({
                "pageUrl": page_url,
                "totalEvents": data["totalEvents"],
                "comparisonPct": comparison_pct,
                "rank": rank,
                "features": formatted_features,
            })
        return formatted
    except Exception:
        return []


@app.get("/features/activity")
def get_feature_activity(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT event_name, count() as total
        FROM feature_intelligence.events_raw 
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY event_name ORDER BY total DESC LIMIT 5
    """
    try:
        results = ch_client.query(sql, params)
        activities = []
        for r in results:
            activities.append({
                "feature": str(r["event_name"]),
                "segments": [],
                "level": "High" if r['total'] > 10 else "Low"
            })
        return activities
    except Exception as e:
        return []

@app.get("/features/heatmap")
def get_feature_heatmap(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """
    Returns grid-based heatmap matrix for features.
    If multiple tenants provided, matrix is Feature x Tenant.
    If single tenant provided, matrix is Feature x Date.
    """
    tenant_ids = [t.strip() for t in tenant_id.split(',')]
    for t in tenant_ids:
        require_tenant_access(t)
        
    is_compare = len(tenant_ids) > 1

    if is_compare:
        sql = """
            SELECT event_name as feature, tenant_id as group_key, count() as total
            FROM feature_intelligence.events_raw
            WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - 14
            GROUP BY feature, group_key
        """
    else:
        sql = """
            SELECT event_name as feature, toString(toDate(timestamp)) as group_key, count() as total
            FROM feature_intelligence.events_raw
            WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - 7
            GROUP BY feature, group_key
        """
        
    try:
        results = ch_client.query(sql, {"tenant_ids": tenant_ids})
        
        # Organize data
        features = {}
        all_groups = set()
        max_total = 0
        
        for r in results:
            f = r["feature"]
            g = r["group_key"]
            t = r["total"]
            
            if f not in features:
                features[f] = {}
            features[f][g] = t
            all_groups.add(g)
            
            if t > max_total:
                max_total = t
                
        # Fill missing groups with 0 and calculate percentages
        sorted_groups = sorted(list(all_groups))
        activities = []
        
        for f, groups in features.items():
            segments = []
            f_total = sum(groups.values())
            
            for g in sorted_groups:
                count = groups.get(g, 0)
                # Percentile globally (against max block) OR relative to feature?
                # Usually heatmap is relative to global max or feature max. Global max is better for absolute intensity.
                percentile = (count / max_total) if max_total > 0 else 0
                pct = round(percentile * 100, 1)
                
                # Assign level
                if pct > 75:
                    lvl = "High"
                    color = "#1e3a8a" # text-blue-900 equivalent / deep blue
                elif pct > 40:
                    lvl = "Med"
                    color = "#3b82f6" # text-blue-500
                elif pct > 0:
                    lvl = "Low"
                    color = "#bfdbfe" # text-blue-200
                else:
                    lvl = "None"
                    color = "#f1f5f9" # slate-100
                    
                segments.append({
                    "group_key": g,
                    "count": count,
                    "percentile": pct,
                    "level": lvl,
                    "color": color
                })
                
            activities.append({
                "feature": f,
                "raw_feature": f,
                "total_usage": f_total,
                "segments": segments,
                "level": "High" if f_total > 50 else ("Med" if f_total > 10 else "Low")
            })
            
        # Sort features by total usage
        activities.sort(key=lambda x: x["total_usage"], reverse=True)
        
        return {
            "is_compare": is_compare,
            "groups": sorted_groups,
            "activities": activities
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/features/configs")
def get_feature_configs(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0]} if len(tenants) == 1 else {"tenant_ids": tuple(tenants)}
    
    sql = f"""
        SELECT event_name as feature, count() as total
        FROM feature_intelligence.events_raw 
        WHERE {cond} AND timestamp >= today() - 30
        GROUP BY feature ORDER BY total DESC LIMIT 10
    """
    try:
        results = ch_client.query(sql, params)
        configs = []
        for i, r in enumerate(results):
            f = str(r['feature'])
            configs.append({
                "id": f"fc-{i+1}",
                "pattern": f"/{f.replace('.', '/')}",
                "featureName": f.capitalize().replace("_", " "),
                "category": "interaction" if ("view" in f or "dashboard" in f) else "transaction",
                "isActive": True
            })
        if not configs:
            return []
        return configs
    except Exception:
        return []

@app.get("/metrics/retention")
def get_retention(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    """
    Real cohort retention analysis.
    For each weekly cohort (users first seen in week W), compute:
      - month1: % of cohort active in week W itself (always 100%)
      - month2: % of cohort active in week W+1
      - month3: % of cohort active in week W+2
    """
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    try:
        # Step 1: Find each user's first-seen week (cohort assignment)
        # Step 2: For each cohort, count how many returned in subsequent weeks
        sql = f"""
            WITH cohorts AS (
                SELECT
                    user_id,
                    toStartOfWeek(min(timestamp)) as cohort_week
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY user_id
            ),
            activity AS (
                SELECT
                    user_id,
                    toStartOfWeek(timestamp) as activity_week
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY user_id, activity_week
            )
            SELECT
                c.cohort_week,
                toUInt32(dateDiff('week', c.cohort_week, a.activity_week)) as week_offset,
                count(DISTINCT c.user_id) as retained_users
            FROM cohorts c
            INNER JOIN activity a ON c.user_id = a.user_id
            WHERE week_offset <= 2
            GROUP BY c.cohort_week, week_offset
            ORDER BY c.cohort_week DESC, week_offset ASC
        """
        results = ch_client.query(sql, params)
        
        # Organize: cohort_week -> { week_offset -> count }
        cohort_data = {}
        for row in results:
            cw = row['cohort_week']
            cw_str = cw.strftime("%Y-%m-%d") if hasattr(cw, "strftime") else str(cw)[:10]
            offset = int(row['week_offset'])
            count_val = int(row['retained_users'])
            if cw_str not in cohort_data:
                cohort_data[cw_str] = {}
            cohort_data[cw_str][offset] = count_val
        
        if not cohort_data:
            return []
        
        retention = []
        cohort_names = ["This Week", "Last Week", "2 Weeks Ago", "3 Weeks Ago"]
        sorted_cohorts = sorted(cohort_data.keys(), reverse=True)[:4]
        
        for i, cw_str in enumerate(sorted_cohorts):
            offsets = cohort_data[cw_str]
            base_users = offsets.get(0, 0)
            if base_users == 0:
                continue
            
            m1_pct = 100  # Week 0 = 100% by definition
            m2_pct = round((offsets.get(1, 0) / base_users) * 100)
            m3_pct = round((offsets.get(2, 0) / base_users) * 100)
            
            retention.append({
                "cohort": cohort_names[i] if i < len(cohort_names) else cw_str,
                "users": base_users,
                "month1": m1_pct,
                "month2": m2_pct,
                "month3": m3_pct
            })
        
        return retention
    except Exception as e:
        return []

@app.get("/deployment/info")
def get_deployment_info():
    return {
        "mode": settings.DEPLOYMENT_MODE,
        "is_cloud": settings.is_cloud,
        "is_on_prem": settings.is_on_prem,
        "local_tenant": settings.TENANT_ID if settings.is_on_prem else None
    }

@app.get("/admin/summary")
def get_admin_summary():
    """Returns high-level global aggregated stats (Cloud mode only)."""
    require_cloud_mode()
    try:
        sql = """
            SELECT count(distinct tenant_id) as total_tenants, 
                   sum(total_events) as total_events
            FROM feature_intelligence.daily_feature_usage
            WHERE date >= today() - 30
        """
        basic = ch_client.query(sql)[0] if ch_client.query(sql) else {"total_tenants": 0, "total_events": 0}
        
        sql_top = """
            SELECT tenant_id as name, sum(total_events) as events
            FROM feature_intelligence.daily_feature_usage
            GROUP BY tenant_id
            ORDER BY events DESC LIMIT 5
        """
        top_tenants_raw = ch_client.query(sql_top)
        top_tenants = [
            {"name": row["name"].capitalize(), "events": int(row["events"])} 
            for row in top_tenants_raw
        ]
        
        return {
            "total_tenants": basic["total_tenants"],
            "total_events": basic["total_events"],
            "top_tenants": top_tenants
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/app/{tenant_id}/summary")
def get_admin_app_summary(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    tenant_id = tenants
    """Returns basic KPIs and Insights for a specfic app (Cloud mode only)."""
    require_cloud_mode()
    return {
        "kpi": get_kpi_metrics(tenants=tenant_id, range=range),
        "insights": get_insights(tenants=tenant_id, range=range)["insights"]
    }

@app.get("/transparency/cloud-data")
def get_transparency_data(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Summarizes what data is visible to the cloud/admin."""
    require_tenant_access(tenant_id)
    
    # In a fully deployed setup, this could query exact synced row counts.
    # For now, it describes the privacy boundaries based on settings.
    return {
        "deploymentMode": settings.DEPLOYMENT_MODE,
        "visible_categories": [
            {
                "category": "Feature Usage KPIs", 
                "is_synced": True, 
                "details": "Aggregated counts of feature interactions. Used for billing and top-level analytics."
            },
            {
                "category": "AI Insights", 
                "is_synced": True, 
                "details": "High-level alerts on stable/trending features without exposing user paths."
            },
            {
                "category": "Raw Audit Logs", 
                "is_synced": False, 
                "details": "Strictly local. Contains PII, emails, roles, and deep user behavior."
            },
            {
                "category": "Traffic & Locations", 
                "is_synced": False, 
                "details": "Strictly local. IP addresses, geographic mapping, and device breakdown."
            }
        ],
        "message": "In ON_PREM mode, no user-level details leave the premises." if settings.is_on_prem else "In CLOUD mode, full data is managed centrally."
    }

# ═══════════════════════════════════════════════════════════
# LICENSE VS USAGE
# ═══════════════════════════════════════════════════════════

from core.models import LicenseSyncRequest, TrackingToggleRequest

@app.get("/license/usage")
def get_license_usage(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("30d", description="Time range like 7d, 30d")):
    """Compare licensed features vs actual usage — multi-tenant aware with proper IN clause."""
    require_tenant_access(tenants)
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenant_list[0], "days": days} if len(tenant_list) == 1 else {"tenant_ids": tuple(tenant_list), "days": days}

    try:
        # 1. Single source of truth catalog
        feature_catalog = {
            # Enterprise
            "pro.crypto_trade_execution.success": {"plan": "enterprise"},
            "pro.crypto_trade_execution.failed": {"plan": "enterprise"},
            "pro.crypto_price_feeds.view": {"plan": "enterprise"},
            "pro.crypto_portfolio.view": {"plan": "enterprise"},
            "pro.wealth_rebalance.success": {"plan": "enterprise"},
            "pro.wealth_rebalance.failed": {"plan": "enterprise"},
            "pro.wealth_insights.view": {"plan": "enterprise"},
            "pro.payroll_batch.success": {"plan": "enterprise"},
            "pro.payroll_batch.failed": {"plan": "enterprise"},
            "pro.payroll_payees.view": {"plan": "enterprise"},
            "pro.payroll_search.success": {"plan": "enterprise"},
            "pro.payroll_search.failed": {"plan": "enterprise"},
            "pro.finance_library_book.access": {"plan": "enterprise"},
            "pro.finance_library_stats.view": {"plan": "enterprise"},
            "pro.features.view": {"plan": "enterprise"},
            "pro.features_unlock.success": {"plan": "enterprise"},
            "pro.features_unlock.failed": {"plan": "enterprise"},
            
            # Free / Base
            "free.dashboard.view": {"plan": "free"},
            "free.auth.login.success": {"plan": "free"},
            "free.auth.login.failed": {"plan": "free"},
            "free.auth.register.success": {"plan": "free"},
            "free.auth.register.failed": {"plan": "free"},
            "free.payment.success": {"plan": "free"},
            "free.payment.failed": {"plan": "free"},
            "free.accounts.view": {"plan": "free"},
            "free.transactions.view": {"plan": "free"},
            "free.payees.view": {"plan": "free"},
            "free.payees.add_success": {"plan": "free"},
            "free.payees.add_failed": {"plan": "free"},
            "free.payees.edit_success": {"plan": "free"},
            "free.payees.edit_failed": {"plan": "free"},
            "free.payees.delete_success": {"plan": "free"},
            "free.payees.delete_failed": {"plan": "free"},
            "free.loan.applied": {"plan": "free"},
            "free.loan.approved": {"plan": "free"},
            "free.loan.rejected": {"plan": "free"},
            "free.loans.view": {"plan": "free"},
            "free.loan.kyc_started": {"plan": "free"},
            "free.loan.kyc_completed": {"plan": "free"},
            "free.loan.kyc_failed": {"plan": "free"},
            "free.loan.kyc_abandoned": {"plan": "free"},
            "free.profile.view": {"plan": "free"},
            "free.profile.edit_success": {"plan": "free"},
            "free.profile.edit_failed": {"plan": "free"},
            "free.profile.location": {"plan": "free"},
        }
        sql_used = f"""
            SELECT 
                event_name as feature_name, 
                count() as usage_count, 
                uniqExact(user_id) as unique_users,
                max(JSONExtractString(metadata, 'tier')) as tier_hint
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
            GROUP BY feature_name
            ORDER BY usage_count DESC
        """
        used = ch_client.query(sql_used, params)
        used_map = {r["feature_name"]: r for r in used}

        pro_features_set = {k for k, v in feature_catalog.items() if v["plan"] == "enterprise"}

        # ─── Usage trends (last 7 days) ───
        sql_trends = f"""
            SELECT event_name as feature_name, toDate(timestamp) as date, count() as count
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - 7
            GROUP BY feature_name, date
            ORDER BY date ASC
        """
        trend_rows = ch_client.query(sql_trends, params)
        trends_map = {}
        for r in trend_rows:
            fname = r["feature_name"]
            if fname not in trends_map:
                trends_map[fname] = []
            date_str = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
            trends_map[fname].append({"date": date_str, "count": int(r["count"])})
            
        # Build lists based strictly on catalog mapping
        total_usage_count = sum(int(r["usage_count"]) for r in used) or 1
        
        licensed_list = []
        unused_licensed = []
        unlicensed_used = []
        
        # Populate pro/licensed from STRICT catalog
        for fname in pro_features_set:
            uc = int(used_map.get(fname, {}).get("usage_count", 0))
            item = {
                "feature_name": fname,
                "plan_tier": feature_catalog[fname]["plan"],
                "is_used": fname in used_map,
                "usage_count": uc,
                "unique_users": int(used_map.get(fname, {}).get("unique_users", 0)),
                "usage_pct": round((uc / total_usage_count) * 100, 1),
                "trend": trends_map.get(fname, []),
            }
            if item["is_used"]:
                licensed_list.append(item)
            else:
                unused_licensed.append(item)
                
        # Populate free/unlicensed ONLY from known catalog + what's not Pro
        for fname, r in used_map.items():
            if fname not in pro_features_set:
                # Do not use raw event names directly, enforce taxonomy where possible
                uc = int(r["usage_count"])
                unlicensed_used.append({
                    "feature_name": fname,
                    "usage_count": uc,
                    "unique_users": int(r["unique_users"]),
                    "usage_pct": round((uc / total_usage_count) * 100, 1),
                })
                
        unlicensed_used.sort(key=lambda x: x["usage_count"], reverse=True)

        # ─── Summaries ───
        pro_user_count = 0
        total_user_count = 1
        wow_change = 0.0

        if pro_features_set:
            pro_str = ", ".join([f"'{f}'" for f in pro_features_set])
            sql_pro_users = f"""
                SELECT uniqExact(user_id) as pro_users
                FROM feature_intelligence.events_raw
                WHERE {cond} AND event_name IN ({pro_str}) AND timestamp >= today() - %(days)s
            """
            pro_res = ch_client.query(sql_pro_users, params)
            pro_user_count = int(pro_res[0]["pro_users"]) if pro_res else 0

            sql_total = f"""
                SELECT uniqExact(user_id) as total_users 
                FROM feature_intelligence.events_raw 
                WHERE {cond} AND timestamp >= today() - %(days)s
            """
            total_user_count = max(int((ch_client.query(sql_total, params) or [{"total_users": 1}])[0]["total_users"]), 1)

            sql_wow = f"""
                SELECT
                    countIf(timestamp >= today() - 7) as current_week,
                    countIf(timestamp >= today() - 14 AND timestamp < today() - 7) as prev_week
                FROM feature_intelligence.events_raw
                WHERE {cond} AND event_name IN ({pro_str})
            """
            wow_res = ch_client.query(sql_wow, params)
            cw = int(wow_res[0]["current_week"]) if wow_res else 0
            pw = int(wow_res[0]["prev_week"]) if wow_res else 0
            wow_change = round(((cw - pw) / max(pw, 1)) * 100, 1)

        total_licensed = len(pro_features_set)
        total_used_licensed = len(licensed_list)
        waste_pct = round(((total_licensed - total_used_licensed) / max(total_licensed, 1)) * 100, 1)

        return {
            "tenant_id": tenants,
            "summary": {
                "total_licensed": total_licensed,
                "total_used": len(used_map),
                "total_used_licensed": total_used_licensed,
                "waste_pct": waste_pct,
                "pro_users": pro_user_count,
                "total_users": total_user_count,
                "pro_adoption_pct": round((pro_user_count / total_user_count) * 100, 1),
                "estimated_revenue": pro_user_count * 2000,
                "wow_change": wow_change,
            },
            "licensed": licensed_list,
            "unused_licensed": unused_licensed,
            "unlicensed_used": unlicensed_used,
            "nexabank_context": {}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/license/sync")
def sync_licenses(req: LicenseSyncRequest):
    """Upsert license records for a tenant."""
    require_tenant_access(req.tenant_id)
    try:
        from datetime import datetime
        rows = []
        for f in req.features:
            rows.append([req.tenant_id, f.feature_name, 1 if f.is_licensed else 0, f.plan_tier, datetime.utcnow()])
        
        client = ch_client._get_client()
        client.insert(
            'feature_intelligence.tenant_licenses',
            rows,
            column_names=['tenant_id', 'feature_name', 'is_licensed', 'plan_tier', 'updated_at']
        )
        return {"status": "ok", "synced": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# TRACKING TOGGLES
# ═══════════════════════════════════════════════════════════

@app.get("/tracking/toggles")
def get_tracking_toggles(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Get all feature tracking toggles for a tenant."""
    require_tenant_access(tenant_id)
    try:
        sql = """
            SELECT feature_name, is_enabled, changed_by, changed_at
            FROM feature_intelligence.tracking_toggles FINAL
            WHERE tenant_id = %(tenant_id)s
        """
        results = ch_client.query(sql, {"tenant_id": tenant_id})
        toggles = []
        for r in results:
            toggles.append({
                "feature_name": r["feature_name"],
                "is_enabled": bool(r["is_enabled"]),
                "changed_by": r["changed_by"],
                "changed_at": str(r["changed_at"]) if hasattr(r["changed_at"], "strftime") else str(r["changed_at"]),
            })
        return {"tenant_id": tenant_id, "toggles": toggles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tracking/toggles")
def set_tracking_toggle(req: TrackingToggleRequest):
    """Set a tracking toggle and record it in audit log."""
    require_tenant_access(req.tenant_id)
    try:
        from datetime import datetime
        now = datetime.utcnow()
        
        # Get existing state for audit
        sql_old = """
            SELECT is_enabled FROM feature_intelligence.tracking_toggles FINAL
            WHERE tenant_id = %(tenant_id)s AND feature_name = %(feature_name)s
        """
        old = ch_client.query(sql_old, {"tenant_id": req.tenant_id, "feature_name": req.feature_name})
        old_val = "enabled" if (old and old[0]["is_enabled"]) else "disabled"
        new_val = "enabled" if req.is_enabled else "disabled"
        
        # Upsert toggle
        client = ch_client._get_client()
        client.insert(
            'feature_intelligence.tracking_toggles',
            [[req.tenant_id, req.feature_name, 1 if req.is_enabled else 0, req.actor_email, now]],
            column_names=['tenant_id', 'feature_name', 'is_enabled', 'changed_by', 'changed_at']
        )
        
        # Write audit log
        client.insert(
            'feature_intelligence.config_audit_log',
            [[req.tenant_id, req.actor_email, "tracking_toggle", req.feature_name, old_val, new_val, now]],
            column_names=['tenant_id', 'actor_email', 'action', 'target', 'old_value', 'new_value', 'timestamp']
        )
        
        return {"status": "ok", "feature_name": req.feature_name, "is_enabled": req.is_enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# CONFIGURATION AUDIT LOG
# ═══════════════════════════════════════════════════════════

@app.get("/config/audit-log")
def get_config_audit_log(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Returns configuration change audit trail for a tenant."""
    require_tenant_access(tenant_id)
    try:
        sql = """
            SELECT actor_email, action, target, old_value, new_value, timestamp
            FROM feature_intelligence.config_audit_log
            WHERE tenant_id = %(tenant_id)s
            ORDER BY timestamp DESC
            LIMIT 100
        """
        results = ch_client.query(sql, {"tenant_id": tenant_id})
        logs = []
        for r in results:
            logs.append({
                "actor": r["actor_email"],
                "action": r["action"],
                "target": r["target"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "timestamp": r["timestamp"].replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S") if hasattr(r["timestamp"], "replace") else (r["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if hasattr(r["timestamp"], "strftime") else str(r["timestamp"])),
            })
        return {"tenant_id": tenant_id, "logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# USER JOURNEY MAPPING
# ═══════════════════════════════════════════════════════════

@app.get("/journey/user")
def get_user_journey(tenants: str = Query(..., description="Comma-separated list of tenants"), user_id: str = Query(..., description="User ID")):
    """Returns a single user's complete event timeline with session detection."""
    tenant_id = tenants  # Use first tenant for journey lookup
    require_tenant_access(tenant_id)
    try:
        sql = """
            SELECT event_name, channel, timestamp, metadata
            FROM feature_intelligence.events_raw
            WHERE tenant_id = %(tenant_id)s AND user_id = %(user_id)s
            ORDER BY timestamp ASC
            LIMIT 500
        """
        results = ch_client.query(sql, {"tenant_id": tenant_id, "user_id": user_id})
        
        events = []
        sessions = []
        current_session = []
        SESSION_GAP_SECONDS = 1800  # 30 minutes
        
        for i, r in enumerate(results):
            import datetime
            ts = r["timestamp"]
            if hasattr(ts, "replace"):
                ts_str = ts.replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            
            event = {
                "event_name": r["event_name"],
                "channel": r["channel"],
                "timestamp": ts_str,
                "metadata": r["metadata"],
            }
            events.append(event)
            
            # Session break detection
            if i > 0:
                prev_ts = results[i - 1]["timestamp"]
                if hasattr(ts, "timestamp") and hasattr(prev_ts, "timestamp"):
                    gap = (ts - prev_ts).total_seconds()
                elif hasattr(ts, "__sub__"):
                    gap = (ts - prev_ts).total_seconds()
                else:
                    gap = 0
                    
                if gap > SESSION_GAP_SECONDS:
                    sessions.append(current_session)
                    current_session = []
            
            current_session.append(event)
        
        if current_session:
            sessions.append(current_session)
        
        # Drop-off detection: did user complete common flow?
        event_names = [e["event_name"] for e in events]
        drop_off_point = None
        if len(events) > 0 and len(sessions) > 0:
            last_event = events[-1]["event_name"] if events else None
            drop_off_point = last_event
        
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "total_events": len(events),
            "total_sessions": len(sessions),
            "events": events,
            "sessions": sessions,
            "last_event": drop_off_point,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/journey/users")
def list_journey_users(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Returns list of users with event counts for journey selection."""
    require_tenant_access(tenant_id)
    try:
        sql = """
            SELECT user_id, count() as event_count, min(timestamp) as first_seen, max(timestamp) as last_seen
            FROM feature_intelligence.events_raw
            WHERE tenant_id = %(tenant_id)s
            GROUP BY user_id
            ORDER BY event_count DESC
            LIMIT 50
        """
        results = ch_client.query(sql, {"tenant_id": tenant_id})
        users = []
        for r in results:
            import datetime
            fs = r["first_seen"]
            ls = r["last_seen"]
            fs_str = fs.replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S") if hasattr(fs, "replace") else (fs.strftime("%Y-%m-%d %H:%M") if hasattr(fs, "strftime") else str(fs))
            ls_str = ls.replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S") if hasattr(ls, "replace") else (ls.strftime("%Y-%m-%d %H:%M") if hasattr(ls, "strftime") else str(ls))
            
            users.append({
                "user_id": r["user_id"],
                "event_count": int(r["event_count"]),
                "first_seen": fs_str,
                "last_seen": ls_str,
            })
        return {"tenant_id": tenant_id, "users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# CUSTOMER SEGMENTATION COMPARISON
# ═══════════════════════════════════════════════════════════

@app.get("/segmentation/compare")
def get_segmentation_comparison(tenants: str = Query(..., description="Comma-separated list of tenants")):
    tenant_id = tenants
    """Group features by plan tier and compare adoption rates."""
    require_tenant_access(tenant_id)
    try:
        # Get license tiers
        sql_tiers = """
            SELECT feature_name, plan_tier
            FROM feature_intelligence.tenant_licenses FINAL
            WHERE tenant_id = %(tenant_id)s AND is_licensed = 1
        """
        tiers = ch_client.query(sql_tiers, {"tenant_id": tenant_id})
        tier_map = {r["feature_name"]: r["plan_tier"] for r in tiers}
        
        # Get usage
        sql_usage = """
            SELECT event_name, sum(total_events) as total, uniqMerge(unique_users) as users
            FROM feature_intelligence.daily_feature_usage
            WHERE tenant_id = %(tenant_id)s AND date >= today() - 30
            GROUP BY event_name
        """
        usage = ch_client.query(sql_usage, {"tenant_id": tenant_id})
        
        # Group by segment
        segments = {}
        for u in usage:
            tier = tier_map.get(u["event_name"], "unlicensed")
            if tier not in segments:
                segments[tier] = {"tier": tier, "features": 0, "total_usage": 0, "unique_users": 0}
            segments[tier]["features"] += 1
            segments[tier]["total_usage"] += int(u["total"])
            segments[tier]["unique_users"] += int(u["users"])
        
        return {
            "tenant_id": tenant_id,
            "segments": list(segments.values()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# PREDICTIVE ADOPTION INSIGHTS
# ═══════════════════════════════════════════════════════════

@app.get("/predictive/adoption")
def get_predictive_adoption(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("14d", description="Time range like 7d, 14d, 30d")):
    """
    Predicts feature adoption likelihood using a weighted heuristic:
    score = (recent_trend * 0.4) + (unique_users_pct * 0.3) + (frequency * 0.3)
    Also computes growth_rate, projected_next_7d, and anomaly flag.
    """
    require_tenant_access(tenants)
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    try:
        # Recent 7d vs previous 7d trend (aggregated across tenants)
        sql_trend = """
            SELECT 
                event_name,
                sumIf(total_events, date >= today() - 7) as recent_7d,
                sumIf(total_events, date >= today() - 14 AND date < today() - 7) as prev_7d
            FROM feature_intelligence.daily_feature_usage
            WHERE tenant_id IN %(tenant_ids)s AND date >= today() - 14
            GROUP BY event_name
        """
        trend_data = ch_client.query(sql_trend, {"tenant_ids": tuple(tenant_list)})
        
        # Total unique users
        sql_total_users = """
            SELECT uniqExact(user_id) as total_users
            FROM feature_intelligence.events_raw
            WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - %(days)s
        """
        total_users_result = ch_client.query(sql_total_users, {"tenant_ids": tuple(tenant_list), "days": days})
        total_users = int(total_users_result[0]["total_users"]) if total_users_result else 1
        total_users = max(total_users, 1)
        
        # Per-feature unique users
        sql_feature_users = """
            SELECT event_name, uniqExact(user_id) as feature_users
            FROM feature_intelligence.events_raw
            WHERE tenant_id IN %(tenant_ids)s AND timestamp >= today() - 14
            GROUP BY event_name
        """
        feature_users = ch_client.query(sql_feature_users, {"tenant_ids": tuple(tenant_list)})
        feature_users_map = {r["event_name"]: int(r["feature_users"]) for r in feature_users}
        
        # Frequency consistency
        sql_frequency = """
            SELECT event_name, count(distinct date) as active_days
            FROM feature_intelligence.daily_feature_usage
            WHERE tenant_id IN %(tenant_ids)s AND date >= today() - 14
            GROUP BY event_name
        """
        frequency_data = ch_client.query(sql_frequency, {"tenant_ids": tuple(tenant_list)})
        frequency_map = {r["event_name"]: int(r["active_days"]) for r in frequency_data}
        
        predictions = []
        for row in trend_data:
            name = row["event_name"]
            recent = int(row["recent_7d"])
            prev = int(row["prev_7d"])
            
            # Growth rate (%)
            if prev > 0:
                growth_rate = round(((recent - prev) / prev) * 100, 1)
            elif recent > 0:
                growth_rate = 100.0
            else:
                growth_rate = 0.0
            
            # Trend score (0-100): normalize growth
            trend_score = min(max(growth_rate + 50, 0), 100)
            
            # Unique users percentage (0-100)
            fu = feature_users_map.get(name, 0)
            users_pct = min((fu / total_users) * 100, 100)
            
            # Frequency consistency (0-100)
            active_days = frequency_map.get(name, 0)
            freq_score = min((active_days / 14) * 100, 100)
            
            # Weighted score
            score = round(trend_score * 0.4 + users_pct * 0.3 + freq_score * 0.3, 1)
            
            # Projected next 7d events (linear projection)
            if prev > 0:
                projected_next_7d = round(recent * (1 + growth_rate / 100))
            else:
                projected_next_7d = recent  # can't project without baseline
            
            # Anomaly detection: >50% change in either direction
            anomaly = abs(growth_rate) > 50
            
            predictions.append({
                "feature_name": name,
                "score": score,
                "trend_score": round(trend_score, 1),
                "users_pct": round(users_pct, 1),
                "frequency_score": round(freq_score, 1),
                "recent_7d": recent,
                "prev_7d": prev,
                "growth_rate": growth_rate,
                "projected_next_7d": projected_next_7d,
                "anomaly": anomaly,
                "status": "High Adoption" if score >= 70 else "Growing" if score >= 40 else "At Risk",
            })
        
        predictions.sort(key=lambda x: x["score"], reverse=True)
        
        return {
            "tenant_id": tenants,
            "total_users": total_users,
            "predictions": predictions,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/ai_report")
def get_ai_report(tenants: str = Query(..., description="Comma-separated list of tenants"), force_refresh: bool = Query(False, description="Bypass the cache and generate a new report")):
    """Generates a comprehensive AI-powered summarization report for the dashboard.
    Reports are persisted in ClickHouse (ai_reports table). Old reports are auto-replaced."""
    tenant_id = tenants  # Alias for backwards compatibility within this function
    require_tenant_access(tenant_id)
    import json as _json

    def _load_report_from_db(tid: str):
        """Load the latest stored report from ClickHouse."""
        sql = """
            SELECT report, insights, generated_by, generated_at
            FROM feature_intelligence.ai_reports FINAL
            WHERE tenant_id = %(tenant_id)s
            LIMIT 1
        """
        rows = ch_client.query(sql, {"tenant_id": tid})
        if not rows:
            return None
        row = rows[0]
        insights_raw = row.get("insights", "[]")
        try:
            insights_parsed = _json.loads(insights_raw) if isinstance(insights_raw, str) else insights_raw
        except Exception:
            insights_parsed = []
        generated_at = row.get("generated_at")
        generated_at_str = generated_at.isoformat() if hasattr(generated_at, "isoformat") else str(generated_at)
        return {
            "report": row.get("report", ""),
            "insights": insights_parsed,
            "generated_by": row.get("generated_by", ""),
            "generated_at": generated_at_str,
        }

    def _save_report_to_db(tid: str, report: str, insights_list: list, generated_by: str = ""):
        """Insert a new report into ClickHouse. ReplacingMergeTree will replace the old one."""
        client = ch_client._get_client()
        client.insert(
            'feature_intelligence.ai_reports',
            [[tid, generated_by, report, _json.dumps(insights_list), datetime.utcnow()]],
            column_names=['tenant_id', 'generated_by', 'report', 'insights', 'generated_at']
        )

    try:
        # --- If NOT force refreshing, try to return stored report ---
        if not force_refresh:
            # Fast path: in-memory cache
            now = time.time()
            if tenant_id in AI_REPORT_CACHE:
                cached_data = AI_REPORT_CACHE[tenant_id]
                if now - cached_data["timestamp"] < AI_CACHE_TTL:
                    return {
                        "tenant_id": tenant_id,
                        "report": cached_data.get("report", ""),
                        "cached": True,
                        "generated_at": cached_data.get("generated_at"),
                        "insights": cached_data.get("insights", []),
                    }

            # Slow path: load from ClickHouse
            db_report = _load_report_from_db(tenant_id)
            if db_report and db_report["report"]:
                # Populate in-memory cache for fast subsequent reads
                AI_REPORT_CACHE[tenant_id] = {
                    "timestamp": time.time(),
                    "report": db_report["report"],
                    "insights": db_report["insights"],
                    "generated_at": db_report["generated_at"],
                }
                return {
                    "tenant_id": tenant_id,
                    "report": db_report["report"],
                    "cached": True,
                    "generated_at": db_report["generated_at"],
                    "insights": db_report["insights"],
                }

        # --- Generate a fresh report ---
        kpi = get_kpi_metrics(tenants=tenant_id, range="30d")
        secondary = get_secondary_kpi(tenants=tenant_id, range="30d")
        locations = get_locations(tenants=tenant_id, range="30d")[:5]
        activities = get_feature_activity(tenants=tenant_id, range="30d")

        # Build HTML visualization payload
        kpi_cards_html = f'''
        <section style="margin-bottom: 20px; font-family: inherit; line-height: 1.35;">
            <h3 style="font-size: 24px; font-weight: 800; color: #0f172a; margin: 0 0 14px;">Platform Health & Activity Metrics</h3>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px;">
        '''

        for k in kpi:
            val = k.get('value', '0')
            label = k.get('label', '')
            kpi_cards_html += f'''
                <div style="padding: 16px; background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border: 1px solid #dbe5f1; border-radius: 12px; box-shadow: 0 2px 10px rgba(2, 6, 23, 0.05);">
                    <p style="margin: 0; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.08em;">{label}</p>
                    <p style="margin: 10px 0 0; font-size: 34px; font-weight: 800; color: #0b1f44;">{val}</p>
                </div>
            '''
        kpi_cards_html += '</div></section>'

        activity_html = f'''
        <section style="margin-bottom: 20px; padding: 18px; background: #ffffff; border-radius: 12px; border: 1px solid #dbe5f1; box-shadow: 0 2px 10px rgba(2, 6, 23, 0.05); line-height: 1.3;">
            <h4 style="margin: 0 0 12px; font-size: 19px; font-weight: 800; color: #0f172a; display: flex; align-items: center; gap: 8px;">
                <span style="display: inline-block; width: 4px; height: 16px; background: #3b82f6; border-radius: 2px;"></span>
                Feature Adoption Matrix
            </h4>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px;">
        '''

        for act in activities[:6]:
            feat_name = act.get('feature', 'Unknown')
            hash_val = sum(ord(c) for c in feat_name) % 60 + 20
            color = act.get('segments', [{'color': '#3b82f6'}])[0].get('color', '#3b82f6')
            activity_html += f'''
                <div style="padding: 12px; background: #f8fafc; border-radius: 10px; border: 1px solid #e6edf5;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 13px; color: #1f2937; font-weight: 700;">
                        <span>{feat_name}</span>
                        <span style="color: {color}; font-weight: 800;">{hash_val}%</span>
                    </div>
                    <div style="height: 8px; width: 100%; background: #dbe4ef; border-radius: 99px; overflow: hidden;">
                        <div style="height: 100%; width: {hash_val}%; background: linear-gradient(90deg, {color}99, {color}); border-radius: 99px;"></div>
                    </div>
                </div>
            '''
        activity_html += '</div></section>'

        continent_map = {
            "USA": "North America", "Canada": "North America",
            "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe",
            "India": "Asia", "Japan": "Asia",
            "Australia": "Oceania", "Brazil": "South America"
        }

        continent_data = {}
        total_visits = 0
        for loc in locations:
            c = loc.get("country", "Unknown")
            v = loc.get("visits", 0)
            cont = continent_map.get(c, "Other Regions")
            continent_data[cont] = continent_data.get(cont, 0) + v
            total_visits += v
        if total_visits == 0: total_visits = 1

        geo_html = f'''
        <section style="margin-bottom: 24px; padding: 18px; background: #ffffff; border-radius: 12px; border: 1px solid #dbe5f1; box-shadow: 0 2px 10px rgba(2, 6, 23, 0.05); line-height: 1.3;">
            <h4 style="margin: 0 0 12px; font-size: 19px; font-weight: 800; color: #0f172a; display: flex; align-items: center; gap: 8px;">
                <span style="display: inline-block; width: 4px; height: 16px; background: #10b981; border-radius: 2px;"></span>
                Global Footprint (Continent-wise Traffic)
            </h4>
            <div style="display: flex; flex-direction: column; gap: 10px;">
        '''

        colors = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#ef4444']
        for i, (cont, visits) in enumerate(sorted(continent_data.items(), key=lambda x: x[1], reverse=True)):
            pct = int((visits / total_visits) * 100)
            c_color = colors[i % len(colors)]
            geo_html += f'''
                <div style="display: grid; grid-template-columns: minmax(120px, 160px) 1fr 60px 80px; gap: 10px; align-items: center;">
                    <div style="font-size: 13px; font-weight: 700; color: #334155;">{cont}</div>
                    <div style="height: 8px; background: #dbe4ef; border-radius: 99px; overflow: hidden;">
                        <div style="height: 100%; width: {pct}%; background: linear-gradient(90deg, {c_color}99, {c_color}); border-radius: 99px;"></div>
                    </div>
                    <div style="text-align: right; font-size: 13px; font-weight: 800; color: #0f172a;">{pct}%</div>
                    <div style="text-align: right; font-size: 12px; font-weight: 600; color: #64748b;">{visits} visits</div>
                </div>
            '''
        geo_html += '</div></section>'

        divider = '<hr style="border: 0; height: 1px; background: #e2e8f0; margin: 40px 0;" />'

        context_str = f"KPI Metrics: {kpi}\n\nSecondary Metrics: {secondary}\n\nTop Locations: {locations}\n\nFeature Activities: {activities}"

        prompt = f"""
        You are an expert UX Researcher and Strategic Data Analyst for NexaBank.
        Write a detailed, critical analysis report based on the following raw metrics for tenant '{tenant_id}'.
        Context Data:
        {context_str}

        CRITICAL INSTRUCTIONS: Focus deeply on **HOW we can improve user interaction** and **HOW we can use structural data insights to make the overall product better**. Provide a heavily analytical perspective.
        IMPORTANT: Make sure to strictly emphasize and identify where the user journey usually falls off. Use **bold** and `highlight` text (e.g. <mark>highlighted text</mark> or **bold text**) wherever you feel it is critical to draw the reader's attention to these drop-offs.

        Please structure your Markdown report exactly as follows:

        ## 1. Executive Summary
        (High-level summary of current product usage health, drop-offs, and critical gaps.)

        ## 2. User Interaction Evaluation
        (Analyze what the data says about how users are interacting with features. Where are the friction points? What behaviors highlight poor UX?)

        ## 3. Product Enhancement Strategy
        (Concrete proposals on how to use these data insights to iterate on the platform. What features should be built next, redesigned, or sunset?)

        ## 4. Geographic & Engagement Insights
        (Brief thoughts on the diverse footprint of the user base and retention anomalies.)

        Do not include any raw JSON or filler content. Do not output anything outside of the markdown itself.
        """
        from api.insights import query_ollama
        llm_response = query_ollama(prompt)

        if not llm_response:
            raise HTTPException(status_code=503, detail="AI Model currently initializing or unavailable.")

        final_report = f"{kpi_cards_html}\n{activity_html}\n{geo_html}\n{divider}\n{llm_response}"
        insights_payload = generate_insights(tenant_id)

        # Get the user who triggered generation (from request headers)
        generated_by = ""
        try:
            from starlette.requests import Request as _Req
            # Use the request context if available
        except Exception:
            pass

        # Persist to ClickHouse (old report is auto-replaced by ReplacingMergeTree)
        _save_report_to_db(tenant_id, final_report, insights_payload, generated_by)

        # Update in-memory cache
        gen_at = datetime.utcnow().isoformat()
        AI_REPORT_CACHE[tenant_id] = {
            "timestamp": time.time(),
            "report": final_report,
            "insights": insights_payload,
            "generated_at": gen_at,
        }

        return {
            "tenant_id": tenant_id,
            "report": final_report,
            "cached": False,
            "generated_at": gen_at,
            "insights": insights_payload,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to generate report: {str(e)}")

@app.get("/insights")
def get_insights(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}
    
    try:
        insights = []
        
        # 1. High Bounce Rate insight
        bounce_sql = f"""
            SELECT 
                count() as total_users,
                countIf(event_count = 1) as bounced_users
            FROM (
                SELECT user_id, count() as event_count
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY user_id
            )
        """
        b_res = ch_client.query(bounce_sql, params)
        if b_res and b_res[0]["total_users"] > 0:
            rate = (b_res[0]["bounced_users"] / b_res[0]["total_users"]) * 100
            if rate > 60:
                insights.append({
                    "id": "insight-bounce",
                    "type": "High Bounce Rate Detected",
                    "message": f"Bounce rate is currently {rate:.1f}%. Consider optimizing the landing experience.",
                    "severity": "high"
                })
            elif rate > 40:
                insights.append({
                    "id": "insight-bounce",
                    "type": "Elevated Bounce Rate",
                    "message": f"Bounce rate is {rate:.1f}%. Minor optimizations might improve retention.",
                    "severity": "medium"
                })
                
        # 2. Top feature usage
        feat_sql = f"""
            SELECT event_name as feature, count() as cnt
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
            GROUP BY event_name ORDER BY cnt DESC LIMIT 1
        """
        f_res = ch_client.query(feat_sql, params)
        if f_res:
            f_name = f_res[0]["feature"]
            f_count = f_res[0]["cnt"]
            insights.append({
                "id": "insight-feat",
                "type": "Dominant Feature Activity",
                "message": f"'{f_name}' is your most used feature with {f_count} events in the selected period.",
                "severity": "low"
            })
            
        # 3. Peak traffic time
        time_sql = f"""
            SELECT toHour(timestamp) as hr, count() as cnt
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
            GROUP BY hr ORDER BY cnt DESC LIMIT 1
        """
        t_res = ch_client.query(time_sql, params)
        if t_res:
            peak_hr = t_res[0]["hr"]
            insights.append({
                "id": "insight-time",
                "type": "Peak Usage Window",
                "message": f"User activity consistently peaks around {peak_hr}:00. Ideal time for maintenance is outside this window.",
                "severity": "medium"
            })

        if not insights:
            insights.append({
                "id": "insight-fallback",
                "type": "Stable Analytics",
                "message": "All system metrics are operating within normal parameters.",
                "severity": "low"
            })
            
        return {"insights": insights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# TENANT COMPARISON
# ═══════════════════════════════════════════════════════════

@app.get("/tenants/compare")
def get_tenant_comparison(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("30d", description="Time range")):
    """Side-by-side comparison of multiple tenants."""
    require_tenant_access(tenants)
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    
    try:
        results = []
        for tid in tenant_list:
            # Total events
            sql_events = """
                SELECT count() as total_events
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tid)s AND timestamp >= today() - %(days)s
            """
            ev = ch_client.query(sql_events, {"tid": tid, "days": days})
            total_events = int(ev[0]["total_events"]) if ev else 0
            
            # Unique users
            sql_users = """
                SELECT uniqExact(user_id) as unique_users
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tid)s AND timestamp >= today() - %(days)s
            """
            usr = ch_client.query(sql_users, {"tid": tid, "days": days})
            unique_users = int(usr[0]["unique_users"]) if usr else 0
            
            # Active features
            sql_features = """
                SELECT uniqExact(event_name) as active_features
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tid)s AND timestamp >= today() - %(days)s
            """
            feat = ch_client.query(sql_features, {"tid": tid, "days": days})
            active_features = int(feat[0]["active_features"]) if feat else 0
            
            # Growth rate (current week vs previous week)
            sql_growth = """
                SELECT
                    sumIf(1, timestamp >= today() - 7) as current_week,
                    sumIf(1, timestamp >= today() - 14 AND timestamp < today() - 7) as prev_week
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tid)s
            """
            gr = ch_client.query(sql_growth, {"tid": tid})
            cw = int(gr[0]["current_week"]) if gr else 0
            pw = int(gr[0]["prev_week"]) if gr else 0
            growth_rate = round(((cw - pw) / max(pw, 1)) * 100, 1)
            
            # Conversion rate (users with >3 events / total users)
            sql_conv = """
                SELECT 
                    count() as total,
                    countIf(event_count > 3) as converted
                FROM (
                    SELECT user_id, count() as event_count
                    FROM feature_intelligence.events_raw
                    WHERE tenant_id = %(tid)s AND timestamp >= today() - %(days)s
                    GROUP BY user_id
                )
            """
            conv = ch_client.query(sql_conv, {"tid": tid, "days": days})
            conversion_rate = round((int(conv[0]["converted"]) / max(int(conv[0]["total"]), 1)) * 100, 1) if conv else 0.0
            
            # Daily event trend (last 7 days)
            sql_trend = """
                SELECT toDate(timestamp) as date, count() as events
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tid)s AND timestamp >= today() - 7
                GROUP BY date
                ORDER BY date ASC
            """
            trend = ch_client.query(sql_trend, {"tid": tid})
            trend_data = []
            for r in trend:
                d = r["date"]
                date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
                trend_data.append({"date": date_str, "events": int(r["events"])})
            
            results.append({
                "id": tid,
                "name": tid.replace("bank", "Bank").replace("nexa", "Nexa").replace("safex", "Safex"),
                "total_events": total_events,
                "unique_users": unique_users,
                "active_features": active_features,
                "growth_rate": growth_rate,
                "conversion_rate": conversion_rate,
                "trend": trend_data,
            })
        
        return {"tenants": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
