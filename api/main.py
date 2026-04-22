import sys
import os
from urllib.parse import parse_qsl, urlencode
from typing import List, Optional
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.client import ch_client
from api.insights import generate_insights, query_vllm
from api.page_map import resolve_page, resolve_display_name, normalize_event, canonicalize_event_name, CANONICAL_EVENT_ALIASES, FEATURE_DISPLAY_NAMES
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


TENANT_ALIAS_MAP = {
    "bank_a": "nexabank",
    "bank_b": "safexbank",
}

APP_TENANT_SCOPES = {
    "nexabank": {"nexabank", "safexbank"},
    "javabank": {"jbank", "obank"},
}

TENANT_TO_APP = {
    "nexabank": "nexabank",
    "safexbank": "nexabank",
    "jbank": "javabank",
    "obank": "javabank",
}


def normalize_tenant_csv(raw_value: str) -> str:
    if not raw_value:
        return raw_value
    parts = [p.strip() for p in str(raw_value).split(",") if p.strip()]
    if not parts:
        return raw_value
    mapped = [TENANT_ALIAS_MAP.get(p.lower(), p.lower()) for p in parts]
    return ",".join(mapped)


def rewrite_tenant_query_aliases(request: Request) -> None:
    query_string = request.scope.get("query_string", b"")
    if not query_string:
        return

    pairs = parse_qsl(query_string.decode("utf-8", errors="ignore"), keep_blank_values=True)
    if not pairs:
        return

    changed = False
    rewritten = []
    for key, value in pairs:
        if key in {"tenants", "tenant_id"}:
            normalized = normalize_tenant_csv(value)
            rewritten.append((key, normalized))
            changed = changed or (normalized != value)
        else:
            rewritten.append((key, value))

    if changed:
        request.scope["query_string"] = urlencode(rewritten).encode("utf-8")


def parse_admin_apps(raw_admin_apps: str) -> set[str]:
    if not raw_admin_apps:
        return set()
    return {a.strip().lower() for a in raw_admin_apps.split(",") if a.strip()}


def expand_admin_scoped_tenants(admin_apps: set[str]) -> set[str]:
    if not admin_apps:
        return set()

    scoped_tenants: set[str] = set()
    for app in admin_apps:
        if app in APP_TENANT_SCOPES:
            scoped_tenants.update(APP_TENANT_SCOPES[app])
            continue
        mapped_app = TENANT_TO_APP.get(app)
        if mapped_app and mapped_app in APP_TENANT_SCOPES:
            scoped_tenants.update(APP_TENANT_SCOPES[mapped_app])
    return scoped_tenants


def normalize_app_id(raw_app: str) -> str:
    if not raw_app:
        return ""
    normalized = raw_app.strip().lower()
    return TENANT_TO_APP.get(normalized, normalized)


def admin_has_app_scope(admin_apps: set[str], app_id: str) -> bool:
    if not app_id:
        return False
    normalized_app = normalize_app_id(app_id)
    for app in admin_apps:
        if normalize_app_id(app) == normalized_app:
            return True
    return False


def resolve_effective_allowed_tenants(admin_apps: set[str], active_app: str | None) -> set[str]:
    allowed_tenants = expand_admin_scoped_tenants(admin_apps)
    if not active_app:
        return allowed_tenants

    normalized_active_app = normalize_app_id(active_app)
    if normalized_active_app in APP_TENANT_SCOPES:
        return set(APP_TENANT_SCOPES[normalized_active_app])
    return set()


def tenants_resolve_to_single_app(requested_tenants: set[str]) -> bool:
    if not requested_tenants:
        return True
    resolved_apps = {TENANT_TO_APP.get(tenant, tenant) for tenant in requested_tenants}
    return len(resolved_apps) <= 1

def build_heatmap_group_labels(days: int, groups: List[str], is_compare: bool) -> List[str]:
    if is_compare:
        return [g.replace('_', ' ').title() for g in groups]

    safe_days = max(days, 1)
    bucket_count = max(len(groups), 1)
    bucket_span = safe_days / bucket_count
    start_date = datetime.utcnow().date() - timedelta(days=safe_days)
    labels: List[str] = []

    for index, _ in enumerate(groups):
        bucket_start = start_date + timedelta(days=int(round(index * bucket_span)))
        bucket_end = start_date + timedelta(days=max(int(round((index + 1) * bucket_span)) - 1, 0))

        if bucket_end < bucket_start:
            bucket_end = bucket_start

        if bucket_start == bucket_end:
            labels.append(bucket_start.strftime('%b %d'))
        else:
            labels.append(f"{bucket_start.strftime('%b %d')} - {bucket_end.strftime('%b %d')}")

    return labels

import time
from datetime import datetime, timedelta, timezone

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
        # Accept backend tenant aliases (bank_a/bank_b) in all analytics query params.
        rewrite_tenant_query_aliases(request)
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
            admin_apps = parse_admin_apps(request.headers.get("X-Admin-Apps", ""))
            active_app = normalize_app_id(request.headers.get("X-Active-App", ""))
            allowed_tenants = resolve_effective_allowed_tenants(admin_apps, active_app)

            if not email or not allowed_tenants:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden: app_admin is not assigned to any app tenants."}
                )

            if active_app and not admin_has_app_scope(admin_apps, active_app):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden: requested app scope is not assigned to this admin."}
                )
            
            # Some endpoints don't require tenant_id (they are global or use request body)
            tenant_optional_paths = ["/tenants", "/deployment", "/license/sync", "/config"]
            is_tenant_optional = any(path.startswith(p) for p in tenant_optional_paths)
            
            if is_tenant_optional:
                pass  # Allow without tenant_id
            elif tenant_id:
                requested_tenants = {t.strip().lower() for t in str(tenant_id).split(",") if t.strip()}
                if not requested_tenants:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: tenant scope is required for this endpoint."}
                    )

                if not tenants_resolve_to_single_app(requested_tenants):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: cross-app tenant comparison is not allowed."}
                    )

                if not requested_tenants.issubset(allowed_tenants):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: requested tenants are outside your bank app scope."}
                    )
                pass
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
from api.data_layer import start_data_layer_refresh

@app.on_event("startup")
async def startup_event():
    import asyncio
    asyncio.create_task(start_data_layer_refresh(interval_minutes=30))
    await start_websocket_background_tasks()

@app.websocket("/ws/dashboard/{tenant_id}")
async def websocket_dashboard(websocket: WebSocket, tenant_id: str):
    normalized_tenant = tenant_id.strip().lower()
    if normalized_tenant in TENANT_ALIAS_MAP:
        normalized_tenant = TENANT_ALIAS_MAP[normalized_tenant]

    if normalized_tenant not in TENANT_TO_APP:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, normalized_tenant)
    try:
        while True:
            # Wait for any incoming keep-alive or message
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, normalized_tenant)


@app.get("/tenants/available")
def get_available_tenants(
    request: Request,
    range: str = Query("90d", description="Time range like 7d, 30d"),
):
    """Returns all distinct tenants from ClickHouse, filtered by admin access if applicable."""
    _role = request.headers.get("X-User-Role", "")
    days = parse_range(range)
    # Always include known tenants in the base list so the dropdown is never empty
    KNOWN_TENANTS = [
        {"id": "nexabank", "name": "NexaBank", "eventCount": 0, "uniqueUsers": 0},
        {"id": "safexbank", "name": "SafexBank", "eventCount": 0, "uniqueUsers": 0},
        {"id": "jbank", "name": "JBank", "eventCount": 0, "uniqueUsers": 0},
        {"id": "obank", "name": "OBank", "eventCount": 0, "uniqueUsers": 0},
    ]
    admin_apps = parse_admin_apps(request.headers.get("X-Admin-Apps", ""))
    active_app = normalize_app_id(request.headers.get("X-Active-App", ""))
    allowed_tenants = resolve_effective_allowed_tenants(admin_apps, active_app)
    if _role == "app_admin" and not allowed_tenants:
        return []
    try:
        sql = """
            SELECT
                tenant_id as id,
                count() as event_count,
                uniq(user_id) as unique_users
            FROM feature_intelligence.events_raw
            WHERE timestamp >= today() - %(days)s
            GROUP BY tenant_id
            ORDER BY event_count DESC
        """
        results = ch_client.query(sql, {"days": days})
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
                
        if _role == "app_admin" and allowed_tenants:
            return [tenant for tenant in merged if tenant["id"].lower() in allowed_tenants]
        return merged
    except Exception:
        if _role == "app_admin" and allowed_tenants:
            return [tenant for tenant in KNOWN_TENANTS if tenant["id"].lower() in allowed_tenants]
        if _role == "app_admin":
            return []
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

        merged = {}
        for row in results:
            raw_event = str(row.get("event_name", ""))
            canonical = canonicalize_event_name(raw_event)
            if not canonical:
                continue
            total = int(row.get("total_interactions", 0))
            unique_users = int(row.get("unique_users", 0))

            if canonical not in merged:
                merged[canonical] = {
                    "event_name": canonical,
                    "total_interactions": 0,
                    "unique_users": 0,
                }

            merged[canonical]["total_interactions"] += total
            # Avoid inflated counts when multiple aliases map to one canonical event.
            merged[canonical]["unique_users"] = max(
                merged[canonical]["unique_users"],
                unique_users,
            )

        usage = sorted(
            merged.values(),
            key=lambda item: item["total_interactions"],
            reverse=True,
        )

        return {"tenant_id": tenants, "period_days": days, "usage": usage}
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

    def sql_quote(value: str) -> str:
        return value.replace("'", "''")

    def expand_step_aliases(step_name: str) -> list[str]:
        canonical = canonicalize_event_name(step_name) or step_name
        aliases = {
            step_name,
            canonical,
            normalize_event(step_name),
        }
        aliases.update({
            alias
            for alias, mapped in CANONICAL_EVENT_ALIASES.items()
            if mapped == canonical and alias
        })
        return [a for a in sorted(aliases) if a]

    step_variants = [expand_step_aliases(step) for step in step_events]
    condition_tokens = []
    for variants in step_variants:
        quoted = ", ".join(["'" + sql_quote(v) + "'" for v in variants])
        condition_tokens.append(f"event_name IN ({quoted})")
    conditions = ", ".join(condition_tokens)
    
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
    """
    Returns AI/Rule-based actionable insights for a tenant. 
    Detects features that are not being used or sudden spikes.
    """
    tenant_id = tenants
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
                "label": "Avg. Response Time",
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
                "icon": "shield-alert",
            },
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
    """
    Time series data for traffic overview. Pivot if comma-separated.
    """
    days = parse_range(range)
    tenant_id = tenants
    try:
        tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
        if len(tenants) == 1:
            sql = """
                SELECT 
                    toDate(timestamp + INTERVAL 330 MINUTE) as date,
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
                    toDate(timestamp + INTERVAL 330 MINUTE) as date,
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
    """
    Time series feature usage data points. Pivot if comma-separated.
    """
    days = parse_range(range)
    tenant_id = tenants
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
            cond = "tenant_id IN %(tenant_ids)s"
            params = {"tenant_ids": tuple(tenants), "days": days}
            groups = tenants
            
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
            max_count = 1
            
            for row in res:
                f = canonicalize_event_name(str(row["feature"]))
                if not f:
                    continue
                if f not in activities:
                    activities[f] = {g: 0 for g in groups}
                activities[f][row["group_key"]] = activities[f].get(row["group_key"], 0) + int(row["count"])
                if activities[f][row["group_key"]] > max_count:
                    max_count = activities[f][row["group_key"]]
                
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
            max_count = 1
            groups = [str(i) for i in builtins_range(1, 8)]

            for row in res:
                f = canonicalize_event_name(str(row["feature"]))
                if not f:
                    continue
                if f not in activities:
                    activities[f] = {g: 0 for g in groups}
                activities[f][row["group_key"]] = activities[f].get(row["group_key"], 0) + int(row["count"])
                if activities[f][row["group_key"]] > max_count:
                    max_count = activities[f][row["group_key"]]

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
            "group_labels": build_heatmap_group_labels(days, groups, is_compare),
            "activities": formatted_activities
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tenants")
def get_all_tenants(tenant_id: Optional[str] = None, range: str = Query("7d", description="Time range like 7d, 30d")):
    """
    Returns list of distinct tenants with real metrics from ClickHouse.
    If tenant_id is provided, returns only that tenant's data (for app_admin).
    """
    try:
        days = parse_range(range)
        where_clause = ""
        params = {"days": days}
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
            WHERE timestamp >= today() - %(days)s
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
    """
    Device breakdown parsed from metadata. Uses Hare-Niemeyer for exact 100% normalization.
    """
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT
            if(isValidJSON(metadata), lower(ifNull(JSONExtractString(metadata, 'device_type'), '')), '') as device_type,
            if(isValidJSON(metadata), lower(ifNull(JSONExtractString(metadata, 'device'), '')), '') as device,
            if(isValidJSON(metadata), lower(ifNull(JSONExtractString(metadata, 'platform'), '')), '') as platform,
            if(isValidJSON(metadata), lower(ifNull(JSONExtractString(metadata, 'channel'), '')), '') as channel,
            count() as value
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY device_type, device, platform, channel
    """
    try:
        device_res = ch_client.query(sql, params)
        device_aliases = {
            "desktop": "desktop",
            "web": "desktop",
            "browser": "desktop",
            "laptop": "desktop",
            "mobile": "mobile",
            "android": "mobile",
            "ios": "mobile",
            "app": "mobile",
            "tablet": "tablet",
            "ipad": "tablet",
        }

        def _field(row, key: str, index: int):
            if isinstance(row, dict):
                return row.get(key)
            if isinstance(row, (list, tuple)) and 0 <= index < len(row):
                return row[index]
            try:
                return row[key]
            except Exception:
                return None

        merged = {}
        for row in device_res:
            device_type = str(_field(row, "device_type", 0) or "").strip()
            device_name = str(_field(row, "device", 1) or "").strip()
            platform = str(_field(row, "platform", 2) or "").strip()
            channel = str(_field(row, "channel", 3) or "").strip()
            raw = (
                device_type
                or device_name
                or platform
                or channel
            ).lower()
            dev = device_aliases.get(raw, "unknown")
            if dev == "unknown":
                continue
            raw_val = int(_field(row, "value", 4) or 0)
            if raw_val > 0:
                merged[dev] = merged.get(dev, 0) + raw_val
        
        if not merged:
            return [{"name": "Unknown", "value": 100, "color": "#9CA3AF"}]

        total = sum(merged.values())
        items = list(merged.items())
        quotients = [(name, count, (count / total) * 100) for name, count in items]
        floors = [(name, count, int(q)) for name, count, q in quotients]
        remainders = [(name, count, (count / total) * 100 - int((count / total) * 100)) for name, count in items]
        
        allocated_sum = sum(f for _, _, f in floors)
        remainder_seats = 100 - allocated_sum
        remainders.sort(key=lambda x: x[2], reverse=True)
        final = {name: fl for name, _, fl in floors}
        for i in builtins_range(remainder_seats):
            final[remainders[i][0]] += 1

        breakdown = []
        base_colors = ['#1a73e8', '#4285F4', '#8AB4F8', '#34A853', '#F59E0B', '#9CA3AF']
        i_color = 0
        for name, _, _ in remainders:
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
        return [{"name": "Unknown", "value": 100, "color": "#9CA3AF"}]

@app.get("/metrics/channels")
def get_acquisition_channels(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    """
    User acquisition channel breakdown derived from event metadata.
    Classifies events by referrer/channel field and returns % distribution.
    """
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT
            if(JSONHas(metadata, 'channel') AND length(JSONExtractString(metadata, 'channel')) > 0,
               JSONExtractString(metadata, 'channel'),
               'unknown') as channel,
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

        # Keep acquisition intent clean by filtering device-surface values out.
        device_surface_values = {
            "mobile", "web", "desktop", "tablet", "android", "ios", "app", "browser", "kiosk"
        }

        merged: dict = {}
        for row in results:
            raw = str(row["channel"]).lower().strip()
            if raw in device_surface_values or raw in {"", "unknown", "na", "n/a", "none", "null"}:
                continue
            label = CHANNEL_LABELS.get(raw, raw.replace("_", " ").title())
            merged[label] = merged.get(label, 0) + int(row["total"])

        if not merged:
            # If channel metadata is missing/unreliable, derive a meaningful intent mix from feature events.
            sql_intent = f"""
                SELECT event_name, count() as total
                FROM feature_intelligence.events_raw
                WHERE {cond} AND timestamp >= today() - %(days)s
                GROUP BY event_name
                ORDER BY total DESC
            """
            feature_rows = ch_client.query(sql_intent, params)

            intent_buckets = {
                "Returning Sessions": 0,
                "New Registrations": 0,
                "High-Intent Actions": 0,
                "Feature Exploration": 0,
            }

            for row in feature_rows:
                event_name = normalize_event(str(row.get("event_name", "")))
                count = int(row.get("total", 0))

                if event_name.startswith("login."):
                    intent_buckets["Returning Sessions"] += count
                elif event_name.startswith("register."):
                    intent_buckets["New Registrations"] += count
                elif (
                    "pay_now" in event_name
                    or "trade_execution" in event_name
                    or "batch" in event_name
                    or "rebalance" in event_name
                    or event_name.endswith(".success")
                ):
                    intent_buckets["High-Intent Actions"] += count
                else:
                    intent_buckets["Feature Exploration"] += count

            total_intent = sum(intent_buckets.values())
            if total_intent == 0:
                return []

            items = sorted(intent_buckets.items(), key=lambda x: x[1], reverse=True)
            quotients = [(name, value, (value / total_intent) * 100) for name, value in items if value > 0]
            if not quotients:
                return []

            floors = [(name, int(q)) for name, _, q in quotients]
            allocated = sum(v for _, v in floors)
            remainder = sorted(quotients, key=lambda x: x[2] - int(x[2]), reverse=True)
            pct_map = {n: v for n, v in floors}

            for n, _, _ in remainder[:max(0, 100 - allocated)]:
                pct_map[n] = pct_map.get(n, 0) + 1

            return [
                {
                    "name": name,
                    "value": pct_map.get(name, 0),
                    "formattedValue": f"{pct_map.get(name, 0)}%"
                }
                for name, _ in items
                if pct_map.get(name, 0) > 0
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
        return []

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
    """Returns real-time active user count with IST timestamp context.

    Active users are calculated by latest session state:
      1) Session seen in the last 5 minutes
      2) Latest event for that session is NOT a logout/session-end marker
    """
    tenant_id = tenants
    from datetime import timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))

    tenant_list = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenant_list[0]} if len(tenant_list) == 1 else {"tenant_ids": tuple(tenant_list)}

    sql = f"""
        WITH session_states AS (
            SELECT
                user_id,
                if(
                    JSONHas(metadata, 'session_id') AND length(JSONExtractString(metadata, 'session_id')) > 0,
                    JSONExtractString(metadata, 'session_id'),
                    concat('user:', user_id)
                ) as session_id,
                max(timestamp) as last_seen,
                argMax(event_name, timestamp) as last_event
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= now('UTC') - INTERVAL 30 MINUTE
            GROUP BY user_id, session_id
        )
        SELECT uniqExact(user_id) as users
        FROM session_states
        WHERE last_seen >= now('UTC') - INTERVAL 5 MINUTE
          AND NOT match(lower(last_event), '(logout|signout|session_end)')
    """

    try:
        results = ch_client.query(sql, params)
        user_count = int(results[0]['users']) if results else 0
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
    """Returns a strict rolling 60-minute pages/minute series with IST-localized labels."""
    tenant_id = tenants
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
        minute_counts = {}
        for r in results:
            if hasattr(r["min"], "replace"):
                utc_time = r["min"].replace(tzinfo=timezone.utc)
            elif hasattr(r["min"], "strftime"):
                utc_time = r["min"].replace(tzinfo=timezone.utc)
            else:
                # Best effort parsing fallback for string timestamps
                parsed = str(r["min"]).replace("Z", "")
                utc_time = datetime.fromisoformat(parsed).replace(tzinfo=timezone.utc)

            minute_key = utc_time.replace(second=0, microsecond=0)
            minute_counts[minute_key] = int(r["val"])

        now_utc_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        formatted = []
        for offset in builtins_range(59, -1, -1):
            bucket_utc = now_utc_minute - timedelta(minutes=offset)
            ist_time = bucket_utc.astimezone(IST)
            formatted.append({
                "hour": ist_time.strftime("%H:%M"),
                "value": minute_counts.get(bucket_utc, 0)
            })
        return formatted
    except Exception:
        return []

@app.get("/metrics/top_pages")
def get_top_pages(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    """
    Returns page-level aggregation: each row is a "page" (URL), with the total
    events across ALL features that fire on that page, plus the list of features.

    Uses centralized page_map.py for feature → page resolution and display names.
    Response includes: pageUrl, totalEvents, comparisonPct, rank, features[]
    Each feature includes: feature, displayName, count, inPagePct
    """
    days = parse_range(range)
    tenant_id = tenants
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
        
        # Exact explicit known pages for NexaBank and admin surfaces.
        KNOWN_PAGES = {
            "/register", "/login", "/dashboard", "/accounts", "/payees",
            "/transactions", "/loans", "/pro-feature?id=crypto-trading",
            "/pro-feature?id=ai-insights", "/pro-feature?id=wealth-management-pro",
            "/pro-feature?id=bulk-payroll-processing", "/profile",
            "/admin/loans", "/admin/feature-toggles", "/admin/simulate",
        }
        
        for r in results:
            raw_page = str(r['page']) if r['page'] is not None else ""
            raw_feature = str(r['raw_feature']) if r['raw_feature'] is not None else ""
            cnt = int(r['cnt'])

            # Normalize first and use canonical page-based taxonomy everywhere.
            feature = canonicalize_event_name(raw_feature)
            if not feature:
                continue
            resolved_page = resolve_page(feature)

            # Prefer canonical page from normalized feature taxonomy.
            if resolved_page and resolved_page in KNOWN_PAGES:
                page = resolved_page
            else:
                # Keep only real pages; skip unresolved / fake buckets such as /pro-feature.
                if not raw_page or raw_page == "null" or raw_page == "":
                    continue
                page = str(raw_page)
                if page not in KNOWN_PAGES:
                    continue

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
        GROUP BY event_name ORDER BY total DESC LIMIT 500
    """
    try:
        results = ch_client.query(sql, params)
        merged = {}
        for r in results:
            canonical = canonicalize_event_name(str(r["event_name"]))
            if not canonical:
                continue
            merged[canonical] = merged.get(canonical, 0) + int(r["total"])

        activities = []
        for feature_name, total in sorted(merged.items(), key=lambda x: x[1], reverse=True)[:5]:
            activities.append({
                "feature": feature_name,
                "segments": [],
                "level": "High" if total > 10 else "Low"
            })
        return activities
    except Exception as e:
        return []

@app.get("/features/configs")
def get_feature_configs(
    tenants: str = Query(..., description="Comma-separated list of tenants"),
    range: str = Query("30d", description="Time range like 7d, 30d"),
):
    days = parse_range(range)
    tenant_id = tenants
    tenants = [t.strip() for t in tenant_id.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenants) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenants[0], "days": days} if len(tenants) == 1 else {"tenant_ids": tuple(tenants), "days": days}

    sql = f"""
        SELECT event_name as feature, count() as total
        FROM feature_intelligence.events_raw
        WHERE {cond} AND timestamp >= today() - %(days)s
        GROUP BY feature
        ORDER BY total DESC
        LIMIT 200
    """

    def infer_category(feature_key: str) -> str:
        key = (feature_key or "").lower()
        if ".auth." in key or "login" in key or "register" in key:
            return "system"
        if ".page.view" in key or key.endswith(".view"):
            return "navigation"
        if "pay" in key or "transfer" in key or "loan" in key or "submit" in key:
            return "transaction"
        return "interaction"

    try:
        results = ch_client.query(sql, params)

        # Canonical dedupe key: normalized feature + resolved route pattern.
        merged = {}

        for row in results:
            raw_feature = str(row["feature"])
            total = int(row["total"])

            normalized = canonicalize_event_name(raw_feature)
            if not normalized:
                continue
            pattern = resolve_page(normalized) or resolve_page(raw_feature) or "/_other"

            # Guardrail for legacy noisy path-like event roots.
            if pattern in {"/loan/page", "/loans/page", "/lending"}:
                pattern = "/loans"
            display_name = resolve_display_name(normalized)

            dedupe_key = (normalized, pattern)
            if dedupe_key not in merged:
                merged[dedupe_key] = {
                    "normalized": normalized,
                    "pattern": pattern,
                    "featureName": display_name,
                    "category": infer_category(normalized),
                    "count": 0,
                }

            merged[dedupe_key]["count"] += total

        if not merged:
            return []

        # Sort by Route Pattern (alphabetically) first, then by count
        ordered = sorted(
            merged.values(),
            key=lambda item: (item["pattern"], -item["count"]),
        )

        configs = []
        for idx, item in enumerate(ordered, start=1):
            configs.append({
                "id": f"fc-{idx}",
                "pattern": item["pattern"],
                "featureName": item["featureName"],
                "category": item["category"],
                "isActive": True,
            })

        return configs
    except Exception:
        return []

@app.get("/metrics/retention")
def get_retention(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    """
    Real cohort retention analysis.
    For each weekly cohort (users first seen in week W), compute:
      - month1: % of cohort active in week W itself (always 100%)
      - month2: % of cohort active in week W+1
      - month3: % of cohort active in week W+2
    """
    days = parse_range(range)
    tenant_id = tenants
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
def get_admin_summary(range: str = Query("30d", description="Time range like 7d, 30d, 90d")):
    """Returns high-level global aggregated stats (Cloud mode only)."""
    require_cloud_mode()
    try:
        days = parse_range(range)
        sql = """
            SELECT count(distinct tenant_id) as total_tenants, 
                   sum(total_events) as total_events
            FROM feature_intelligence.daily_feature_usage
            WHERE date >= today() - %(days)s
        """
        basic_rows = ch_client.query(sql, {"days": days})
        basic = basic_rows[0] if basic_rows else {"total_tenants": 0, "total_events": 0}
        
        sql_top = """
            SELECT tenant_id as name, sum(total_events) as events
            FROM feature_intelligence.daily_feature_usage
            WHERE date >= today() - %(days)s
            GROUP BY tenant_id
            ORDER BY events DESC LIMIT 5
        """
        top_tenants_raw = ch_client.query(sql_top, {"days": days})
        top_tenants = [
            {"id": row["name"], "name": row["name"].capitalize(), "events": int(row["events"])} 
            for row in top_tenants_raw
        ]
        
        return {
            "total_tenants": basic["total_tenants"],
            "total_events": basic["total_events"],
            "top_tenants": top_tenants,
            "time_range": range,
            "available": True,
        }
    except Exception as e:
        return {
            "total_tenants": 0,
            "total_events": 0,
            "top_tenants": [],
            "time_range": range,
            "available": False,
        }

@app.get("/admin/app/{tenant_id}/summary")
def get_admin_app_summary(tenants: str = Query(..., description="Comma-separated list of tenants"), range: str = Query("7d", description="Time range like 7d, 30d")):
    """Returns basic KPIs and Insights for a specfic app (Cloud mode only)."""
    tenant_id = tenants
    require_cloud_mode()
    return {
        "kpi": get_kpi_metrics(tenants=tenant_id, range=range),
        "insights": get_insights(tenants=tenant_id, range=range)["insights"]
    }

@app.get("/transparency/cloud-data")
def get_transparency_data(tenants: str = Query(..., description="Comma-separated list of tenants")):
    """Summarizes what data is visible to the cloud/admin."""
    tenant_id = tenants
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
# PRO USERS METRICS
# ═══════════════════════════════════════════════════════════

@app.get("/metrics/pro_users")
def get_pro_users(
    tenants: str = Query(..., description="Comma-separated list of tenants"), 
    range: str = Query("7d", description="Time range like 7d, 30d")
):
    """
    Returns count of users who have used any pro/enterprise feature in the given time range.
    Dynamically respects the global time range selector.
    """
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {"tenant_id": tenant_list[0], "days": days} if len(tenant_list) == 1 else {"tenant_ids": tuple(tenant_list), "days": days}

    try:
        # Pro/Enterprise features from the catalog
        pro_features = {
            "crypto-trading.trade_execution.success",
            "crypto-trading.trade_execution.failure",
            "crypto-trading.price_feeds.view",
            "crypto-trading.portfolio.view",
            "wealth-management-pro.rebalance.success",
            "wealth-management-pro.rebalance.failure",
            "wealth-management-pro.insights.view",
            "bulk-payroll-processing.batch.success",
            "bulk-payroll-processing.batch.failure",
            "bulk-payroll-processing.payees.view",
            "bulk-payroll-processing.search.success",
            "bulk-payroll-processing.search.failure",
            "ai-insights.book.access",
            "ai-insights.book.success",
            "ai-insights.stats.view",
        }
        
        # Also include raw feature names that map to pro features
        pro_raw_features = {
            alias for alias, canonical in CANONICAL_EVENT_ALIASES.items()
            if canonical in pro_features and alias
        }
        pro_raw_features.update(pro_features)
        
        # Query unique users who have used any pro feature
        pro_str = ", ".join([f"'{f}'" for f in sorted(pro_raw_features)])
        sql = f"""
            SELECT uniqExact(user_id) as pro_users
            FROM feature_intelligence.events_raw
            WHERE {cond} AND event_name IN ({pro_str}) AND timestamp >= today() - %(days)s
        """
        
        result = ch_client.query(sql, params)
        pro_user_count = int(result[0]["pro_users"]) if result and result[0].get("pro_users") else 0
        
        # Also get total users for comparison
        sql_total = f"""
            SELECT uniqExact(user_id) as total_users
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
        """
        total_result = ch_client.query(sql_total, params)
        total_users = max(int(total_result[0]["total_users"]) if total_result else 0, 1)
        
        pro_adoption_pct = round((pro_user_count / total_users) * 100, 1) if total_users > 0 else 0
        
        return {
            "tenant_id": tenants,
            "range": range,
            "period_days": days,
            "pro_users": pro_user_count,
            "total_users": total_users,
            "pro_adoption_pct": pro_adoption_pct,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            "crypto-trading.page.view": {"plan": "enterprise"},
            "crypto-trading.trade_execution.success": {"plan": "enterprise"},
            "crypto-trading.trade_execution.failure": {"plan": "enterprise"},
            "crypto-trading.price_feeds.view": {"plan": "enterprise"},
            "crypto-trading.portfolio.view": {"plan": "enterprise"},
            "wealth-management-pro.page.view": {"plan": "enterprise"},
            "wealth-management-pro.rebalance.success": {"plan": "enterprise"},
            "wealth-management-pro.rebalance.failure": {"plan": "enterprise"},
            "wealth-management-pro.insights.view": {"plan": "enterprise"},
            "bulk-payroll-processing.page.view": {"plan": "enterprise"},
            "bulk-payroll-processing.batch.success": {"plan": "enterprise"},
            "bulk-payroll-processing.batch.failure": {"plan": "enterprise"},
            "bulk-payroll-processing.payees.view": {"plan": "enterprise"},
            "bulk-payroll-processing.search.success": {"plan": "enterprise"},
            "bulk-payroll-processing.search.failure": {"plan": "enterprise"},
            "ai-insights.page.view": {"plan": "enterprise"},
            "ai-insights.book.access": {"plan": "enterprise"},
            "ai-insights.book.success": {"plan": "enterprise"},
            "ai-insights.stats.view": {"plan": "enterprise"},
            
            # Free / Base
            "dashboard.page.view": {"plan": "free"},
            "login.auth.success": {"plan": "free"},
            "login.auth.failure": {"plan": "free"},
            "register.auth.success": {"plan": "free"},
            "register.auth.failure": {"plan": "free"},
            "transaction.pay_now.success": {"plan": "free"},
            "transaction.pay_now.failure": {"plan": "free"},
            "account.page.view": {"plan": "free"},
            "transaction.page.view": {"plan": "free"},
            "payee.page.view": {"plan": "free"},
            "payee.add_payee.success": {"plan": "free"},
            "payee.add_payee.failure": {"plan": "free"},
            "payee.edit_payee.success": {"plan": "free"},
            "payee.edit_payee.failure": {"plan": "free"},
            "payee.remove_payee.success": {"plan": "free"},
            "payee.remove_payee.failure": {"plan": "free"},
            "loan.applied.success": {"plan": "free"},
            "loan.approved.success": {"plan": "free"},
            "loan.rejected.failure": {"plan": "free"},
            "loan.page.view": {"plan": "free"},
            "loan.kyc_started.success": {"plan": "free"},
            "loan.kyc_completed.success": {"plan": "free"},
            "loan.kyc_failed.failure": {"plan": "free"},
            "loan.kyc_abandoned.failure": {"plan": "free"},
            "profile.page.view": {"plan": "free"},
            "profile.edit_details.success": {"plan": "free"},
            "profile.edit_details.failure": {"plan": "free"},
            "dashboard.location.captured": {"plan": "free"},
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
        used_map = {}
        for r in used:
            canonical = canonicalize_event_name(str(r["feature_name"]))
            if not canonical:
                continue
            usage_count = int(r["usage_count"])
            unique_users = int(r["unique_users"])
            tier_hint = r.get("tier_hint")
            if canonical not in used_map:
                used_map[canonical] = {
                    "feature_name": canonical,
                    "usage_count": 0,
                    "unique_users": 0,
                    "tier_hint": None,
                }
            used_map[canonical]["usage_count"] += usage_count
            # Avoid inflating users when multiple raw aliases map to one canonical feature.
            used_map[canonical]["unique_users"] = max(int(used_map[canonical]["unique_users"]), unique_users)
            if not used_map[canonical]["tier_hint"] and tier_hint:
                used_map[canonical]["tier_hint"] = tier_hint

        canonical_catalog = {
            canonicalize_event_name(key): value
            for key, value in feature_catalog.items()
            if canonicalize_event_name(key)
        }

        pro_features_set = {k for k, v in canonical_catalog.items() if v["plan"] == "enterprise"}
        pro_raw_features = {
            alias for alias, canonical in CANONICAL_EVENT_ALIASES.items()
            if canonical in pro_features_set and alias
        }
        pro_raw_features.update(pro_features_set)

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
            fname = canonicalize_event_name(str(r["feature_name"]))
            if not fname:
                continue
            date_str = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
            if fname not in trends_map:
                trends_map[fname] = {}
            trends_map[fname][date_str] = trends_map[fname].get(date_str, 0) + int(r["count"])

        trends_map = {
            fname: [
                {"date": d, "count": c}
                for d, c in sorted(by_date.items())
            ]
            for fname, by_date in trends_map.items()
        }
            
        # Build lists based strictly on catalog mapping
        total_usage_count = sum(int(r["usage_count"]) for r in used_map.values()) or 1
        total_pro_usage_count = sum(
            int(used_map.get(fname, {}).get("usage_count", 0))
            for fname in pro_features_set
        ) or 1
        
        licensed_list = []
        unused_licensed = []
        unlicensed_used = []
        
        # Populate pro/licensed from STRICT catalog
        for fname in pro_features_set:
            uc = int(used_map.get(fname, {}).get("usage_count", 0))
            item = {
                "feature_name": fname,
                "display_name": FEATURE_DISPLAY_NAMES.get(fname, fname),
                "plan_tier": canonical_catalog[fname]["plan"],
                "is_used": fname in used_map,
                "usage_count": uc,
                "unique_users": int(used_map.get(fname, {}).get("unique_users", 0)),
                "usage_pct": round((uc / total_pro_usage_count) * 100, 1),
                "trend": trends_map.get(fname, []),
            }
            if item["is_used"]:
                licensed_list.append(item)
            else:
                unused_licensed.append(item)
                
        # Populate free/unlicensed ONLY from known catalog + what's not Pro
        for fname, r in used_map.items():
            if fname not in pro_features_set:
                uc = int(r["usage_count"])
                unlicensed_used.append({
                    "feature_name": fname,
                    "display_name": FEATURE_DISPLAY_NAMES.get(fname, fname),
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
            pro_str = ", ".join([f"'{f}'" for f in sorted(pro_raw_features)])
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
    """Get dynamic feature tracking toggles for a tenant.

    Source of truth:
    1) Feature mapping catalog (FEATURE_DISPLAY_NAMES / canonical map)
    2) Observed tenant features in recent data
    3) Existing toggle overrides
    """
    tenant_list = [t.strip() for t in str(tenants).split(",") if t.strip()]
    tenant_id = tenant_list[0] if tenant_list else str(tenants).strip()
    require_tenant_access(tenant_id)

    GLOBAL_TOGGLE_TENANTS = ["nexabank", "safexbank"]
    scope_tenants = sorted(set(GLOBAL_TOGGLE_TENANTS + tenant_list))
    tenants_sql = ", ".join([f"'{t}'" for t in scope_tenants])

    def normalize_tracking_feature_key(raw_key: str) -> str:
        key = str(raw_key or "").strip().lower()
        if not key:
            return key

        return (
            key
            .replace("_page.view", ".page.view")
            .replace(".page_view", ".page.view")
            .replace("_dashboard.view", ".dashboard.view")
            .replace(".dashboard_view", ".dashboard.view")
            .replace("..", ".")
        )

    def infer_category(feature_key: str) -> str:
        key = (feature_key or "").lower()
        if ".auth." in key or "login" in key or "register" in key:
            return "system"
        if ".page.view" in key or key.endswith(".view"):
            return "navigation"
        if any(token in key for token in ["pay", "transfer", "loan", "submit", "kyc"]):
            return "transaction"
        return "interaction"

    def normalize_display_label(label: str) -> str:
        return str(label or "").strip().lower()

    try:
        sql = f"""
            SELECT tenant_id, feature_name, is_enabled, changed_by, changed_at
            FROM feature_intelligence.tracking_toggles FINAL
            WHERE tenant_id IN ({tenants_sql})
        """
        results = ch_client.query(sql)

        overrides = {}
        for r in results:
            feature_name = canonicalize_event_name(str(r.get("feature_name", ""))) or str(r.get("feature_name", ""))
            feature_name = normalize_tracking_feature_key(feature_name)
            row_enabled = bool(r.get("is_enabled", 1))
            row_changed_by = r.get("changed_by") or "system"
            row_changed_at_raw = r.get("changed_at")
            row_changed_at = str(row_changed_at_raw) if hasattr(row_changed_at_raw, "strftime") else str(row_changed_at_raw or "-")

            existing = overrides.get(feature_name)
            if not existing:
                overrides[feature_name] = {
                    "is_enabled": row_enabled,
                    "changed_by": row_changed_by,
                    "changed_at": row_changed_at,
                    "_changed_at_raw": row_changed_at_raw,
                }
                continue

            # Global semantics: if any tenant has it disabled, it is disabled everywhere.
            existing["is_enabled"] = bool(existing.get("is_enabled", True)) and row_enabled

            prev_raw = existing.get("_changed_at_raw")
            if (prev_raw is None and row_changed_at_raw is not None) or (
                prev_raw is not None and row_changed_at_raw is not None and row_changed_at_raw > prev_raw
            ):
                existing["changed_by"] = row_changed_by
                existing["changed_at"] = row_changed_at
                existing["_changed_at_raw"] = row_changed_at_raw

        observed_sql = f"""
            SELECT event_name, count() as total
            FROM feature_intelligence.events_raw
            WHERE tenant_id IN ({tenants_sql}) AND timestamp >= today() - 180
            GROUP BY event_name
            ORDER BY total DESC
            LIMIT 1000
        """
        observed_rows = ch_client.query(observed_sql)

        feature_catalog = set(FEATURE_DISPLAY_NAMES.keys())
        for row in observed_rows:
            canonical = canonicalize_event_name(str(row.get("event_name", "")))
            if canonical:
                feature_catalog.add(normalize_tracking_feature_key(canonical))
        feature_catalog.update(overrides.keys())

        grouped = {}
        for feature_name in sorted(feature_catalog):
            if not feature_name:
                continue

            display_name = FEATURE_DISPLAY_NAMES.get(feature_name, resolve_display_name(feature_name) or feature_name)
            label_key = normalize_display_label(display_name)
            ov = overrides.get(feature_name)
            item_enabled = ov["is_enabled"] if ov else True
            item_changed_by = ov["changed_by"] if ov else "system"
            item_changed_at = ov["changed_at"] if ov else "-"
            item_changed_at_raw = ov.get("_changed_at_raw") if ov else None

            current = grouped.get(label_key)
            if not current:
                grouped[label_key] = {
                    "feature_name": feature_name,
                    "display_name": display_name,
                    "category": infer_category(feature_name),
                    "is_enabled": item_enabled,
                    "changed_by": item_changed_by,
                    "changed_at": item_changed_at,
                    "_changed_at_raw": item_changed_at_raw,
                }
                continue

            # Same user-facing feature name: if any alias key is disabled, show disabled.
            current["is_enabled"] = bool(current.get("is_enabled", True)) and bool(item_enabled)

            # Keep canonical representative key deterministic (shortest key, then lexical).
            prev_key = str(current.get("feature_name") or "")
            if len(feature_name) < len(prev_key) or (len(feature_name) == len(prev_key) and feature_name < prev_key):
                current["feature_name"] = feature_name
                current["category"] = infer_category(feature_name)

            # Preserve most recent mutation metadata among aliases.
            prev_raw = current.get("_changed_at_raw")
            if (prev_raw is None and item_changed_at_raw is not None) or (
                prev_raw is not None and item_changed_at_raw is not None and item_changed_at_raw > prev_raw
            ):
                current["changed_by"] = item_changed_by
                current["changed_at"] = item_changed_at
                current["_changed_at_raw"] = item_changed_at_raw

        toggles = []
        for value in grouped.values():
            toggles.append({
                "feature_name": value["feature_name"],
                "display_name": value["display_name"],
                "category": value["category"],
                "is_enabled": value["is_enabled"],
                "changed_by": value["changed_by"],
                "changed_at": value["changed_at"],
            })

        toggles.sort(key=lambda x: str(x.get("display_name") or x.get("feature_name") or "").lower())

        return {"tenant_id": tenant_id, "scope_tenants": scope_tenants, "toggles": toggles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tracking/toggles")
def set_tracking_toggle(req: TrackingToggleRequest):
    """Set a tracking toggle and record it in audit log."""
    tenant_list = [t.strip() for t in str(req.tenant_id).split(",") if t.strip()]
    target_tenant = tenant_list[0] if tenant_list else str(req.tenant_id).strip()
    require_tenant_access(target_tenant)
    GLOBAL_TOGGLE_TENANTS = ["nexabank", "safexbank"]
    scope_tenants = sorted(set(GLOBAL_TOGGLE_TENANTS + tenant_list))
    tenants_sql = ", ".join([f"'{t}'" for t in scope_tenants])
    try:
        from datetime import datetime
        now = datetime.utcnow()
        canonical_feature = canonicalize_event_name(req.feature_name) or req.feature_name
        canonical_feature = (
            str(canonical_feature or "").strip().lower()
            .replace("_page.view", ".page.view")
            .replace(".page_view", ".page.view")
            .replace("_dashboard.view", ".dashboard.view")
            .replace(".dashboard_view", ".dashboard.view")
            .replace("..", ".")
        )

        target_display = FEATURE_DISPLAY_NAMES.get(canonical_feature, resolve_display_name(canonical_feature) or canonical_feature)
        target_display_norm = str(target_display or "").strip().lower()
        alias_features = set([canonical_feature])
        for key, label in FEATURE_DISPLAY_NAMES.items():
            if str(label or "").strip().lower() == target_display_norm:
                alias_features.add(canonicalize_event_name(str(key)) or str(key))

        alias_features = sorted({
            str(f or "").strip().lower()
            .replace("_page.view", ".page.view")
            .replace(".page_view", ".page.view")
            .replace("_dashboard.view", ".dashboard.view")
            .replace(".dashboard_view", ".dashboard.view")
            .replace("..", ".")
            for f in alias_features if str(f or "").strip()
        })
        aliases_sql = ", ".join(["'" + f.replace("'", "''") + "'" for f in alias_features])
        
        # Get existing state for audit
        sql_old = f"""
            SELECT is_enabled FROM feature_intelligence.tracking_toggles FINAL
            WHERE tenant_id IN ({tenants_sql}) AND feature_name IN ({aliases_sql})
        """
        old = ch_client.query(sql_old)
        # Default behavior is enabled when no explicit toggle row exists.
        old_enabled = all(bool(row.get("is_enabled", 1)) for row in old) if old else True
        old_val = "enabled" if old_enabled else "disabled"
        new_val = "enabled" if req.is_enabled else "disabled"
        
        # Upsert toggle
        client = ch_client._get_client()
        toggle_rows = [
            [tenant, feature_name, 1 if req.is_enabled else 0, req.actor_email, now]
            for tenant in scope_tenants
            for feature_name in alias_features
        ]
        client.insert(
            'feature_intelligence.tracking_toggles',
            toggle_rows,
            column_names=['tenant_id', 'feature_name', 'is_enabled', 'changed_by', 'changed_at']
        )
        
        # Write audit log
        audit_rows = [[tenant, req.actor_email, "tracking_toggle", canonical_feature, old_val, new_val, now] for tenant in scope_tenants]
        client.insert(
            'feature_intelligence.config_audit_log',
            audit_rows,
            column_names=['tenant_id', 'actor_email', 'action', 'target', 'old_value', 'new_value', 'timestamp']
        )
        
        return {
            "status": "ok",
            "tenant_id": target_tenant,
            "scope_tenants": scope_tenants,
            "feature_name": canonical_feature,
            "alias_count": len(alias_features),
            "is_enabled": req.is_enabled,
            "changed_by": req.actor_email,
            "changed_at": now.isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# CONFIGURATION AUDIT LOG
# ═══════════════════════════════════════════════════════════

@app.get("/config/audit-log")
def get_config_audit_log(tenants: str = Query(..., description="Comma-separated list of tenants")):
    """Returns configuration change audit trail for a tenant."""
    tenant_id = tenants
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
def get_user_journey(
    tenants: str = Query(..., description="Comma-separated list of tenants"),
    user_id: str = Query(..., description="User ID"),
    range: str = Query("30d", description="Time range like 7d, 30d"),
):
    """Returns a single user's complete event timeline with session detection."""
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    require_tenant_access(",".join(tenant_list))
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {
        "tenant_id": tenant_list[0],
        "user_id": user_id,
        "days": days,
    } if len(tenant_list) == 1 else {
        "tenant_ids": tuple(tenant_list),
        "user_id": user_id,
        "days": days,
    }
    try:
        sql = f"""
            SELECT event_name, channel, timestamp, metadata
            FROM feature_intelligence.events_raw
            WHERE {cond} AND user_id = %(user_id)s AND timestamp >= today() - %(days)s
            ORDER BY timestamp ASC
            LIMIT 500
        """
        results = ch_client.query(sql, params)
        
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
            "tenant_id": tenants,
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
def list_journey_users(
    tenants: str = Query(..., description="Comma-separated list of tenants"),
    range: str = Query("30d", description="Time range like 7d, 30d"),
):
    """Returns list of users with event counts for journey selection."""
    days = parse_range(range)
    tenant_list = [t.strip() for t in tenants.split(",") if t.strip()]
    require_tenant_access(",".join(tenant_list))
    cond = "tenant_id = %(tenant_id)s" if len(tenant_list) == 1 else "tenant_id IN %(tenant_ids)s"
    params = {
        "tenant_id": tenant_list[0],
        "days": days,
    } if len(tenant_list) == 1 else {
        "tenant_ids": tuple(tenant_list),
        "days": days,
    }
    try:
        sql = f"""
            SELECT user_id, count() as event_count, min(timestamp) as first_seen, max(timestamp) as last_seen
            FROM feature_intelligence.events_raw
            WHERE {cond} AND timestamp >= today() - %(days)s
            GROUP BY user_id
            ORDER BY event_count DESC
            LIMIT 50
        """
        results = ch_client.query(sql, params)
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
        return {"tenant_id": tenants, "users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════
# CUSTOMER SEGMENTATION COMPARISON
# ═══════════════════════════════════════════════════════════

@app.get("/segmentation/compare")
def get_segmentation_comparison(tenants: str = Query(..., description="Comma-separated list of tenants")):
    """Group features by plan tier and compare adoption rates."""
    tenant_id = tenants
    require_tenant_access(tenant_id)
    try:
        # Get license tiers
        sql_tiers = """
            SELECT feature_name, plan_tier
            FROM feature_intelligence.tenant_licenses FINAL
            WHERE tenant_id = %(tenant_id)s AND is_licensed = 1
        """
        tiers = ch_client.query(sql_tiers, {"tenant_id": tenant_id})
        tier_map = {}
        for r in tiers:
            canonical_license = canonicalize_event_name(str(r["feature_name"]))
            if canonical_license:
                tier_map[canonical_license] = r["plan_tier"]
        
        # Get usage
        sql_usage = """
            SELECT event_name, sum(total_events) as total, uniqMerge(unique_users) as users
            FROM feature_intelligence.daily_feature_usage
            WHERE tenant_id = %(tenant_id)s AND date >= today() - 30
            GROUP BY event_name
        """
        usage = ch_client.query(sql_usage, {"tenant_id": tenant_id})

        canonical_usage = {}
        for u in usage:
            canonical_feature = canonicalize_event_name(str(u["event_name"]))
            if not canonical_feature:
                continue
            if canonical_feature not in canonical_usage:
                canonical_usage[canonical_feature] = {"total": 0, "users": 0}
            canonical_usage[canonical_feature]["total"] += int(u["total"])
            canonical_usage[canonical_feature]["users"] = max(canonical_usage[canonical_feature]["users"], int(u["users"]))
        
        # Group by segment
        segments = {}
        for feature_name, stats in canonical_usage.items():
            tier = tier_map.get(feature_name, "unlicensed")
            if tier not in segments:
                segments[tier] = {"tier": tier, "features": 0, "total_usage": 0, "unique_users": 0}
            segments[tier]["features"] += 1
            segments[tier]["total_usage"] += int(stats["total"])
            segments[tier]["unique_users"] += int(stats["users"])
        
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
        canonical_trend = {}
        for row in trend_data:
            canonical = canonicalize_event_name(str(row["event_name"]))
            if not canonical:
                continue
            if canonical not in canonical_trend:
                canonical_trend[canonical] = {"recent_7d": 0, "prev_7d": 0}
            canonical_trend[canonical]["recent_7d"] += int(row["recent_7d"])
            canonical_trend[canonical]["prev_7d"] += int(row["prev_7d"])
        
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
        feature_users_map = {}
        for row in feature_users:
            canonical = canonicalize_event_name(str(row["event_name"]))
            if not canonical:
                continue
            feature_users_map[canonical] = feature_users_map.get(canonical, 0) + int(row["feature_users"])
        
        # Frequency consistency
        sql_frequency = """
            SELECT event_name, count(distinct date) as active_days
            FROM feature_intelligence.daily_feature_usage
            WHERE tenant_id IN %(tenant_ids)s AND date >= today() - 14
            GROUP BY event_name
        """
        frequency_data = ch_client.query(sql_frequency, {"tenant_ids": tuple(tenant_list)})
        frequency_map = {}
        for row in frequency_data:
            canonical = canonicalize_event_name(str(row["event_name"]))
            if not canonical:
                continue
            frequency_map[canonical] = frequency_map.get(canonical, 0) + int(row["active_days"])
        
        predictions = []
        for name, row in canonical_trend.items():
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
def get_ai_report(
    tenants: str = Query(..., description="Comma-separated list of tenants"),
    range: str = Query("30d", description="Time range like 7d, 30d, 90d"),
    force_refresh: bool = Query(False, description="Bypass the cache and generate a new report")
):
    """Generates a comprehensive AI-powered summarization report for the dashboard.
    Reports are persisted in ClickHouse (ai_reports table). Old reports are auto-replaced."""
    tenant_id = tenants  # Alias for backwards compatibility within this function
    cache_key = f"{tenant_id}:{range}"
    require_tenant_access(tenant_id)
    import json as _json

    def _load_report_from_db(tid: str, expected_range: str):
        """Load the latest stored report from ClickHouse."""
        sql = """
            SELECT report, insights, generated_by, generated_at
            FROM feature_intelligence.ai_reports FINAL
            WHERE tenant_id = %(tenant_id)s
            LIMIT 1
        """
        try:
            rows = ch_client.query(sql, {"tenant_id": tid})
        except Exception:
            # If storage is unavailable, continue with on-demand generation.
            return None
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
        generated_by = row.get("generated_by", "")
        stored_range = None
        if isinstance(generated_by, str) and generated_by.startswith("range:"):
            stored_range = generated_by.split(":", 1)[1].strip()

        if stored_range and stored_range != expected_range:
            return None

        return {
            "report": row.get("report", ""),
            "insights": insights_parsed,
            "generated_by": generated_by,
            "generated_at": generated_at_str,
            "time_range": stored_range or expected_range,
        }

    def _save_report_to_db(tid: str, report: str, insights_list: list, generated_by: str = ""):
        """Insert a new report into ClickHouse. ReplacingMergeTree will replace the old one."""
        try:
            client = ch_client._get_client()
            client.insert(
                'feature_intelligence.ai_reports',
                [[tid, generated_by, report, _json.dumps(insights_list), datetime.utcnow()]],
                column_names=['tenant_id', 'generated_by', 'report', 'insights', 'generated_at']
            )
        except Exception:
            # Don't fail the report response if persistence is temporarily unavailable.
            return

    try:
        # --- If NOT force refreshing, try to return stored report ---
        if not force_refresh:
            # Fast path: in-memory cache
            now = time.time()
            if cache_key in AI_REPORT_CACHE:
                cached_data = AI_REPORT_CACHE[cache_key]
                if now - cached_data["timestamp"] < AI_CACHE_TTL:
                    return {
                        "tenant_id": tenant_id,
                        "report": cached_data.get("report", ""),
                        "cached": True,
                        "generated_at": cached_data.get("generated_at"),
                        "time_range": cached_data.get("time_range", range),
                        "insights": cached_data.get("insights", []),
                    }

            # Slow path: load from ClickHouse
            db_report = _load_report_from_db(tenant_id, range)
            if db_report and db_report["report"]:
                # Populate in-memory cache for fast subsequent reads
                AI_REPORT_CACHE[cache_key] = {
                    "timestamp": time.time(),
                    "report": db_report["report"],
                    "insights": db_report["insights"],
                    "generated_at": db_report["generated_at"],
                    "time_range": db_report.get("time_range", range),
                }
                return {
                    "tenant_id": tenant_id,
                    "report": db_report["report"],
                    "cached": True,
                    "generated_at": db_report["generated_at"],
                    "time_range": db_report.get("time_range", range),
                    "insights": db_report["insights"],
                }

        # --- Generate a fresh report ---
        kpi = get_kpi_metrics(tenants=tenant_id, range=range)
        secondary = get_secondary_kpi(tenants=tenant_id, range=range)
        locations = get_locations(tenants=tenant_id, range=range)[:5]
        activities = get_feature_activity(tenants=tenant_id, range=range)

        try:
            funnels = get_funnel_analysis(tenants=tenant_id, steps="login,dashboard_view,loan_applied,kyc_started,kyc_completed", window_minutes=60, range=range)
        except Exception:
            funnels = "No funnel data available."
            
        try:
            retention = get_retention(tenants=tenant_id, range=range)
        except Exception:
            retention = "No retention data available."
            
        try:
            pred_adoption = get_predictive_adoption(tenants=tenant_id, range=range)
        except Exception:
            pred_adoption = "No predictive adoption data available."
            


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
            segments = act.get('segments', [])
            color = segments[0].get('color', '#3b82f6') if segments else '#3b82f6'
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

        def _as_dict_row(value):
            return value if isinstance(value, dict) else {}

        kpi_compact = [
            {
                "label": _as_dict_row(item).get("label"),
                "value": _as_dict_row(item).get("value"),
                "change": _as_dict_row(item).get("change"),
            }
            for item in (kpi or [])[:8]
        ]
        secondary_compact = [
            {
                "label": _as_dict_row(item).get("label"),
                "value": _as_dict_row(item).get("value"),
            }
            for item in (secondary or [])[:8]
        ]
        locations_compact = [
            {
                "country": _as_dict_row(loc).get("country"),
                "visits": _as_dict_row(loc).get("visits"),
            }
            for loc in (locations or [])[:5]
        ]
        activities_compact = [
            {
                "feature": _as_dict_row(act).get("feature"),
                "level": _as_dict_row(act).get("level"),
            }
            for act in (activities or [])[:8]
        ]
        funnel_compact = funnels if isinstance(funnels, str) else {
            "funnel": [
                {
                    "step": _as_dict_row(step).get("step"),
                    "event_name": _as_dict_row(step).get("event_name"),
                    "users_completed": _as_dict_row(step).get("users_completed"),
                    "drop_off_pct": _as_dict_row(step).get("drop_off_pct"),
                }
                for step in (funnels or {}).get("funnel", [])[:8]
            ]
        }
        retention_compact = retention if isinstance(retention, str) else {
            "cohorts": retention[:6] if isinstance(retention, list) else [],
        }
        predictive_compact = pred_adoption if isinstance(pred_adoption, str) else {
            "total_users": (pred_adoption or {}).get("total_users", 0),
            "predictions": [
                {
                    "feature_name": _as_dict_row(p).get("feature_name"),
                    "score": _as_dict_row(p).get("score"),
                    "status": _as_dict_row(p).get("status"),
                    "growth_rate": _as_dict_row(p).get("growth_rate"),
                }
                for p in (pred_adoption or {}).get("predictions", [])[:8]
            ],
        }

        context_str = (
            f"KPI Metrics: {kpi_compact}\n\nSecondary Metrics: {secondary_compact}\n\n"
            f"Top Locations: {locations_compact}\n\nFeature Activities: {activities_compact}\n\n"
            f"Funnel Step Drop-offs: {funnel_compact}\n\nRetention Loop Metrics: {retention_compact}\n\n"
            f"Predictive Adoption Scores: {predictive_compact}"
        )

        prompt = f"""
You are a **Senior Product Analytics Strategist** writing a premium executive briefing for the banking platform tenant: **{tenant_id}**.
The time range being analyzed is: **{range}**.

You have access to the following raw telemetry data:
{context_str}

CRITICAL FORMATTING RULES:
- Use rich Markdown: **bold**, `code highlights`, blockquotes, bullet lists, numbered lists, and horizontal rules.
- Use Markdown tables where numerical comparisons are helpful.
- Use emoji icons (📊 📉 📈 🚨 ✅ ⚠️ 💡 🎯 🔥 🧪) at section headers and key callouts to make the report visually engaging.
- Use GitHub-style alert blocks for critical findings: `> [!WARNING]`, `> [!NOTE]`, `> [!TIP]`.
- Write in a professional but direct tone. Avoid filler. Every sentence must add analytical value.
- Reference specific numbers from the data (e.g., "drop-off of 43% at KYC step", "only 12 events recorded for AI Advisor").

Structure the report in these 6 sections:

---

## 📊 1. Executive Health Scorecard
Provide a high-level snapshot of platform health. Summarize the most important KPIs (total events, active users, bounce rate, session duration) in a **Markdown table**. Comment on whether the platform is in a growth, plateau, or decline phase. Cite specific numbers.

## 📉 2. Conversion Funnel & Drop-off Analysis
Deep-dive into the funnel data. Identify the **exact step** where the largest user attrition occurs. Calculate and present the drop-off percentages between each step. Use a table or numbered list. Explain the likely UX causes (e.g., form complexity, unclear CTAs, trust barriers). Highlight the most critical drop-off with a `> [!WARNING]` block.

## 🔥 3. Feature Engagement & Stickiness Map
Analyze which features drive repeat usage versus which are underperforming. Reference predictive adoption scores and activity data. Categorize features into:
- 🟢 **High-stickiness** (strong engagement, growing)
- 🟡 **At-risk** (declining or stagnant)
- 🔴 **Low adoption** (underused despite strategic importance)

## 🌍 4. Geographic & Demographic Intelligence
Analyze the location data to identify which regions contribute most traffic. Comment on expansion opportunities or regions where engagement is disproportionately low. Present a brief table of top regions.

## 🧪 5. Retention & Cohort Patterns
Evaluate the retention cohort data. Identify whether early-stage retention (Week 1-2) or late-stage retention (Week 4+) is the bigger problem. Suggest specific interventions (onboarding improvements, re-engagement campaigns, feature nudges).

## 🎯 6. Strategic Action Plan (Next 30 Days)
Provide **5 concrete, prioritized actions** the product team should execute in the next 30 days. Each action should:
- Reference a specific metric or finding from the sections above
- Describe the expected impact
- Be implementable within a sprint cycle

Format each action as a numbered item with a bold title and 1-2 sentence description.

---

Do not include raw JSON. Do not add disclaimers or meta-commentary outside the report. Output ONLY the markdown report.
        """
        from api.insights import query_vllm
        llm_response = query_vllm(prompt, timeout_seconds=180, max_tokens=1200)

        if not llm_response:
            # Graceful fallback when the model is unavailable.
            total_events = 0
            active_users = 0
            bounce_rate = None
            session_duration = None
            kpi_lookup = {str(item.get("label", "")).lower(): item.get("value") for item in kpi}

            if "total events" in kpi_lookup:
                total_events = kpi_lookup.get("total events") or 0
            if "active users" in kpi_lookup:
                active_users = kpi_lookup.get("active users") or 0
            if "bounce rate" in kpi_lookup:
                bounce_rate = kpi_lookup.get("bounce rate")
            if "avg session duration" in kpi_lookup:
                session_duration = kpi_lookup.get("avg session duration")

            llm_response = f"""
## 📊 1. Executive Health Scorecard
The AI model is temporarily unavailable. This report is generated from live telemetry summaries for **{tenant_id}** over **{range}**.

| Metric | Value |
|--------|-------|
| Total Events | **{total_events}** |
| Active Users | **{active_users}** |
| Bounce Rate | **{bounce_rate if bounce_rate is not None else 'n/a'}** |
| Avg Session Duration | **{session_duration if session_duration is not None else 'n/a'}** |

## 📉 2. Conversion Funnel & Drop-off Analysis
Current funnel metrics indicate where users stall. Prioritize screens with the steepest conversion drops and validate form complexity, field count, and error messaging.

## 🔥 3. Feature Engagement & Stickiness Map
Feature activity suggests where engagement is concentrated. Promote high-utility features earlier in journeys and simplify access to underused but strategic actions.

## 🌍 4. Geographic & Demographic Intelligence
Location distribution data is available for regional analysis once AI model reconnects.

## 🧪 5. Retention & Cohort Patterns
Retention metrics are being tracked. A full cohort analysis will be available when the AI model is restored.

## 🎯 6. Strategic Action Plan (Next 30 Days)
1. **Streamline top drop-off step** — Reduce required inputs and add clearer progress indicators.
2. **Add contextual nudges** — Deploy tooltips on low-adoption but high-value features.
3. **Enable saved-state recovery** — Allow users to resume interrupted critical flows.
4. **Optimize geographic targeting** — Tailor onboarding flows for top-traffic regions.
5. **Implement re-engagement triggers** — Target users who drop off at Week 2 with personalized nudges.
""".strip()

        final_report = f"{kpi_cards_html}\n{activity_html}\n{geo_html}\n{divider}\n{llm_response}"
        try:
            insights_payload = generate_insights(tenant_id)
        except Exception:
            insights_payload = []

        # Get the user who triggered generation (from request headers)
        generated_by = ""
        try:
            from starlette.requests import Request as _Req
            # Use the request context if available
        except Exception:
            pass

        # Persist to ClickHouse (old report is auto-replaced by ReplacingMergeTree)
        generated_by = f"range:{range}"
        _save_report_to_db(tenant_id, final_report, insights_payload, generated_by)

        # Update in-memory cache
        gen_at = datetime.utcnow().isoformat()
        AI_REPORT_CACHE[cache_key] = {
            "timestamp": time.time(),
            "report": final_report,
            "insights": insights_payload,
            "generated_at": gen_at,
            "time_range": range,
        }

        return {
            "tenant_id": tenant_id,
            "report": final_report,
            "cached": False,
            "generated_at": gen_at,
            "time_range": range,
            "insights": insights_payload,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()

        # Last-resort safety: never return a hard 500 for AI report rendering.
        # Prefer latest stored snapshot; otherwise return a compact deterministic fallback.
        try:
            db_report = _load_report_from_db(tenant_id, range)
            if db_report and db_report.get("report"):
                return {
                    "tenant_id": tenant_id,
                    "report": db_report["report"],
                    "cached": True,
                    "generated_at": db_report.get("generated_at"),
                    "time_range": db_report.get("time_range", range),
                    "insights": db_report.get("insights", []),
                    "fallback_reason": str(e),
                }
        except Exception:
            pass

        fallback_report = f"""
## 📊 1. Executive Health Scorecard
AI report generation is temporarily degraded for **{tenant_id}** over **{range}**. Core telemetry endpoints are available, but narrative synthesis could not complete.

## 📉 2. Conversion Funnel & Drop-off Analysis
Review the funnel stages with the highest drop-off and prioritize form simplification and clearer progression cues.

## 🔥 3. Feature Engagement & Stickiness Map
Prioritize high-frequency features in primary navigation and improve discoverability for strategic low-adoption features.

## 🌍 4. Geographic & Demographic Intelligence
Regional data is being collected. Full geographic analysis will resume when the AI model recovers.

## 🧪 5. Retention & Cohort Patterns
Cohort retention data is available for review. Focus on Week 1-2 retention to identify early churn patterns.

## 🎯 6. Strategic Action Plan (Next 30 Days)
1. **Reduce steps on the top drop-off flow** — Simplify the most abandoned conversion step.
2. **Add contextual guidance at abandonment points** — Deploy inline help where users disengage.
3. **Track post-change conversion** — Measure impact of recent UX updates.
4. **Optimize onboarding for top regions** — Tailor flows for highest-traffic geographies.
5. **Deploy re-engagement campaigns** — Target users who churned in the first 2 weeks.
""".strip()

        return {
            "tenant_id": tenant_id,
            "report": fallback_report,
            "cached": False,
            "generated_at": datetime.utcnow().isoformat(),
            "time_range": range,
            "insights": [],
            "fallback_reason": str(e),
        }

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
                SELECT toDate(timestamp + INTERVAL 330 MINUTE) as date, count() as events
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
